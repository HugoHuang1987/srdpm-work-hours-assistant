import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
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


class FakeMappingRefresher:
    def __init__(self):
        self.calls = []
        self.before_refresh = None
        self.fail = False
        self.attachment_filename = "团队成员项目负荷_新拆分-20260720.xlsx"
        self.payload = {
            "schema_version": 3,
            "source": {
                "kind": "confluence_attachment",
                "page_id": "22824730",
                "attachment_id": "132360786",
                "filename": self.attachment_filename,
                "updated_at": "2026-07-20T17:06:07+08:00",
                "sha256": "a" * 64,
                "sheet": "计算负荷用-2026-7月",
            },
            "mapping": [
                {
                    "row": 2,
                    "customer": "G",
                    "project": "离线测试项目",
                    "chip": "AM963D5",
                    "spm": "测试人员",
                    "bsp": "",
                    "diag": "",
                }
            ],
            "person_projects": {"测试人员": ["G/AM963D5"]},
            "all_people": ["测试人员"],
            "all_chips": ["AM963D5"],
            "authorization_retention": {
                "policy_version": 1,
                "grace_months": 2,
                "month_semantics": "removal_month_plus_two_full_calendar_months",
                "records": [
                    {
                        "person": "旧测试人员",
                        "chip": "MT9612",
                        "canonical_chip": "MT9612",
                        "removed_month": "2026-07",
                        "valid_through_month": "2026-09",
                        "last_present_source": {"kind": "offline_fixture"},
                        "removed_by_source": {"kind": "offline_fixture"},
                    }
                ],
            },
        }

    def __call__(self, mapping_path):
        mapping_path = Path(mapping_path)
        self.calls.append(mapping_path)
        if callable(self.before_refresh):
            self.before_refresh()
        if self.fail:
            raise RuntimeError("offline wiki mapping failure")
        mapping_path.write_text(
            json.dumps(self.payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            updated=True,
            attachment_filename=self.attachment_filename,
        )


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
        self.return_none_audit = False

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
        if self.return_none_audit:
            return None
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
        self.readback_snapshots = {}
        self.fail = False

    def main(self):
        self.calls.append((self.OUT_DIR, self.ARCHIVE_DIR, self.OUTPUT_HTML))
        self.readback_snapshots = {
            path.parent.name: path.read_bytes()
            for path in Path(self.ARCHIVE_DIR).glob("*/approval_readback.json")
        }
        if self.fail:
            raise RuntimeError("offline dashboard failure")
        Path(self.OUTPUT_HTML).write_text(
            "<html><body>staged new dashboard</body></html>", encoding="utf-8"
        )


class RefreshDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        self.old_mapping = {
            "schema_version": 3,
            "source": {
                "kind": "confluence_attachment",
                "page_id": "22824730",
                "attachment_id": "120000001",
                "filename": "团队成员项目负荷_新拆分-20260703.xlsx",
                "updated_at": "2026-07-03T10:00:00+08:00",
                "sha256": "b" * 64,
                "sheet": "计算负荷用-2026-7月",
            },
            "mapping": [
                {
                    "row": 2,
                    "customer": "A",
                    "project": "旧项目",
                    "chip": "MT9612",
                    "spm": "旧测试人员",
                    "bsp": "",
                    "diag": "",
                }
            ],
            "person_projects": {"旧测试人员": ["A/MT9612"]},
            "all_people": ["旧测试人员"],
            "all_chips": ["MT9612"],
            "authorization_retention": {
                "policy_version": 1,
                "grace_months": 2,
                "month_semantics": "removal_month_plus_two_full_calendar_months",
                "records": [],
            },
        }
        (self.project / refresh.MAPPING_NAME).write_text(
            json.dumps(self.old_mapping, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.project / refresh.CHIP_HISTORY_NAME).write_text(
            '{"schema_version":1,"chips":["T963D4Z"]}', encoding="utf-8"
        )
        self.month = "2026-07"
        self.previous_month = "2026-06"
        self.month_dir = self.project / "srdpm_archive" / self.month
        self.previous_month_dir = self.project / "srdpm_archive" / self.previous_month
        for month_dir, label in (
            (self.previous_month_dir, self.previous_month),
            (self.month_dir, self.month),
        ):
            month_dir.mkdir(parents=True)
            (month_dir / "raw_data.json").write_text(
                json.dumps({"old": f"raw-{label}"}), encoding="utf-8"
            )
            (month_dir / "audit_report.json").write_text(
                json.dumps({"old": f"audit-{label}"}), encoding="utf-8"
            )
            (month_dir / "audit_report.md").write_text(
                f"old audit {label}", encoding="utf-8"
            )
        (self.project / refresh.DASHBOARD_NAME).write_text("<html>old</html>", encoding="utf-8")
        self.mapping_refresher = FakeMappingRefresher()
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
            month_dir / name
            for month_dir in (self.previous_month_dir, self.month_dir)
            for name in refresh.REQUIRED_MONTH_ARTIFACTS
        ] + [
            self.project / refresh.MAPPING_NAME,
            self.project / refresh.CHIP_HISTORY_NAME,
            self.project / refresh.DASHBOARD_NAME,
        ]
        return {path: path.read_bytes() for path in paths}

    def _refresh(self, **overrides):
        values = {
            "project_dir": self.project,
            "now": self.now,
            "credential_store": self.store,
            "fetch_module": self.fetch,
            "dashboard_module": self.dashboard,
            "mapping_refresher": self.mapping_refresher,
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
        self.mapping_refresher.before_refresh = lambda: observe("mapping")
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
        self.assertEqual((self.previous_month, self.month), result.refreshed_months)
        self.assertTrue(result.mapping_updated)
        self.assertEqual(9, len(result.published_paths))
        self.assertIn(self.project / refresh.MAPPING_NAME, result.published_paths)
        self.assertEqual(
            [("offline-user", "offline-password"), ("offline-user", "offline-password")],
            self.fetch.login_inputs,
        )
        self.assertEqual(2, len(self.fetch.fetch_inputs))
        self.assertEqual(2, len(self.fetch.audit_inputs))
        self.assertTrue(self.fetch.session.closed)
        self.assertEqual("original-out", self.fetch.OUT_DIR)
        self.assertEqual("original-archive", self.fetch.ARCHIVE_DIR)
        self.assertEqual("original-out", self.dashboard.OUT_DIR)
        self.assertEqual("original-archive", self.dashboard.ARCHIVE_DIR)
        self.assertEqual("original-output", self.dashboard.OUTPUT_HTML)
        self.assertTrue(self.lock.released)
        self.assertTrue(self.execution_lock.released)
        self.assertFalse((self.project / refresh.REFRESH_FILE_LOCK_NAME).exists())
        observed_stages = [stage for stage, _locked in lock_observations]
        self.assertTrue(all(locked for _stage, locked in lock_observations))
        self.assertIn("mapping", observed_stages)
        self.assertLess(observed_stages.index("mapping"), observed_stages.index("login"))
        self.assertEqual(
            ["login", "fetch", "audit", "login", "fetch", "audit", "dashboard"],
            [stage for stage in observed_stages if stage not in {"credentials", "mapping"}],
        )
        self.assertEqual(1, len(self.mapping_refresher.calls))
        staged_mapping = self.mapping_refresher.calls[0]
        self.assertEqual(refresh.MAPPING_NAME, staged_mapping.name)
        self.assertNotEqual(self.project / refresh.MAPPING_NAME, staged_mapping)
        self.assertTrue(staged_mapping.parent.name.startswith(".srdpm-refresh-stage-"))
        self.assertEqual(1, len(lock_payloads))
        self.assertEqual({"schema_version", "pid", "created_at"}, set(lock_payloads[0]))
        self.assertNotIn("offline-password", json.dumps(lock_payloads[0]))
        self.assertIn('"daily_data": {}', (self.month_dir / "raw_data.json").read_text(encoding="utf-8"))
        self.assertIn('"month": "2026-07"', (self.month_dir / "audit_report.json").read_text(encoding="utf-8"))
        self.assertEqual("# staged audit\n", (self.month_dir / "audit_report.md").read_text(encoding="utf-8"))
        self.assertIn(
            '"month": "2026-06"',
            (self.previous_month_dir / "audit_report.json").read_text(encoding="utf-8"),
        )
        published_mapping = json.loads(
            (self.project / refresh.MAPPING_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(
            self.mapping_refresher.payload["person_projects"],
            published_mapping["person_projects"],
        )
        self.assertEqual(
            self.mapping_refresher.payload["authorization_retention"],
            published_mapping["authorization_retention"],
        )
        self.assertIn("staged new dashboard", (self.project / refresh.DASHBOARD_NAME).read_text(encoding="utf-8"))
        self.assertFalse((self.project / "srdpm_daily_data_20260701_20260731.json").exists())
        self.assertFalse((self.project / "审核报告_2026年7月.json").exists())

    def test_full_refresh_preserves_monthly_approval_readback_ledger(self):
        ledger_path = self.month_dir / "approval_readback.json"
        ledger_bytes = (
            b'{"schema_version":1,"month":"2026-07","entries":{}}\n'
        )
        ledger_path.write_bytes(ledger_bytes)

        self._refresh()

        self.assertEqual(ledger_bytes, ledger_path.read_bytes())
        self.assertEqual(ledger_bytes, self.dashboard.readback_snapshots[self.month])

    def test_missing_credentials_does_not_fetch_or_modify_published_files(self):
        before = self._snapshot_published_files()
        missing_store = FakeCredentialStore(None)

        with self.assertRaises(refresh.RefreshCredentialError):
            self._refresh(credential_store=missing_store)

        self.assertEqual(before, self._snapshot_published_files())

    def test_missing_chip_history_stops_before_login(self):
        (self.project / refresh.CHIP_HISTORY_NAME).unlink()
        with self.assertRaisesRegex(refresh.RefreshError, "机芯历史库不存在"):
            self._refresh()
        self.assertEqual([], self.fetch.login_inputs)
        self.assertEqual([], self.fetch.fetch_inputs)
        self.assertEqual([], self.fetch.login_inputs)
        self.assertEqual([], self.dashboard.calls)
        self.assertFalse((self.project / refresh.REFRESH_FILE_LOCK_NAME).exists())
        self.assertTrue(self.lock.released)

    def test_wiki_mapping_failure_keeps_every_published_file(self):
        before = self._snapshot_published_files()
        self.mapping_refresher.fail = True

        with self.assertRaises(refresh.RefreshError):
            self._refresh()

        self.assertEqual(before, self._snapshot_published_files())
        self.assertEqual([], self.fetch.login_inputs)
        self.assertEqual([], self.fetch.fetch_inputs)
        self.assertEqual([], self.dashboard.calls)
        self.assertFalse((self.project / refresh.REFRESH_FILE_LOCK_NAME).exists())
        self.assertTrue(self.lock.released)
        self.assertTrue(self.execution_lock.released)

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

    def test_audit_returning_none_cannot_reuse_stale_staged_report(self):
        before = self._snapshot_published_files()
        self.fetch.return_none_audit = True

        with self.assertRaises(refresh.RefreshError):
            self._refresh()

        self.assertEqual(before, self._snapshot_published_files())
        self.assertEqual(1, len(self.fetch.login_inputs))
        self.assertEqual(1, len(self.fetch.fetch_inputs))
        self.assertEqual(1, len(self.fetch.audit_inputs))
        self.assertEqual([], self.dashboard.calls)
        self.assertTrue(self.fetch.session.closed)
        self.assertFalse((self.project / refresh.REFRESH_FILE_LOCK_NAME).exists())

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

        def fail_on_dashboard(source, target):
            if target.name == refresh.DASHBOARD_NAME:
                raise OSError("offline replacement failure")
            return original_replace(source, target)

        with patch.object(refresh, "_atomic_copy_replace", side_effect=fail_on_dashboard):
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

    def test_local_archive_rebuild_publishes_only_dashboard_under_both_locks(self):
        before = self._snapshot_published_files()

        output = refresh.rebuild_dashboard_from_local_archive(
            project_dir=self.project,
            dashboard_module=self.dashboard,
            lock_factory=lambda: self.lock,
            execution_lock_factory=lambda: self.execution_lock,
        )

        self.assertEqual(self.project / refresh.DASHBOARD_NAME, output)
        after = self._snapshot_published_files()
        self.assertNotEqual(before[self.project / refresh.DASHBOARD_NAME], after[output])
        for path, content in before.items():
            if path != self.project / refresh.DASHBOARD_NAME:
                self.assertEqual(content, after[path])
        self.assertIn("staged new dashboard", output.read_text(encoding="utf-8"))
        self.assertEqual(1, len(self.dashboard.calls))
        self.assertEqual(0, self.store.load_calls)
        self.assertEqual([], self.fetch.login_inputs)
        self.assertEqual([], self.mapping_refresher.calls)
        self.assertTrue(self.lock.released)
        self.assertTrue(self.execution_lock.released)
        self.assertFalse((self.project / refresh.REFRESH_FILE_LOCK_NAME).exists())

    def test_local_archive_rebuild_failure_keeps_old_dashboard(self):
        before = self._snapshot_published_files()
        self.dashboard.fail = True

        with self.assertRaises(refresh.RefreshError):
            refresh.rebuild_dashboard_from_local_archive(
                project_dir=self.project,
                dashboard_module=self.dashboard,
                lock_factory=lambda: self.lock,
                execution_lock_factory=lambda: self.execution_lock,
            )

        self.assertEqual(before, self._snapshot_published_files())
        self.assertTrue(self.lock.released)
        self.assertTrue(self.execution_lock.released)
        self.assertFalse((self.project / refresh.REFRESH_FILE_LOCK_NAME).exists())

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
        self.assertIn("AddHours(10)", script)
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
