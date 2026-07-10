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
    confirmation_phrase,
    execute_plan,
    load_plan,
    run_cli,
    summarize_plan,
)
from srdpm_client import ConfigurationError, SRDPMClient


class NonTTYInput(io.StringIO):
    def isatty(self) -> bool:
        return False


class FakeClient:
    def __init__(
        self,
        *,
        pending: dict[tuple[str, str], list[str]] | None = None,
        approved: dict[tuple[str, str], dict[str, str]] | None = None,
        check_live_result: bool = True,
        approve_error: Exception | None = None,
    ) -> None:
        self.pending = pending or {}
        self.approved = approved or {}
        self.check_live_result = check_live_result
        self.approve_error = approve_error
        self.pending_calls: list[tuple[str, str]] = []
        self.status_calls: list[tuple[str, str]] = []
        self.approve_calls: list[tuple[str, ...]] = []
        self.check_live_calls = 0
        self.closed = False

    def check_live(self) -> bool:
        self.check_live_calls += 1
        return self.check_live_result

    def get_pending_ids(self, person: str, date: str) -> list[str]:
        self.pending_calls.append((person, date))
        return list(self.pending.get((person, date), []))

    def approve_ids(self, approve_ids: list[str] | tuple[str, ...]) -> None:
        self.approve_calls.append(tuple(approve_ids))
        if self.approve_error is not None:
            raise self.approve_error

    def get_approved_statuses(self, person: str, date: str) -> dict[str, str]:
        self.status_calls.append((person, date))
        return dict(self.approved.get((person, date), {}))

    def close(self) -> None:
        self.closed = True


class NetworkBlockedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.socket_patcher = patch(
            "socket.create_connection",
            side_effect=AssertionError("unit tests must not access network"),
        )
        self.request_patcher = patch.object(
            requests.sessions.Session,
            "request",
            side_effect=AssertionError("unit tests must not make HTTP requests"),
        )
        self.socket_patcher.start()
        self.request_patcher.start()
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

    def test_drift_rejects_before_any_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(self.write_plan(directory))
            fake = FakeClient(
                pending={("测试人员A", "2026-07-08"): ["1001", "9999"]}
            )

            report = execute_plan(plan, fake)

            self.assertFalse(report.success)
            self.assertEqual([], fake.approve_calls)
            self.assertIn("实时待审 ID 与计划不一致", report.message)

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
            self.assertEqual([("1001", "1002")], fake.approve_calls)
            self.assertEqual([group_key, group_key], fake.pending_calls)
            self.assertEqual([group_key], fake.status_calls)

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
            self.assertEqual([("1001", "1002")], fake.approve_calls)
            self.assertIn("回读未全部通过", report.message)

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


if __name__ == "__main__":
    unittest.main()
