from __future__ import annotations

import http.client
import json
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from unittest.mock import patch

from approval_model import make_group_key
from local_approval_server import (
    DASHBOARD_NAME,
    MAX_HTTP_BODY_BYTES,
    SERVICE_BUILD_ID,
    ServiceError,
    create_server,
    rebuild_plan_from_archive,
)
from windows_credential_store import Credentials


class FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeCredentialStore:
    def __init__(self) -> None:
        self.credentials: Credentials | None = None
        self.save_calls: list[tuple[str, str]] = []

    def has_credentials(self) -> bool:
        return self.credentials is not None

    def load(self) -> Credentials | None:
        return self.credentials

    def save(self, username: str, password: str) -> None:
        self.save_calls.append((username, password))
        self.credentials = Credentials(username, password)


class FakeClient:
    def __init__(
        self,
        *,
        pending: Mapping[tuple[str, str], Sequence[str]],
        approved: Mapping[tuple[str, str], Mapping[str, str]],
        fail_on_ids: Sequence[str] = (),
    ) -> None:
        self.pending = {key: list(value) for key, value in pending.items()}
        self.approved = {key: dict(value) for key, value in approved.items()}
        self.fail_on_ids = frozenset(fail_on_ids)
        self.pending_calls: list[tuple[str, str]] = []
        self.pending_user_ids: list[str | None] = []
        self.approve_calls: list[tuple[str, ...]] = []
        self.status_calls: list[tuple[str, str]] = []
        self.status_user_ids: list[str | None] = []
        self.check_live_calls = 0
        self.closed = False

    def check_live(self) -> bool:
        self.check_live_calls += 1
        return True

    def get_pending_ids(
        self, person: str, date: str, user_id: str | None = None
    ) -> list[str]:
        self.pending_calls.append((person, date))
        self.pending_user_ids.append(user_id)
        return list(self.pending.get((person, date), []))

    def approve_ids(self, approve_ids: Sequence[str]) -> None:
        normalized = tuple(str(value) for value in approve_ids)
        self.approve_calls.append(normalized)
        if frozenset(normalized) == self.fail_on_ids:
            raise RuntimeError("offline fake submit failure")

    def get_approved_statuses(
        self, person: str, date: str, user_id: str | None = None
    ) -> dict[str, str]:
        self.status_calls.append((person, date))
        self.status_user_ids.append(user_id)
        return dict(self.approved.get((person, date), {}))

    def close(self) -> None:
        self.closed = True


def _minimal_audit() -> dict[str, Any]:
    return {
        "platform_summary": {},
        "missed": {},
        "no_checkin_leave": [],
        "hours_over": [],
        "hours_low": [],
        "project_mismatch": [],
    }


def _child(approve_id: str, title: str) -> dict[str, Any]:
    return {
        "approve_id": approve_id,
        "items": "G/TEST",
        "title": title,
        "content": "离线测试内容",
        "work_hours": 4,
        "status": "待审",
    }


class LocalApprovalServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {"SRDPM_USERNAME": "", "SRDPM_PASSWORD": ""},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / DASHBOARD_NAME).write_text(
            "<!doctype html><html><head><title>离线看板</title></head><body>OK</body></html>",
            encoding="utf-8",
        )
        self.archive = self.root / "srdpm_archive"
        self.month_dir = self.archive / "2026-07"
        self.month_dir.mkdir(parents=True)
        (self.month_dir / "audit_report.json").write_text(
            json.dumps(_minimal_audit(), ensure_ascii=False), encoding="utf-8"
        )
        self.raw_data = {
            "daily_data": {
                "2026-07-08": {
                    "list": [
                        {
                            "cn_name": "测试人员A",
                            "id": "daily-parent-a",
                            "children": [_child("1001", "A1"), _child("1002", "A2")],
                        }
                    ]
                },
                "2026-07-09": {
                    "list": [
                        {
                            "cn_name": "测试人员B",
                            "uid": "u-b",
                            "children": [_child("2001", "B1")],
                        }
                    ]
                },
                "2026-07-10": {
                    "list": [
                        {
                            "cn_name": "测试人员C",
                            "uid": "u-c",
                            "children": [_child("3001", "C1")],
                        }
                    ]
                },
            }
        }
        self._write_raw()

        self.group_a = make_group_key("2026-07-08", "测试人员A", ["1001"])
        self.group_b = make_group_key("2026-07-09", "测试人员B", ["2001"])
        self.group_c = make_group_key("2026-07-10", "测试人员C", ["3001"])
        self.clock = FakeClock()
        self.credential_store = FakeCredentialStore()
        self.credential_validation_inputs: list[tuple[str, str]] = []
        self.credential_clients: list[FakeClient] = []
        self.native_confirmation_summaries: list[Any] = []
        self.clients: list[FakeClient] = []
        self.client_builder = self._success_client
        self.refresh_calls: list[Path] = []
        self.refresh_started = threading.Event()
        self.release_refresh = threading.Event()
        self.release_refresh.set()
        self.refresh_error: Exception | None = None
        self.dashboard_rebuild_calls: list[Path] = []

        def factory() -> FakeClient:
            client = self.client_builder()
            self.clients.append(client)
            return client

        def credential_factory(username: str, password: str) -> FakeClient:
            self.credential_validation_inputs.append((username, password))
            client = self._success_client()
            self.credential_clients.append(client)
            return client

        def approval_confirmer(summary: Any) -> bool:
            self.native_confirmation_summaries.append(summary)
            return True

        def refresh_runner(project_dir: Path) -> SimpleNamespace:
            self.refresh_calls.append(Path(project_dir))
            self.refresh_started.set()
            if not self.release_refresh.wait(timeout=3):
                raise RuntimeError("offline refresh runner timed out")
            if self.refresh_error is not None:
                raise self.refresh_error
            return SimpleNamespace(
                month="2026-07",
                refreshed_months=("2026-06", "2026-07"),
                mapping_updated=True,
            )

        def dashboard_rebuilder(project_dir: Path) -> Path:
            self.dashboard_rebuild_calls.append(Path(project_dir))
            return Path(project_dir) / DASHBOARD_NAME

        self.server = create_server(
            port=0,
            project_dir=self.root,
            archive_root=self.archive,
            client_factory=factory,
            credential_store=self.credential_store,
            credential_client_factory=credential_factory,
            approval_confirmer=approval_confirmer,
            refresh_runner=refresh_runner,
            dashboard_rebuilder=dashboard_rebuilder,
            monotonic=self.clock,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.release_refresh.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _write_raw(self) -> None:
        (self.month_dir / "raw_data.json").write_text(
            json.dumps(self.raw_data, ensure_ascii=False), encoding="utf-8"
        )

    def _success_client(self) -> FakeClient:
        return FakeClient(
            pending={
                ("测试人员A", "2026-07-08"): ["1002", "1001"],
                ("测试人员B", "2026-07-09"): ["2001"],
                ("测试人员C", "2026-07-10"): ["3001"],
            },
            approved={
                ("测试人员A", "2026-07-08"): {"1001": "通过", "1002": "通过"},
                ("测试人员B", "2026-07-09"): {"2001": "通过"},
                ("测试人员C", "2026-07-10"): {"3001": "通过"},
            },
        )

    @property
    def host(self) -> str:
        return self.server.expected_host

    def _request(
        self,
        method: str,
        path: str,
        payload: Any | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        secure: bool = True,
    ) -> tuple[int, dict[str, str], Any]:
        request_headers: dict[str, str] = {"Host": self.host}
        if secure:
            request_headers.update(
                {
                    "Origin": self.server.origin,
                    "Sec-Fetch-Site": "same-origin",
                    "X-SRDPM-CSRF": self.server.service.csrf_token,
                }
            )
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        response_body = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        parsed: Any = None
        if response_body:
            if "application/json" in response_headers.get("content-type", ""):
                parsed = json.loads(response_body.decode("utf-8"))
            else:
                parsed = response_body.decode("utf-8")
        return response.status, response_headers, parsed

    def _prepare(self, group_keys: Sequence[str]) -> dict[str, Any]:
        status, _, body = self._request(
            "POST",
            "/api/v1/approval/prepare",
            {"month": "2026-07", "group_keys": list(group_keys)},
        )
        self.assertEqual(200, status, body)
        return body["prepared"]

    def _execute(self, ticket: str) -> tuple[int, Any]:
        status, _, body = self._request(
            "POST", "/api/v1/approval/execute", {"ticket": ticket}
        )
        return status, body

    def _start_refresh(self) -> tuple[int, Any]:
        status, _, body = self._request("POST", "/api/v1/dashboard/refresh", {})
        return status, body

    def _wait_refresh_job(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, _, body = self._request(
                "GET", f"/api/v1/dashboard/refresh/jobs/{job_id}"
            )
            self.assertEqual(200, status, body)
            job = body["job"]
            if job["status"] in {"succeeded", "failed"}:
                return job
            time.sleep(0.01)
        self.fail("本机数据刷新任务没有在离线测试时限内完成")

    def _wait_job(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, _, body = self._request(
                "GET", f"/api/v1/approval/jobs/{job_id}"
            )
            self.assertEqual(200, status, body)
            job = body["job"]
            if job["status"] in {"succeeded", "failed"}:
                return job
            time.sleep(0.01)
        self.fail("后台审批任务没有在离线测试时限内完成")

    def test_binds_only_ipv4_loopback_and_injects_runtime_config(self) -> None:
        self.assertEqual("127.0.0.1", self.server.server_address[0])
        status, headers, html = self._request("GET", "/", secure=False)
        self.assertEqual(200, status)
        self.assertIn('id="srdpm-local-service-config"', html)
        self.assertIn(self.server.service.csrf_token, html)
        self.assertIn(self.server.service.instance_id, html)
        self.assertIn(SERVICE_BUILD_ID, html)
        self.assertRegex(SERVICE_BUILD_ID, r"^[0-9a-f]{16}$")
        self.assertIn("default-src 'self'", headers["content-security-policy"])
        self.assertEqual("DENY", headers["x-frame-options"])
        self.assertEqual("nosniff", headers["x-content-type-options"])
        self.assertNotIn("access-control-allow-origin", headers)

    def test_host_origin_fetch_site_and_csrf_are_all_enforced(self) -> None:
        payload = {"month": "2026-07", "group_keys": [self.group_a]}
        cases = [
            {"Host": "evil.example"},
            {"Origin": "null"},
            {"Origin": "http://evil.example"},
            {"Sec-Fetch-Site": "cross-site"},
            {"X-SRDPM-CSRF": "wrong"},
        ]
        for override in cases:
            with self.subTest(override=override):
                status, headers, _ = self._request(
                    "POST", "/api/v1/approval/prepare", payload, headers=override
                )
                self.assertEqual(403, status)
                self.assertNotIn("access-control-allow-origin", headers)
        self.assertEqual([], self.clients)

    def test_credentials_are_validated_read_only_then_saved_without_echo(self) -> None:
        status, _, body = self._request("GET", "/api/v1/credentials/status")
        self.assertEqual(200, status)
        self.assertEqual(
            {"configured": False, "source": None}, body["credentials"]
        )

        username = "offline-user"
        password = "offline-password-never-echo"
        status, _, body = self._request(
            "POST",
            "/api/v1/credentials/configure",
            {"username": username, "password": password},
        )
        self.assertEqual(200, status, body)
        self.assertEqual(
            {"configured": True, "source": "windows_credential_manager"},
            body["credentials"],
        )
        self.assertNotIn(username, json.dumps(body, ensure_ascii=False))
        self.assertNotIn(password, json.dumps(body, ensure_ascii=False))
        self.assertEqual([(username, password)], self.credential_validation_inputs)
        self.assertEqual([(username, password)], self.credential_store.save_calls)
        self.assertEqual(1, self.credential_clients[0].check_live_calls)
        self.assertTrue(self.credential_clients[0].closed)

        status, _, body = self._request("GET", "/api/v1/credentials/status")
        self.assertEqual(200, status)
        self.assertTrue(body["credentials"]["configured"])

    def test_failed_credential_validation_never_saves_or_echoes_secret(self) -> None:
        password = "invalid-password-never-echo"

        class RejectingClient(FakeClient):
            def check_live(self) -> bool:
                self.check_live_calls += 1
                raise RuntimeError("offline rejected login")

        self.server.service.credential_client_factory = (
            lambda username, supplied: RejectingClient(pending={}, approved={})
        )
        status, _, body = self._request(
            "POST",
            "/api/v1/credentials/configure",
            {"username": "bad-user", "password": password},
        )

        self.assertEqual(401, status)
        rendered = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("bad-user", rendered)
        self.assertNotIn(password, rendered)
        self.assertEqual([], self.credential_store.save_calls)

    def test_options_is_405_without_cors_headers(self) -> None:
        status, headers, body = self._request("OPTIONS", "/api/v1/approval/prepare")
        self.assertEqual(405, status)
        self.assertEqual("method_not_allowed", body["error"]["code"])
        self.assertFalse(any(key.startswith("access-control-allow") for key in headers))

    def test_json_content_length_and_transfer_encoding_are_strict(self) -> None:
        status, _, body = self._request(
            "POST",
            "/api/v1/approval/prepare",
            "not-json-object",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(415, status)
        self.assertEqual("json_required", body["error"]["code"])

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        connection.putrequest("POST", "/api/v1/approval/prepare", skip_host=True)
        connection.putheader("Host", self.host)
        connection.putheader("Origin", self.server.origin)
        connection.putheader("Sec-Fetch-Site", "same-origin")
        connection.putheader("X-SRDPM-CSRF", self.server.service.csrf_token)
        connection.putheader("Content-Type", "application/json")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(411, response.status)
        response.read()
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        connection.putrequest("POST", "/api/v1/approval/prepare", skip_host=True)
        connection.putheader("Host", self.host)
        connection.putheader("Origin", self.server.origin)
        connection.putheader("Sec-Fetch-Site", "same-origin")
        connection.putheader("X-SRDPM-CSRF", self.server.service.csrf_token)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(400, response.status)
        response.read()
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        connection.putrequest("POST", "/api/v1/approval/prepare", skip_host=True)
        connection.putheader("Host", self.host)
        connection.putheader("Origin", self.server.origin)
        connection.putheader("Sec-Fetch-Site", "same-origin")
        connection.putheader("X-SRDPM-CSRF", self.server.service.csrf_token)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(MAX_HTTP_BODY_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(413, response.status)
        response.read()
        connection.close()

    def test_browser_cannot_supply_people_or_approval_ids(self) -> None:
        status, _, body = self._request(
            "POST",
            "/api/v1/approval/prepare",
            {
                "month": "2026-07",
                "group_keys": [self.group_a],
                "approve_ids": ["9999"],
            },
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", body["error"]["code"])

        prepared = self._prepare([self.group_a])
        self.assertEqual(1, prepared["summary"]["id_count"])
        self.assertEqual(1, prepared["summary"]["selection_count"])
        self.assertEqual("测试人员A", prepared["groups"][0]["person"])
        self.assertEqual(1, prepared["groups"][0]["item_count"])
        self.assertEqual(4.0, prepared["groups"][0]["work_hours"])
        self.assertEqual(["G/TEST"], prepared["groups"][0]["projects"])
        self.assertEqual(1, prepared["groups"][0]["project_count"])
        self.assertTrue(prepared["groups"][0]["review_summary"])
        self.assertEqual([], self.clients)

    def test_exact_selection_submits_only_its_id_when_same_day_has_other_pending_ids(self) -> None:
        """A selected row must not expand to every pending item on that day."""

        prepared = self._prepare([self.group_a])
        self.assertEqual(1, prepared["summary"]["id_count"])
        self.assertEqual(1, prepared["summary"]["selection_count"])
        status, body = self._execute(prepared["ticket"])
        self.assertEqual(202, status)
        job = self._wait_job(body["job"]["job_id"])

        self.assertEqual("succeeded", job["outcome"])
        self.assertEqual([("1001",)], self.clients[0].approve_calls)
        self.assertNotIn("1002", self.clients[0].approve_calls[0])

    def test_project_mismatch_row_never_expands_to_same_day_normal_rows(self) -> None:
        audit = _minimal_audit()
        audit["project_mismatch"] = [
            {
                "date": "2026-07-08",
                "person": "测试人员A",
                "items": "G/TEST",
                "title": "A1",
                "content": "离线测试内容",
                "work_hours": 4,
                "allowed_chips": [],
                "reason": "fixture project mismatch",
            }
        ]
        (self.month_dir / "audit_report.json").write_text(
            json.dumps(audit, ensure_ascii=False), encoding="utf-8"
        )

        prepared = self._prepare([self.group_a])
        self.assertEqual("manual", prepared["groups"][0]["review_mode"])
        self.assertEqual(1, prepared["groups"][0]["id_count"])
        status, body = self._execute(prepared["ticket"])
        self.assertEqual(202, status)
        self._wait_job(body["job"]["job_id"])
        self.assertEqual([("1001",)], self.clients[0].approve_calls)

    def test_ticket_expiration_and_reuse_never_duplicate_execution(self) -> None:
        expired = self._prepare([self.group_a])
        self.clock.advance(121)
        status, body = self._execute(expired["ticket"])
        self.assertEqual(410, status)
        self.assertEqual("ticket_expired", body["error"]["code"])
        self.assertEqual([], self.clients)

        fresh = self._prepare([self.group_a])
        status, body = self._execute(fresh["ticket"])
        self.assertEqual(202, status)
        job_id = body["job"]["job_id"]
        second_status, second_body = self._execute(fresh["ticket"])
        self.assertEqual(409, second_status)
        self.assertEqual("ticket_used", second_body["error"]["code"])
        job = self._wait_job(job_id)
        self.assertEqual("succeeded", job["status"])
        self.assertEqual(1, len(self.clients))
        self.assertEqual(1, len(self.clients[0].approve_calls))

    def test_native_confirmation_is_required_and_rejection_consumes_ticket(self) -> None:
        prepared = self._prepare([self.group_a])
        self.server.service.approval_confirmer = lambda summary: False

        status, body = self._execute(prepared["ticket"])

        self.assertEqual(409, status)
        self.assertEqual("native_confirmation_rejected", body["error"]["code"])
        self.assertEqual([], self.clients)
        second_status, second_body = self._execute(prepared["ticket"])
        self.assertEqual(409, second_status)
        self.assertEqual("ticket_used", second_body["error"]["code"])

    def test_success_rechecks_and_only_marks_readback_success_verified(self) -> None:
        prepared = self._prepare([self.group_a, self.group_b])
        status, body = self._execute(prepared["ticket"])
        self.assertEqual(202, status)
        job = self._wait_job(body["job"]["job_id"])
        self.assertEqual("succeeded", job["status"])
        self.assertEqual("succeeded", job["outcome"])
        self.assertEqual(
            ["verified_approved", "verified_approved"],
            [group["state"] for group in job["groups"]],
        )
        client = self.clients[0]
        self.assertEqual(4, len(client.pending_calls))
        self.assertEqual(2, len(client.approve_calls))
        self.assertEqual(2, len(client.status_calls))
        self.assertEqual([None, "u-b", None, "u-b"], client.pending_user_ids)
        self.assertEqual([None, "u-b"], client.status_user_ids)
        self.assertTrue(client.closed)
        self.assertEqual(1, len(self.native_confirmation_summaries))
        native_summary = self.native_confirmation_summaries[0]
        self.assertEqual(2, native_summary.group_count)
        self.assertEqual(prepared["summary"]["sha256"], native_summary.sha256)

        ledger = json.loads(
            (self.month_dir / "approval_readback.json").read_text(encoding="utf-8")
        )
        self.assertEqual({"1001", "2001"}, set(ledger["entries"]))
        self.assertEqual("succeeded", job["local_persistence"])
        with self.assertRaises(ServiceError) as context:
            rebuild_plan_from_archive(self.archive, "2026-07", [self.group_a])
        self.assertEqual("group_already_approved", context.exception.code)

    def test_background_job_uses_stored_windows_credentials(self) -> None:
        self.credential_store.credentials = Credentials(
            "stored-offline-user", "stored-offline-password"
        )
        self.server.service.client_factory = (
            self.server.service._client_from_configured_credentials
        )
        prepared = self._prepare([self.group_a])
        status, body = self._execute(prepared["ticket"])
        self.assertEqual(202, status)
        job = self._wait_job(body["job"]["job_id"])

        self.assertEqual("succeeded", job["outcome"])
        self.assertEqual(
            [("stored-offline-user", "stored-offline-password")],
            self.credential_validation_inputs,
        )
        self.assertEqual(1, len(self.credential_clients[0].approve_calls))
        self.assertTrue(self.credential_clients[0].closed)

    def test_real_service_wiring_rebuilds_html_with_persisted_approved_status(self) -> None:
        (self.root / "project_mapping.json").write_text("{}", encoding="utf-8")
        (self.root / "chip_history.json").write_text(
            '{"schema_version":1,"chips":[]}', encoding="utf-8"
        )
        self.server.service.dashboard_rebuilder = (
            self.server.service._rebuild_local_dashboard
        )
        prepared = self._prepare([self.group_a])
        status, body = self._execute(prepared["ticket"])
        self.assertEqual(202, status)
        job = self._wait_job(body["job"]["job_id"])

        self.assertEqual("succeeded", job["local_persistence"])
        status, _headers, html = self._request("GET", "/", secure=False)
        self.assertEqual(200, status)
        match = re.search(
            r"const ALL_DATA = (.*?);\s*const MONTH_SELECTION_KEY",
            html,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        embedded = json.loads(match.group(1))
        self.assertEqual(
            "approved",
            embedded["2026-07"]["approval_groups"][self.group_a]["status"],
        )

    def test_verified_remote_result_is_not_downgraded_when_local_store_fails(self) -> None:
        def fail_write(_month_dir, _month, _entries):
            raise OSError("offline local persistence failure")

        self.server.service.readback_writer = fail_write
        prepared = self._prepare([self.group_a])
        status, body = self._execute(prepared["ticket"])
        self.assertEqual(202, status)
        job = self._wait_job(body["job"]["job_id"])

        self.assertEqual("succeeded", job["outcome"])
        self.assertEqual("verified_approved", job["groups"][0]["state"])
        self.assertEqual("failed", job["local_persistence"])
        self.assertIn("本地审批状态保存失败", job["message"])
        self.assertFalse((self.month_dir / "approval_readback.json").exists())
        self.assertEqual([], self.dashboard_rebuild_calls)

    def test_ledger_remains_authoritative_when_derived_dashboard_rebuild_fails(self) -> None:
        def fail_rebuild(_project_dir):
            raise RuntimeError("offline dashboard build failure")

        self.server.service.dashboard_rebuilder = fail_rebuild
        prepared = self._prepare([self.group_a])
        status, body = self._execute(prepared["ticket"])
        self.assertEqual(202, status)
        job = self._wait_job(body["job"]["job_id"])

        self.assertEqual("succeeded", job["outcome"])
        self.assertEqual("verified_approved", job["groups"][0]["state"])
        self.assertEqual("ledger_saved", job["local_persistence"])
        self.assertIn("看板重建未完成", job["message"])
        ledger = json.loads(
            (self.month_dir / "approval_readback.json").read_text(encoding="utf-8")
        )
        self.assertEqual({"1001"}, set(ledger["entries"]))

        os.utime(self.root / DASHBOARD_NAME, (1, 1))
        heal_calls = []

        def heal_dashboard(project_dir):
            heal_calls.append(Path(project_dir))
            (Path(project_dir) / DASHBOARD_NAME).write_text(
                "<!doctype html><html><head><title>healed</title></head><body>healed</body></html>",
                encoding="utf-8",
            )

        self.server.service.dashboard_rebuilder = heal_dashboard
        status, _headers, html = self._request("GET", "/", secure=False)
        self.assertEqual(200, status)
        self.assertIn("healed", html)
        self.assertEqual([self.root], heal_calls)

    def test_second_group_preflight_drift_maps_to_its_stable_group_key(self) -> None:
        self.client_builder = lambda: FakeClient(
            pending={
                ("测试人员A", "2026-07-08"): ["1001", "1002"],
                ("测试人员B", "2026-07-09"): ["9999"],
            },
            approved={},
        )
        prepared = self._prepare([self.group_a, self.group_b])
        status, body = self._execute(prepared["ticket"])
        self.assertEqual(202, status)
        job = self._wait_job(body["job"]["job_id"])
        by_key = {group["group_key"]: group for group in job["groups"]}
        self.assertEqual("not_attempted", by_key[self.group_a]["state"])
        self.assertEqual("not_attempted", by_key[self.group_b]["state"])
        self.assertIn("提交前校验失败", by_key[self.group_b]["message"])
        self.assertEqual("尚未提交", by_key[self.group_a]["message"])
        self.assertEqual([], self.clients[0].approve_calls)

    def test_drift_is_not_attempted_and_stops_every_submission(self) -> None:
        self.client_builder = lambda: FakeClient(
            pending={
                ("测试人员A", "2026-07-08"): ["9999"],
                ("测试人员B", "2026-07-09"): ["2001"],
            },
            approved={},
        )
        prepared = self._prepare([self.group_a, self.group_b])
        status, body = self._execute(prepared["ticket"])
        self.assertEqual(202, status)
        job = self._wait_job(body["job"]["job_id"])
        self.assertEqual("failed", job["status"])
        self.assertEqual("rejected_no_change", job["outcome"])
        self.assertTrue(all(group["state"] == "not_attempted" for group in job["groups"]))
        self.assertEqual([], self.clients[0].approve_calls)
        self.assertTrue(self.clients[0].closed)

    def test_partial_success_is_structured_and_later_groups_are_not_attempted(self) -> None:
        self.client_builder = lambda: FakeClient(
            pending={
                ("测试人员A", "2026-07-08"): ["1001", "1002"],
                ("测试人员B", "2026-07-09"): ["2001"],
                ("测试人员C", "2026-07-10"): ["3001"],
            },
            approved={
                ("测试人员A", "2026-07-08"): {"1001": "通过", "1002": "通过"}
            },
            fail_on_ids=["2001"],
        )
        prepared = self._prepare([self.group_a, self.group_b, self.group_c])
        status, body = self._execute(prepared["ticket"])
        self.assertEqual(202, status)
        job = self._wait_job(body["job"]["job_id"])
        self.assertEqual("failed", job["status"])
        self.assertEqual("partial_success", job["outcome"])
        by_key = {group["group_key"]: group for group in job["groups"]}
        self.assertEqual("verified_approved", by_key[self.group_a]["state"])
        self.assertEqual("unknown", by_key[self.group_b]["state"])
        self.assertEqual("not_attempted", by_key[self.group_c]["state"])
        self.assertEqual(2, len(self.clients[0].approve_calls))
        self.assertTrue(self.clients[0].closed)

        ledger = json.loads(
            (self.month_dir / "approval_readback.json").read_text(encoding="utf-8")
        )
        self.assertEqual({"1001"}, set(ledger["entries"]))
        self.assertEqual("succeeded", job["local_persistence"])

    def test_archive_change_after_prepare_fails_before_client_creation(self) -> None:
        prepared = self._prepare([self.group_a])
        self.raw_data["daily_data"]["2026-07-08"]["list"][0]["children"][0][
            "approve_id"
        ] = "1003"
        self._write_raw()
        status, body = self._execute(prepared["ticket"])
        self.assertEqual(409, status)
        self.assertEqual("group_not_allowed", body["error"]["code"])
        self.assertEqual([], self.native_confirmation_summaries)
        self.assertEqual([], self.clients)

    def test_client_configuration_failure_is_rejected_without_unknown_state(self) -> None:
        def broken_builder() -> FakeClient:
            raise RuntimeError("offline missing credentials")

        self.client_builder = broken_builder
        prepared = self._prepare([self.group_a])
        status, body = self._execute(prepared["ticket"])
        self.assertEqual(202, status)
        job = self._wait_job(body["job"]["job_id"])

        self.assertEqual("failed", job["status"])
        self.assertEqual("rejected_no_change", job["outcome"])
        self.assertIn("未联网、未提交审批", job["message"])
        self.assertEqual([], self.clients)

    def test_selection_group_limit_is_enforced_before_archive_lookup(self) -> None:
        keys = [f"grp_{index:020x}" for index in range(201)]
        with self.assertRaises(ServiceError) as context:
            rebuild_plan_from_archive(self.archive, "2026-07", keys)
        self.assertEqual(413, context.exception.status)
        self.assertEqual("too_many_groups", context.exception.code)

    def test_prepare_is_rejected_while_refresh_lock_is_present(self) -> None:
        lock_path = self.root / ".srdpm-refresh.lock"
        lock_path.write_text('{"schema_version":1}', encoding="utf-8")
        try:
            status, _, body = self._request(
                "POST",
                "/api/v1/approval/prepare",
                {"month": "2026-07", "group_keys": [self.group_a]},
            )
        finally:
            lock_path.unlink(missing_ok=True)

        self.assertEqual(409, status)
        self.assertEqual("refresh_in_progress", body["error"]["code"])
        self.assertEqual([], self.clients)

    def test_dashboard_refresh_is_pollable_read_only_job(self) -> None:
        status, body = self._start_refresh()

        self.assertEqual(202, status, body)
        job_id = body["job"]["job_id"]
        job = self._wait_refresh_job(job_id)
        self.assertEqual("succeeded", job["status"])
        self.assertEqual("2026-07", job["updated_month"])
        self.assertEqual(["2026-06", "2026-07"], job["updated_months"])
        self.assertTrue(job["mapping_updated"])
        self.assertIn("允许机芯已核对", job["message"])
        self.assertEqual([self.root], self.refresh_calls)
        self.assertEqual([], self.clients)
        self.assertEqual([], self.native_confirmation_summaries)
        self.assertEqual([], self.credential_validation_inputs)

    def test_dashboard_refresh_requires_exact_empty_body_and_same_origin(self) -> None:
        status, _, body = self._request(
            "POST", "/api/v1/dashboard/refresh", {"month": "2026-07"}
        )
        self.assertEqual(400, status, body)
        self.assertEqual("invalid_request", body["error"]["code"])

        status, _, body = self._request(
            "POST",
            "/api/v1/dashboard/refresh",
            {},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(403, status, body)
        self.assertEqual("invalid_origin", body["error"]["code"])
        self.assertEqual([], self.refresh_calls)

    def test_dashboard_refresh_blocks_new_or_prepared_approval_execution(self) -> None:
        prepared = self._prepare([self.group_a])
        self.release_refresh.clear()
        try:
            status, body = self._start_refresh()
            self.assertEqual(202, status, body)
            self.assertTrue(self.refresh_started.wait(timeout=1))

            status, _, duplicate_body = self._request("POST", "/api/v1/dashboard/refresh", {})
            self.assertEqual(202, status, duplicate_body)
            self.assertEqual(body["job"]["job_id"], duplicate_body["job"]["job_id"])
            self.assertEqual("running", duplicate_body["job"]["status"])

            status, _, body = self._request(
                "POST",
                "/api/v1/approval/prepare",
                {"month": "2026-07", "group_keys": [self.group_a]},
            )
            self.assertEqual(409, status, body)
            self.assertEqual("refresh_in_progress", body["error"]["code"])

            status, body = self._execute(prepared["ticket"])
            self.assertEqual(409, status, body)
            self.assertEqual("refresh_in_progress", body["error"]["code"])
            self.assertEqual([], self.clients)
        finally:
            self.release_refresh.set()

    def test_dashboard_refresh_is_rejected_while_approval_execution_is_reserved(self) -> None:
        self.assertTrue(self.server.service._execution_lock.acquire(blocking=False))
        try:
            status, body = self._start_refresh()
        finally:
            self.server.service._execution_lock.release()

        self.assertEqual(409, status, body)
        self.assertEqual("approval_in_progress", body["error"]["code"])
        self.assertEqual([], self.refresh_calls)

    def test_dashboard_refresh_failure_does_not_echo_internal_error(self) -> None:
        internal_error = "offline-refresh-internal-detail"
        self.refresh_error = RuntimeError(internal_error)
        status, body = self._start_refresh()

        self.assertEqual(202, status, body)
        job = self._wait_refresh_job(body["job"]["job_id"])
        self.assertEqual("failed", job["status"])
        self.assertIn("数据刷新失败", job["message"])
        self.assertNotIn(internal_error, job["message"])
        self.assertEqual([], self.clients)

    def test_dashboard_wiki_mapping_failure_has_safe_specific_message(self) -> None:
        from refresh_dashboard import RefreshMappingError

        internal_error = "secret-bearing-offline-wiki-detail"
        self.refresh_error = RefreshMappingError(internal_error)
        status, body = self._start_refresh()

        self.assertEqual(202, status, body)
        job = self._wait_refresh_job(body["job"]["job_id"])
        self.assertEqual("failed", job["status"])
        self.assertIn("Wiki 最新项目负荷附件检查失败", job["message"])
        self.assertNotIn(internal_error, job["message"])


if __name__ == "__main__":
    unittest.main()
