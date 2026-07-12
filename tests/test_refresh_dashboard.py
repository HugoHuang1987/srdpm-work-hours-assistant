import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import fetch_and_audit
import refresh_dashboard as refresh
from windows_credential_store import Credentials


class FakeLock:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class FakeCredentialStore:
    def __init__(self, credentials):
        self.credentials = credentials
        self.load_calls = 0
        self.before_load = None

    def load(self):
        self.load_calls += 1
        if callable(self.before_load):
            self.before_load()
        return self.credentials


class FakeSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeFetchModule:
    READ_ONLY_API_PATHS = refresh.READ_ONLY_FETCH_PATHS

    def __init__(self):
        self.OUT_DIR = "original-out"
        self.ARCHIVE_DIR = "original-archive"
        self.login_inputs = []
        self.fetch_inputs = []
        self.audit_inputs = []
        self.session = FakeSession()
        self.fail_fetch = False
        self.fail_audit = False

    def login_srdpm(self, username, password):
        self.login_inputs.append((username, password))
        return self.session

    def fetch_month(self, session, year, month):
        self.fetch_inputs.append((session, year, month, self.OUT_DIR, self.ARCHIVE_DIR))
        if self.fail_fetch:
            raise RuntimeError("offline fetch failure")
        month_dir = Path(self.ARCHIVE_DIR) / f"{year:04d}-{month:02d}"
        month_dir.mkdir(parents=True, exist_ok=True)
        (month_dir / "raw_data.json").write_text(
            json.dumps(
                {
                    "users": [],
                    "daily_data": {},
                    "fetch_time": "2026-07-13T09:00:00",
                    "date_range": "2026-07-01 ~ 2026-07-31",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        # Existing fetch_and_audit.py writes compatibility copies.  They must stay
        # in staging and never be published to the project root by the scheduler.
        (Path(self.OUT_DIR) / "srdpm_daily_data_20260701_20260731.json").write_text(
            "staged-only", encoding="utf-8"
        )
        return {"month_dir": str(month_dir)}

    def run_audit(self, year, month, fetch_result):
        self.audit_inputs.append((year, month, fetch_result, self.OUT_DIR))
        if self.fail_audit:
            raise RuntimeError("offline audit failure")
        month_dir = Path(fetch_result["month_dir"])
        month_label = f"{year:04d}-{month:02d}"
        (month_dir / "audit_report.json").write_text(
            json.dumps({"month": month_label, "daily_summary": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        (month_dir / "audit_report.md").write_text("# staged audit\n", encoding="utf-8")
        (Path(self.OUT_DIR) / "审核报告_2026年7月.json").write_text(
            "staged-only", encoding="utf-8"
        )
        return {"month": month_label}


class FakeDashboardModule:
    def __init__(self):
        self.OUT_DIR = "original-out"
        self.ARCHIVE_DIR = "original-archive"
        self.OUTPUT_HTML = "original-output"
        self.calls = []
        self.fail = False

    def main(self):
        self.calls.append((self.OUT_DIR, self.ARCHIVE_DIR, self.OUTPUT_HTML))
        if self.fail:
            raise RuntimeError("offline dashboard failure")
        Path(self.OUTPUT_HTML).write_text(
            "<html><body>staged new dashboard</body></html>", encoding="utf-8"
        )


class RefreshDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        (self.project / "project_mapping.json").write_text("{}", encoding="utf-8")
        self.month = "2026-07"
        self.month_dir = self.project / "srdpm_archive" / self.month
        self.month_dir.mkdir(parents=True)
        (self.month_dir / "raw_data.json").write_text('{"old":"raw"}', encoding="utf-8")
        (self.month_dir / "audit_report.json").write_text(
            '{"old":"audit"}', encoding="utf-8"
        )
        (self.month_dir / "audit_report.md").write_text("old audit", encoding="utf-8")
        (self.project / refresh.DASHBOARD_NAME).write_text("<html>old</html>", encoding="utf-8")
        self.fetch = FakeFetchModule()
        self.dashboard = FakeDashboardModule()
        self.credentials = Credentials("offline-user", "offline-password")
        self.store = FakeCredentialStore(self.credentials)
        self.lock = FakeLock()
        self.execution_lock = FakeLock()
        self.now = datetime(2026, 7, 13, 9, 0, 0)

    def tearDown(self):
        self.temporary.cleanup()

    def _snapshot_published_files(self):
        paths = [
            self.month_dir / name for name in refresh.REQUIRED_MONTH_ARTIFACTS
        ] + [self.project / refresh.DASHBOARD_NAME]
        return {path: path.read_bytes() for path in paths}

    def _refresh(self, **overrides):
        values = {
            "project_dir": self.project,
            "now": self.now,
            "credential_store": self.store,
            "fetch_module": self.fetch,
            "dashboard_module": self.dashboard,
            "lock_factory": lambda: self.lock,
            "execution_lock_factory": lambda: self.execution_lock,
        }
        values.update(overrides)
        return refresh.refresh_current_month(**values)

    def test_success_uses_credential_manager_stages_then_publishes(self):
        original_publish = refresh._publish_staged_artifacts
        lock_payloads = []
        lock_observations = []

        original_login = self.fetch.login_srdpm
        original_fetch = self.fetch.fetch_month
        original_audit = self.fetch.run_audit
        original_dashboard = self.dashboard.main

        def observe(stage):
            lock_observations.append(
                (stage, (self.project / refresh.REFRESH_FILE_LOCK_NAME).is_file())
            )

        def observe_login(username, password):
            observe("login")
            return original_login(username, password)

        def observe_fetch(session, year, month):
            observe("fetch")
            return original_fetch(session, year, month)

        def observe_audit(year, month, fetch_result):
            observe("audit")
            return original_audit(year, month, fetch_result)

        def observe_dashboard():
            observe("dashboard")
            return original_dashboard()

        self.store.before_load = lambda: observe("credentials")
        self.fetch.login_srdpm = observe_login
        self.fetch.fetch_month = observe_fetch
        self.fetch.run_audit = observe_audit
        self.dashboard.main = observe_dashboard

        def inspect_publish(artifacts, stage_root):
            lock_path = self.project / refresh.REFRESH_FILE_LOCK_NAME
            self.assertTrue(lock_path.is_file())
            lock_payloads.append(json.loads(lock_path.read_text(encoding="utf-8")))
            return original_publish(artifacts, stage_root)

        with patch.object(refresh, "_publish_staged_artifacts", side_effect=inspect_publish):
            result = self._refresh()

        self.assertEqual(self.month, result.month)
        self.assertEqual(4, len(result.published_paths))
        self.assertEqual([("offline-user", "offline-password")], self.fetch.login_inputs)
        self.assertEqual(1, len(self.fetch.fetch_inputs))
        self.assertEqual(1, len(self.fetch.audit_inputs))
        self.assertTrue(self.fetch.session.closed)
        self.assertEqual("original-out", self.fetch.OUT_DIR)
        self.assertEqual("original-archive", self.fetch.ARCHIVE_DIR)
        self.assertEqual("original-out", self.dashboard.OUT_DIR)
        self.assertEqual("original-archive", self.dashboard.ARCHIVE_DIR)
        self.assertEqual("original-output", self.dashboard.OUTPUT_HTML)
        self.assertTrue(self.lock.released)
        self.assertTrue(self.execution_lock.released)
        self.assertFalse((self.project / refresh.REFRESH_FILE_LOCK_NAME).exists())
        self.assertEqual(
            [
                ("credentials", True),
                ("login", True),
                ("fetch", True),
                ("audit", True),
                ("dashboard", True),
            ],
            lock_observations,
        )
        self.assertEqual(1, len(lock_payloads))
        self.assertEqual({"schema_version", "pid", "created_at"}, set(lock_payloads[0]))
        self.assertNotIn("offline-password", json.dumps(lock_payloads[0]))
        self.assertIn('"daily_data": {}', (self.month_dir / "raw_data.json").read_text(encoding="utf-8"))
        self.assertIn('"month": "2026-07"', (self.month_dir / "audit_report.json").read_text(encoding="utf-8"))
        self.assertEqual("# staged audit\n", (self.month_dir / "audit_report.md").read_text(encoding="utf-8"))
        self.assertIn("staged new dashboard", (self.project / refresh.DASHBOARD_NAME).read_text(encoding="utf-8"))
        self.assertFalse((self.project / "srdpm_daily_data_20260701_20260731.json").exists())
        self.assertFalse((self.project / "审核报告_2026年7月.json").exists())

    def test_missing_credentials_does_not_fetch_or_modify_published_files(self):
        before = self._snapshot_published_files()
        missing_store = FakeCredentialStore(None)

        with self.assertRaises(refresh.RefreshCredentialError):
            self._refresh(credential_store=missing_store)

        self.assertEqual(before, self._snapshot_published_files())
        self.assertEqual([], self.fetch.login_inputs)
        self.assertEqual([], self.dashboard.calls)
        self.assertFalse((self.project / refresh.REFRESH_FILE_LOCK_NAME).exists())
        self.assertTrue(self.lock.released)

    def test_fetch_failure_keeps_old_archive_and_dashboard(self):
        before = self._snapshot_published_files()
        lock_seen = []

        def fail_while_locked(session, year, month):
            lock_seen.append((self.project / refresh.REFRESH_FILE_LOCK_NAME).is_file())
            raise RuntimeError("offline fetch failure")

        self.fetch.fetch_month = fail_while_locked

        with self.assertRaises(refresh.RefreshError):
            self._refresh()

        self.assertEqual(before, self._snapshot_published_files())
        self.assertEqual([], self.dashboard.calls)
        self.assertTrue(self.fetch.session.closed)
        self.assertFalse((self.project / refresh.REFRESH_FILE_LOCK_NAME).exists())
        self.assertEqual([True], lock_seen)
        self.assertTrue(self.execution_lock.released)

    def test_dashboard_failure_keeps_old_archive_and_dashboard(self):
        before = self._snapshot_published_files()
        self.dashboard.fail = True

        with self.assertRaises(refresh.RefreshError):
            self._refresh()

        self.assertEqual(before, self._snapshot_published_files())
        self.assertTrue(self.fetch.session.closed)
        self.assertFalse((self.project / refresh.REFRESH_FILE_LOCK_NAME).exists())

    def test_publish_failure_rolls_back_every_published_file(self):
        before = self._snapshot_published_files()
        original_replace = refresh._atomic_copy_replace

        def fail_on_audit(source, target):
            if target.name == "audit_report.json":
                raise OSError("offline replacement failure")
            return original_replace(source, target)

        with patch.object(refresh, "_atomic_copy_replace", side_effect=fail_on_audit):
            with self.assertRaises(refresh.RefreshPublishError):
                self._refresh()

        self.assertEqual(before, self._snapshot_published_files())
        self.assertFalse((self.project / refresh.REFRESH_FILE_LOCK_NAME).exists())

    def test_busy_lock_stops_before_credentials_or_network(self):
        before = self._snapshot_published_files()

        with self.assertRaises(refresh.RefreshBusyError):
            self._refresh(lock_factory=lambda: None)

        self.assertEqual(before, self._snapshot_published_files())
        self.assertEqual(0, self.store.load_calls)
        self.assertEqual([], self.fetch.login_inputs)
        self.assertFalse((self.project / refresh.REFRESH_FILE_LOCK_NAME).exists())

    def test_busy_approval_execution_lock_stops_before_credentials_or_network(self):
        before = self._snapshot_published_files()

        with self.assertRaises(refresh.RefreshBusyError):
            self._refresh(execution_lock_factory=lambda: None)

        self.assertEqual(before, self._snapshot_published_files())
        self.assertEqual(0, self.store.load_calls)
        self.assertEqual([], self.fetch.login_inputs)
        self.assertFalse((self.project / refresh.REFRESH_FILE_LOCK_NAME).exists())
        self.assertTrue(self.lock.released)

    def test_current_month_uses_local_calendar_month(self):
        self.assertEqual((2027, 1, "2027-01"), refresh.current_month(datetime(2027, 1, 1, 0, 0)))
        self.assertEqual((2026, 12, "2026-12"), refresh.current_month(datetime(2026, 12, 31, 23, 59)))

    def test_fetch_api_allowlist_rejects_mutating_path_without_request(self):
        class Session:
            def __init__(self):
                self.calls = []

            def post(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                raise AssertionError("must not make a request")

        session = Session()
        with self.assertRaises(ValueError):
            fetch_and_audit.api_call(session, "approval", {"approve_id": "offline"})
        self.assertEqual([], session.calls)

    def test_fetch_api_failure_is_not_converted_into_partial_data(self):
        class Response:
            def json(self):
                return {"code": 500, "data": {}}

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return Response()

        session = Session()
        with self.assertRaises(RuntimeError):
            fetch_and_audit.api_call(session, "list", {"page": 1})
        self.assertEqual(1, len(session.calls))

    def test_scheduler_management_contract_is_refresh_only(self):
        script = (Path(__file__).resolve().parents[1] / "manage_weekly_refresh.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("New-ScheduledTaskTrigger", script)
        self.assertIn("-Weekly", script)
        self.assertIn("Monday", script)
        self.assertIn("AddHours(9)", script)
        self.assertIn("-LogonType Interactive", script)
        self.assertIn("-RunLevel Limited", script)
        self.assertIn("Limited 表示最低权限", script)
        self.assertIn("-StartWhenAvailable", script)
        self.assertIn("-AllowStartIfOnBatteries", script)
        self.assertIn("-DontStopIfGoingOnBatteries", script)
        self.assertIn("-MultipleInstances IgnoreNew", script)
        self.assertIn("refresh_dashboard.py", script)
        self.assertNotIn("Start-ScheduledTask", script)
        self.assertNotIn("apply_approval_plan.py", script)
        self.assertNotIn("local_approval_server.py", script)
        self.assertNotIn("--execute", script)

    def test_refresh_entrypoint_never_reads_environment_credentials(self):
        script = (Path(__file__).resolve().parents[1] / "refresh_dashboard.py").read_text(encoding="utf-8")
        self.assertNotIn("SRDPM_USERNAME", script)
        self.assertNotIn("SRDPM_PASSWORD", script)
        self.assertIn("from apply_approval_plan import _try_acquire_execution_lock", script)
        self.assertIn("execution_lock_factory", script)
        self.assertNotIn("execute_plan(", script)
        self.assertNotIn("local_approval_server", script)


if __name__ == "__main__":
    unittest.main()
