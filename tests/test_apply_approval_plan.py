from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from apply_approval_plan import (
    OUTCOME_PARTIAL_SUCCESS,
    OUTCOME_REJECTED_NO_CHANGE,
    OUTCOME_STATE_UNKNOWN,
    OUTCOME_SUCCEEDED,
    VERIFICATION_NOT_ATTEMPTED,
    VERIFICATION_UNKNOWN,
    VERIFICATION_VERIFIED_APPROVED,
    canonical_plan_payload,
    confirmation_phrase,
    execute_plan,
    load_plan,
    run_cli,
    summarize_plan,
)
from srdpm_client import (
    APIError,
    ConfigurationError,
    SRDPMClient,
    _is_target_cookie_domain,
    _is_target_url,
)


class NonTTYInput(io.StringIO):
    def isatty(self) -> bool:
        return False


class FakeExecutionLock:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class FakeClient:
    def __init__(
        self,
        *,
        pending: dict[tuple[object, ...], list[str]] | None = None,
        pending_sequences: dict[
            tuple[object, ...], list[list[str]]
        ] | None = None,
        approved: dict[tuple[object, ...], dict[str, str]] | None = None,
        check_live_result: bool = True,
        approve_error: Exception | None = None,
        readback_error: Exception | None = None,
    ) -> None:
        self.pending = pending or {}
        self.pending_sequences = pending_sequences or {}
        self.approved = approved or {}
        self.check_live_result = check_live_result
        self.approve_error = approve_error
        self.readback_error = readback_error
        self.pending_calls: list[tuple[str, str, str | None]] = []
        self.status_calls: list[tuple[str, str, str | None]] = []
        self.approve_calls: list[tuple[str, ...]] = []
        self._sequence_positions: dict[tuple[object, ...], int] = {}
        self.check_live_calls = 0
        self.closed = False

    def check_live(self) -> bool:
        self.check_live_calls += 1
        return self.check_live_result

    @staticmethod
    def _lookup_key(
        values: dict[tuple[object, ...], object],
        person: str,
        date: str,
        user_id: str | None,
    ) -> tuple[object, ...]:
        exact_key = (person, date, user_id)
        if exact_key in values:
            return exact_key
        return (person, date)

    def get_pending_ids(
        self, person: str, date: str, user_id: str | None = None
    ) -> list[str]:
        self.pending_calls.append((person, date, user_id))
        sequence_key = self._lookup_key(
            self.pending_sequences, person, date, user_id
        )
        sequence = self.pending_sequences.get(sequence_key)
        if sequence:
            position = self._sequence_positions.get(sequence_key, 0)
            self._sequence_positions[sequence_key] = position + 1
            return list(sequence[min(position, len(sequence) - 1)])
        pending_key = self._lookup_key(self.pending, person, date, user_id)
        return list(self.pending.get(pending_key, []))

    def approve_ids(self, approve_ids: list[str] | tuple[str, ...]) -> None:
        self.approve_calls.append(tuple(approve_ids))
        if self.approve_error is not None:
            raise self.approve_error

    def get_approved_statuses(
        self, person: str, date: str, user_id: str | None = None
    ) -> dict[str, str]:
        self.status_calls.append((person, date, user_id))
        if self.readback_error is not None:
            raise self.readback_error
        approved_key = self._lookup_key(self.approved, person, date, user_id)
        return dict(self.approved.get(approved_key, {}))

    def close(self) -> None:
        self.closed = True


class NetworkBlockedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.execution_locks: list[FakeExecutionLock] = []

        def acquire_execution_lock() -> FakeExecutionLock:
            execution_lock = FakeExecutionLock()
            self.execution_locks.append(execution_lock)
            return execution_lock

        self.execution_lock_patcher = patch(
            "apply_approval_plan._try_acquire_execution_lock",
            side_effect=acquire_execution_lock,
        )
        self.socket_patcher = patch(
            "socket.create_connection",
            side_effect=AssertionError("unit tests must not access network"),
        )
        self.request_patcher = patch.object(
            requests.sessions.Session,
            "request",
            side_effect=AssertionError("unit tests must not make HTTP requests"),
        )
        self.execution_lock_patcher.start()
        self.socket_patcher.start()
        self.request_patcher.start()
        self.addCleanup(self.execution_lock_patcher.stop)
        self.addCleanup(self.socket_patcher.stop)
        self.addCleanup(self.request_patcher.stop)

    def write_plan(self, directory: str, groups: list[dict] | None = None) -> Path:
        path = Path(directory) / "plan.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "month": "2026-07",
                    "groups": groups
                    or [
                        {
                            "person": "测试人员A",
                            "date": "2026-07-08",
                            "approve_ids": ["1001", "1002"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path


class ApprovalPlanTests(NetworkBlockedTestCase):
    def test_default_cli_is_offline_and_does_not_create_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan_path = self.write_plan(directory)
            output = io.StringIO()
            errors = io.StringIO()
            factory_calls = 0

            def forbidden_factory() -> FakeClient:
                nonlocal factory_calls
                factory_calls += 1
                raise AssertionError("offline mode must not create client")

            code = run_cli(
                [str(plan_path)],
                client_factory=forbidden_factory,
                input_stream=NonTTYInput(),
                output_stream=output,
                error_stream=errors,
            )

            self.assertEqual(0, code)
            self.assertEqual(0, factory_calls)
            self.assertIn("未读取凭据、未联网、未执行审批", output.getvalue())
            self.assertEqual("", errors.getvalue())

    def test_plan_ids_are_deduplicated_before_summary_and_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan_path = self.write_plan(
                directory,
                [
                    {
                        "person": "测试人员A",
                        "date": "2026-07-08",
                        "approve_ids": ["1001", 1001, "1002", "1001"],
                    }
                ],
            )
            plan = load_plan(plan_path)
            summary = summarize_plan(plan)

            self.assertEqual(("1001", "1002"), plan.groups[0].approve_ids)
            self.assertEqual(2, summary.id_count)

    def test_optional_user_id_is_normalized_and_bound_into_plan_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            legacy_plan = load_plan(self.write_plan(directory))
            legacy_hash = summarize_plan(legacy_plan).sha256

            plan = load_plan(
                self.write_plan(
                    directory,
                    [
                        {
                            "person": "测试人员A",
                            "user_id": "  uid-a  ",
                            "date": "2026-07-08",
                            "approve_ids": ["1001", "1002"],
                        }
                    ],
                )
            )

            self.assertIsNone(legacy_plan.groups[0].user_id)
            self.assertNotIn(
                "user_id", canonical_plan_payload(legacy_plan)["groups"][0]
            )
            self.assertEqual("uid-a", plan.groups[0].user_id)
            self.assertEqual(
                "uid-a", canonical_plan_payload(plan)["groups"][0]["user_id"]
            )
            self.assertNotEqual(legacy_hash, summarize_plan(plan).sha256)

    def test_user_id_is_forwarded_to_every_live_identity_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(
                self.write_plan(
                    directory,
                    [
                        {
                            "person": "测试人员A",
                            "user_id": "uid-a",
                            "date": "2026-07-08",
                            "approve_ids": ["1001"],
                        }
                    ],
                )
            )
            identity = ("测试人员A", "2026-07-08", "uid-a")
            fake = FakeClient(
                pending={identity: ["1001"]},
                approved={identity: {"1001": "通过"}},
            )

            report = execute_plan(plan, fake)

            self.assertTrue(report.success)
            self.assertEqual([identity, identity], fake.pending_calls)
            self.assertEqual([identity], fake.status_calls)

    def test_busy_execution_lock_rejects_before_any_client_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(self.write_plan(directory))
            fake = FakeClient(
                pending={("测试人员A", "2026-07-08"): ["1001", "1002"]},
                approved={
                    ("测试人员A", "2026-07-08"): {
                        "1001": "通过",
                        "1002": "通过",
                    }
                },
            )

            with patch(
                "apply_approval_plan._try_acquire_execution_lock",
                return_value=None,
            ):
                report = execute_plan(plan, fake)

            self.assertFalse(report.success)
            self.assertEqual(OUTCOME_REJECTED_NO_CHANGE, report.outcome)
            self.assertEqual((), report.group_results)
            self.assertIn("其他审批执行进程正在运行", report.message)
            self.assertIn("未联网、未提交审批", report.message)
            self.assertEqual([], fake.pending_calls)
            self.assertEqual([], fake.status_calls)
            self.assertEqual([], fake.approve_calls)

    def test_execution_lock_error_rejects_before_any_client_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(self.write_plan(directory))
            fake = FakeClient()

            with patch(
                "apply_approval_plan._try_acquire_execution_lock",
                side_effect=RuntimeError("offline lock failure"),
            ):
                report = execute_plan(plan, fake)

            self.assertFalse(report.success)
            self.assertEqual(OUTCOME_REJECTED_NO_CHANGE, report.outcome)
            self.assertEqual((), report.group_results)
            self.assertIn("审批执行锁不可用（RuntimeError）", report.message)
            self.assertEqual([], fake.pending_calls)
            self.assertEqual([], fake.status_calls)
            self.assertEqual([], fake.approve_calls)

    def test_after_report_runs_exactly_once_before_execution_lock_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(self.write_plan(directory))
            group_key = ("测试人员A", "2026-07-08")
            fake = FakeClient(
                pending={group_key: ["1001", "1002"]},
                approved={group_key: {"1001": "通过", "1002": "通过"}},
            )
            callback_calls: list[tuple[object, object, bool]] = []

            def after_report(received_plan, received_report) -> None:
                callback_calls.append(
                    (
                        received_plan,
                        received_report,
                        self.execution_locks[-1].released,
                    )
                )

            report = execute_plan(plan, fake, after_report=after_report)

            self.assertEqual(1, len(callback_calls))
            self.assertIs(plan, callback_calls[0][0])
            self.assertIs(report, callback_calls[0][1])
            self.assertFalse(callback_calls[0][2])
            self.assertTrue(self.execution_locks[-1].released)

    def test_after_report_receives_precheck_failure_before_lock_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(self.write_plan(directory))
            fake = FakeClient(
                pending={("测试人员A", "2026-07-08"): ["1001", "9999"]}
            )
            callback_reports = []
            callback_lock_states = []

            def after_report(received_plan, received_report) -> None:
                self.assertIs(plan, received_plan)
                callback_reports.append(received_report)
                callback_lock_states.append(self.execution_locks[-1].released)

            report = execute_plan(plan, fake, after_report=after_report)

            self.assertFalse(report.success)
            self.assertEqual("precheck", report.group_results[0].phase)
            self.assertEqual([report], callback_reports)
            self.assertEqual([False], callback_lock_states)
            self.assertTrue(self.execution_locks[-1].released)

    def test_after_report_exception_propagates_and_still_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(self.write_plan(directory))
            group_key = ("测试人员A", "2026-07-08")
            fake = FakeClient(
                pending={group_key: ["1001", "1002"]},
                approved={group_key: {"1001": "通过", "1002": "通过"}},
            )
            callback_count = 0

            def after_report(_received_plan, _received_report) -> None:
                nonlocal callback_count
                callback_count += 1
                self.assertFalse(self.execution_locks[-1].released)
                raise RuntimeError("offline persistence failure")

            with self.assertRaisesRegex(RuntimeError, "offline persistence failure"):
                execute_plan(plan, fake, after_report=after_report)

            self.assertEqual(1, callback_count)
            self.assertTrue(self.execution_locks[-1].released)

    def test_drift_rejects_before_any_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(self.write_plan(directory))
            fake = FakeClient(
                pending={("测试人员A", "2026-07-08"): ["1001", "9999"]}
            )

            report = execute_plan(plan, fake)

            self.assertFalse(report.success)
            self.assertEqual(OUTCOME_REJECTED_NO_CHANGE, report.outcome)
            self.assertEqual([], fake.approve_calls)
            self.assertIn("实时待审 ID 与计划不一致", report.message)
            self.assertEqual("precheck", report.group_results[0].phase)
            self.assertFalse(report.group_results[0].submission_attempted)
            self.assertFalse(report.group_results[0].mutation_possible)
            self.assertEqual(
                VERIFICATION_NOT_ATTEMPTED,
                report.group_results[0].verification_status,
            )
            self.assertTrue(self.execution_locks[-1].released)

    def test_exact_service_selection_allows_other_same_day_ids_but_submits_only_selected_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(
                self.write_plan(
                    directory,
                    [
                        {
                            "person": "测试人员A",
                            "date": "2026-07-08",
                            "approve_ids": ["1001"],
                        }
                    ],
                )
            )
            group_key = ("测试人员A", "2026-07-08")
            fake = FakeClient(
                pending={group_key: ["1001", "1002"]},
                approved={group_key: {"1001": "通过"}},
            )

            report = execute_plan(plan, fake, allow_partial=True)

            self.assertTrue(report.success)
            self.assertEqual([("1001",)], fake.approve_calls)
            self.assertEqual(VERIFICATION_VERIFIED_APPROVED, report.group_results[0].verification_status)

    def test_success_requires_second_pending_check_and_approved_readback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(self.write_plan(directory))
            group_key = ("测试人员A", "2026-07-08")
            fake = FakeClient(
                pending={group_key: ["1002", "1001", "1001"]},
                approved={group_key: {"1001": "通过", "1002": "通过"}},
            )

            report = execute_plan(plan, fake)

            self.assertTrue(report.success)
            self.assertEqual(OUTCOME_SUCCEEDED, report.outcome)
            self.assertEqual([("1001", "1002")], fake.approve_calls)
            expected_identity = ("测试人员A", "2026-07-08", None)
            self.assertEqual(
                [expected_identity, expected_identity], fake.pending_calls
            )
            self.assertEqual([expected_identity], fake.status_calls)
            self.assertEqual("readback", report.group_results[0].phase)
            self.assertTrue(report.group_results[0].submission_attempted)
            self.assertTrue(report.group_results[0].mutation_possible)
            self.assertEqual(
                VERIFICATION_VERIFIED_APPROVED,
                report.group_results[0].verification_status,
            )
            self.assertTrue(self.execution_locks[-1].released)

    def test_failed_readback_returns_failure_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(self.write_plan(directory))
            group_key = ("测试人员A", "2026-07-08")
            fake = FakeClient(
                pending={group_key: ["1001", "1002"]},
                approved={group_key: {"1001": "通过", "1002": "拒绝"}},
            )

            report = execute_plan(plan, fake)

            self.assertFalse(report.success)
            self.assertEqual(OUTCOME_STATE_UNKNOWN, report.outcome)
            self.assertEqual([("1001", "1002")], fake.approve_calls)
            self.assertIn("回读未全部通过", report.message)
            self.assertEqual("readback", report.group_results[0].phase)
            self.assertTrue(report.group_results[0].submission_attempted)
            self.assertTrue(report.group_results[0].mutation_possible)
            self.assertEqual(
                VERIFICATION_UNKNOWN,
                report.group_results[0].verification_status,
            )

    def test_submit_exception_is_not_retried_and_verified_readback_can_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(self.write_plan(directory))
            group_key = ("测试人员A", "2026-07-08")
            fake = FakeClient(
                pending={group_key: ["1001", "1002"]},
                approved={group_key: {"1001": "通过", "1002": "通过"}},
                approve_error=TimeoutError("response lost after submit"),
            )

            report = execute_plan(plan, fake)

            self.assertTrue(report.success)
            self.assertEqual(OUTCOME_SUCCEEDED, report.outcome)
            self.assertEqual([("1001", "1002")], fake.approve_calls)
            self.assertEqual(1, len(fake.status_calls))
            result = report.group_results[0]
            self.assertTrue(result.success)
            self.assertEqual("readback", result.phase)
            self.assertTrue(result.submission_attempted)
            self.assertTrue(result.mutation_possible)
            self.assertEqual(
                VERIFICATION_VERIFIED_APPROVED, result.verification_status
            )

    def test_submit_exception_with_unverified_ids_returns_state_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(self.write_plan(directory))
            group_key = ("测试人员A", "2026-07-08")
            fake = FakeClient(
                pending={group_key: ["1001", "1002"]},
                approved={group_key: {"1001": "通过"}},
                approve_error=TimeoutError("response lost after submit"),
            )

            report = execute_plan(plan, fake)

            self.assertFalse(report.success)
            self.assertEqual(OUTCOME_STATE_UNKNOWN, report.outcome)
            self.assertEqual([("1001", "1002")], fake.approve_calls)
            self.assertEqual(1, len(fake.status_calls))
            result = report.group_results[0]
            self.assertEqual("submit", result.phase)
            self.assertTrue(result.submission_attempted)
            self.assertTrue(result.mutation_possible)
            self.assertEqual(VERIFICATION_UNKNOWN, result.verification_status)

    def test_verified_first_group_then_pre_submit_drift_is_partial_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(
                self.write_plan(
                    directory,
                    [
                        {
                            "person": "测试人员A",
                            "date": "2026-07-08",
                            "approve_ids": ["1001"],
                        },
                        {
                            "person": "测试人员B",
                            "date": "2026-07-09",
                            "approve_ids": ["2001"],
                        },
                    ],
                )
            )
            first_key = ("测试人员A", "2026-07-08")
            second_key = ("测试人员B", "2026-07-09")
            fake = FakeClient(
                pending_sequences={
                    first_key: [["1001"]],
                    second_key: [["2001"], ["9999"]],
                },
                approved={first_key: {"1001": "通过"}},
            )

            report = execute_plan(plan, fake)

            self.assertFalse(report.success)
            self.assertEqual(OUTCOME_PARTIAL_SUCCESS, report.outcome)
            self.assertEqual([("1001",)], fake.approve_calls)
            self.assertEqual(2, len(report.group_results))
            self.assertEqual(
                VERIFICATION_VERIFIED_APPROVED,
                report.group_results[0].verification_status,
            )
            self.assertEqual("pre_submit", report.group_results[1].phase)
            self.assertFalse(report.group_results[1].submission_attempted)
            self.assertFalse(report.group_results[1].mutation_possible)
            self.assertEqual(
                VERIFICATION_NOT_ATTEMPTED,
                report.group_results[1].verification_status,
            )

    def test_verified_first_group_takes_partial_precedence_over_later_unknown(self) -> None:
        class SecondGroupSubmitErrorClient(FakeClient):
            def approve_ids(
                self, approve_ids: list[str] | tuple[str, ...]
            ) -> None:
                ids = tuple(approve_ids)
                self.approve_calls.append(ids)
                if ids == ("2001",):
                    raise TimeoutError("response lost after submit")

        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(
                self.write_plan(
                    directory,
                    [
                        {
                            "person": "测试人员A",
                            "date": "2026-07-08",
                            "approve_ids": ["1001"],
                        },
                        {
                            "person": "测试人员B",
                            "date": "2026-07-09",
                            "approve_ids": ["2001"],
                        },
                    ],
                )
            )
            first_key = ("测试人员A", "2026-07-08")
            second_key = ("测试人员B", "2026-07-09")
            fake = SecondGroupSubmitErrorClient(
                pending={first_key: ["1001"], second_key: ["2001"]},
                approved={first_key: {"1001": "通过"}, second_key: {}},
            )

            report = execute_plan(plan, fake)

            self.assertFalse(report.success)
            self.assertEqual(OUTCOME_PARTIAL_SUCCESS, report.outcome)
            self.assertEqual([("1001",), ("2001",)], fake.approve_calls)
            self.assertEqual(
                VERIFICATION_VERIFIED_APPROVED,
                report.group_results[0].verification_status,
            )
            self.assertEqual(
                VERIFICATION_UNKNOWN,
                report.group_results[1].verification_status,
            )

    def test_non_tty_execute_without_explicit_confirmation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan_path = self.write_plan(directory)
            output = io.StringIO()
            errors = io.StringIO()
            factory_calls = 0

            def forbidden_factory() -> FakeClient:
                nonlocal factory_calls
                factory_calls += 1
                raise AssertionError("confirmation failure must happen before client")

            code = run_cli(
                [str(plan_path), "--execute", "--confirm-month", "2026-07"],
                client_factory=forbidden_factory,
                input_stream=NonTTYInput(),
                output_stream=output,
                error_stream=errors,
            )

            self.assertEqual(2, code)
            self.assertEqual(0, factory_calls)
            self.assertIn("非 TTY", errors.getvalue())

    def test_explicit_confirmation_must_match_current_plan_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan_path = self.write_plan(directory)
            errors = io.StringIO()
            factory_calls = 0

            def forbidden_factory() -> FakeClient:
                nonlocal factory_calls
                factory_calls += 1
                raise AssertionError("mismatch must fail before client creation")

            code = run_cli(
                [
                    str(plan_path),
                    "--execute",
                    "--confirm-month",
                    "2026-07",
                    "--confirmation",
                    "EXECUTE SRDPM APPROVAL stale-summary",
                ],
                client_factory=forbidden_factory,
                input_stream=NonTTYInput(),
                output_stream=io.StringIO(),
                error_stream=errors,
            )

            self.assertEqual(2, code)
            self.assertEqual(0, factory_calls)
            self.assertIn("确认语与当前计划摘要不匹配", errors.getvalue())

    def test_controlled_confirmation_must_match_summary_and_failure_is_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan_path = self.write_plan(directory)
            plan = load_plan(plan_path)
            phrase = confirmation_phrase(summarize_plan(plan))
            fake = FakeClient(
                pending={("测试人员A", "2026-07-08"): ["1001"]}
            )
            output = io.StringIO()
            errors = io.StringIO()

            code = run_cli(
                [
                    str(plan_path),
                    "--execute",
                    "--confirm-month",
                    "2026-07",
                    "--confirmation",
                    phrase,
                ],
                client_factory=lambda: fake,
                input_stream=NonTTYInput(),
                output_stream=output,
                error_stream=errors,
            )

            self.assertEqual(4, code)
            self.assertTrue(fake.closed)
            self.assertEqual([], fake.approve_calls)
            self.assertIn("实时待审 ID 与计划不一致", errors.getvalue())

    def test_check_live_only_checks_login(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan_path = self.write_plan(directory)
            fake = FakeClient()
            output = io.StringIO()
            code = run_cli(
                [str(plan_path), "--check-live"],
                client_factory=lambda: fake,
                input_stream=NonTTYInput(),
                output_stream=output,
                error_stream=io.StringIO(),
            )

            self.assertEqual(0, code)
            self.assertEqual(1, fake.check_live_calls)
            self.assertEqual([], fake.pending_calls)
            self.assertEqual([], fake.approve_calls)
            self.assertTrue(fake.closed)


class ClientConfigurationTests(NetworkBlockedTestCase):
    def test_target_url_and_cookie_checks_reject_lookalike_hosts(self) -> None:
        self.assertTrue(
            _is_target_url(
                "https://rd-mokadisplay.tcl.com/srdpm/#/work-list",
                "/srdpm/",
            )
        )
        self.assertTrue(
            _is_target_url(
                "https://rd-mokadisplay.tcl.com/srdpm-api/workload/approve/list",
                "/srdpm-api/",
            )
        )
        self.assertFalse(
            _is_target_url(
                "https://evil.example/srdpm-api/workload/approve/list",
                "/srdpm-api/",
            )
        )
        self.assertFalse(
            _is_target_url(
                "https://rd-mokadisplay.tcl.com.evil.example/srdpm/",
                "/srdpm/",
            )
        )
        self.assertFalse(
            _is_target_url(
                "https://rd-mokadisplay.tcl.com@evil.example/srdpm/",
                "/srdpm/",
            )
        )
        self.assertTrue(_is_target_cookie_domain(".rd-mokadisplay.tcl.com"))
        self.assertFalse(_is_target_cookie_domain("evil.rd-mokadisplay.tcl.com"))

    def test_user_id_matching_is_strict_and_name_is_only_legacy_fallback(self) -> None:
        records = [
            {
                "cn_name": "同名人员",
                "uid": "uid-a",
                "children": [{"approve_id": "1001"}],
            },
            {
                "cn_name": "同名人员",
                "user_id": "uid-b",
                "children": [{"approve_id": "2001"}],
            },
        ]

        strict = list(
            SRDPMClient._children_for_person(records, "过期姓名", "uid-b")
        )
        legacy = list(SRDPMClient._children_for_person(records, "同名人员"))

        self.assertEqual(["2001"], [item["approve_id"] for item in strict])
        self.assertEqual(
            ["1001", "2001"], [item["approve_id"] for item in legacy]
        )

        conflicting = [
            {
                "cn_name": "同名人员",
                "uid": "uid-a",
                "user_id": "uid-b",
                "children": [],
            }
        ]
        with self.assertRaises(APIError):
            list(
                SRDPMClient._children_for_person(
                    conflicting, "同名人员", "uid-a"
                )
            )

    def test_credentials_are_read_only_from_environment_and_repr_is_masked(self) -> None:
        with patch.dict(
            os.environ,
            {"SRDPM_USERNAME": "offline-user", "SRDPM_PASSWORD": "offline-secret"},
            clear=True,
        ):
            client = SRDPMClient.from_env()

        self.assertTrue(client.verify_tls)
        self.assertNotIn("offline-secret", repr(client))

    def test_tls_can_only_be_disabled_by_explicit_false(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SRDPM_USERNAME": "offline-user",
                "SRDPM_PASSWORD": "offline-secret",
                "SRDPM_VERIFY_TLS": "false",
            },
            clear=True,
        ):
            client = SRDPMClient.from_env()
        self.assertFalse(client.verify_tls)

        with patch.dict(
            os.environ,
            {
                "SRDPM_USERNAME": "offline-user",
                "SRDPM_PASSWORD": "offline-secret",
                "SRDPM_VERIFY_TLS": "0",
            },
            clear=True,
        ):
            with self.assertRaises(ConfigurationError):
                SRDPMClient.from_env()

    def test_client_can_be_created_from_in_memory_credentials_without_repr_leak(self) -> None:
        client = SRDPMClient.from_credentials(" offline-user ", "offline-secret")

        self.assertNotIn("offline-secret", repr(client))
        self.assertNotIn("offline-user", repr(client))
        with self.assertRaises(ConfigurationError):
            SRDPMClient.from_credentials("", "offline-secret")
        with self.assertRaises(ConfigurationError):
            SRDPMClient.from_credentials("offline-user", "bad\nsecret")


if __name__ == "__main__":
    unittest.main()
