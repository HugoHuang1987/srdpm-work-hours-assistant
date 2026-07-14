import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import build_multi_month_dashboard as dashboard
from apply_approval_plan import load_plan, summarize_plan
from playwright.sync_api import sync_playwright


class DashboardUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary_output = tempfile.TemporaryDirectory()
        cls._original_output_html = dashboard.OUTPUT_HTML
        dashboard.OUTPUT_HTML = Path(cls._temporary_output.name) / "dashboard-under-test.html"
        try:
            dashboard.main()
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch(headless=True)
            cls.dashboard_uri = Path(dashboard.OUTPUT_HTML).resolve().as_uri()
        except Exception as exc:  # pragma: no cover - depends on local browser install
            if hasattr(cls, "browser"):
                cls.browser.close()
            if hasattr(cls, "playwright"):
                cls.playwright.stop()
            dashboard.OUTPUT_HTML = cls._original_output_html
            cls._temporary_output.cleanup()
            raise unittest.SkipTest(f"Playwright Chromium 不可用：{exc}")

    @classmethod
    def tearDownClass(cls):
        try:
            if hasattr(cls, "browser"):
                cls.browser.close()
            if hasattr(cls, "playwright"):
                cls.playwright.stop()
        finally:
            dashboard.OUTPUT_HTML = cls._original_output_html
            cls._temporary_output.cleanup()

    def setUp(self):
        self.context = self.browser.new_context(accept_downloads=True)
        self.context.route("http://**", lambda route: route.abort())
        self.context.route("https://**", lambda route: route.abort())
        self.page = self.context.new_page()
        self.page_errors = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        self.page.goto(self.dashboard_uri, wait_until="domcontentloaded")
        self.page.wait_for_function(
            """() => {
                const element = document.querySelector('#pendingCount');
                return Boolean(element && element.textContent.trim());
            }""",
            timeout=10_000,
        )
        self.current_month = self.page.evaluate("currentMonth")

    def tearDown(self):
        self.context.close()

    def assert_no_page_errors(self):
        self.assertEqual(self.page_errors, [])

    def open_mock_local_service(
        self,
        job_factory,
        *,
        credential_configured=True,
        initial_fragment="",
        initial_groups=None,
        refresh_job_factory=None,
    ):
        requests = []
        expected = {
            "id_counts": {},
            "groups": {},
            "credential_configured": credential_configured,
        }
        for group in initial_groups or []:
            expected["id_counts"][group["group_key"]] = group["id_count"]
            expected["groups"][group["group_key"]] = group
        dashboard_html = Path(dashboard.OUTPUT_HTML).read_text(encoding="utf-8")
        config = {
            "api_base": "/api/v1",
            "csrf_header": "X-SRDPM-CSRF",
            "csrf_token": "test-csrf-token-with-at-least-32-characters",
            "instance_id": "test-instance",
        }
        served_html = dashboard_html.replace(
            "</head>",
            '<script id="srdpm-local-service-config" type="application/json">'
            + json.dumps(config)
            + "</script></head>",
            1,
        )

        def route_local_service(route):
            request = route.request
            path = request.url.split("127.0.0.1:8765", 1)[-1]
            if path in {"/", "/index.html"}:
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=served_html)
                return
            requests.append(
                {
                    "method": request.method,
                    "path": path,
                    "body": request.post_data_json if request.post_data else None,
                    "csrf": request.headers.get("x-srdpm-csrf"),
                }
            )
            if path == "/api/v1/credentials/status":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "credentials": {
                                "configured": expected["credential_configured"],
                                "source": "windows_credential_manager"
                                if expected["credential_configured"]
                                else None,
                            },
                        }
                    ),
                )
                return
            if path == "/api/v1/credentials/configure":
                expected["credential_configured"] = True
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "credentials": {
                                "configured": True,
                                "source": "windows_credential_manager",
                            },
                        }
                    ),
                )
                return
            if path == "/api/v1/approval/prepare":
                body = request.post_data_json
                keys = body["group_keys"]
                groups = [expected["groups"][key] for key in keys]
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "prepared": {
                                "ticket": "one-use-ticket",
                                "expires_at": "2099-01-01T00:00:00Z",
                                "expires_in_seconds": 120,
                                "summary": {
                                    "month": body["month"],
                                    "group_count": len(keys),
                                    "selection_count": len(keys),
                                    "id_count": sum(expected["id_counts"][key] for key in keys),
                                    "person_count": len(keys),
                                    "date_count": len(keys),
                                    "sha256": "test-hash",
                                },
                                "groups": groups,
                            },
                        }
                    ),
                )
                return
            if path == "/api/v1/approval/execute":
                route.fulfill(
                    status=202,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "job": {"job_id": "test-job-id-1234567890", "status": "queued"},
                        }
                    ),
                )
                return
            if path == "/api/v1/approval/jobs/test-job-id-1234567890":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"ok": True, "job": job_factory()}),
                )
                return
            if path == "/api/v1/dashboard/refresh":
                route.fulfill(
                    status=202,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "job": {
                                "job_id": "test-refresh-job-id-1234567890",
                                "status": "queued",
                            },
                        }
                    ),
                )
                return
            if path == "/api/v1/dashboard/refresh/jobs/test-refresh-job-id-1234567890":
                job = (
                    refresh_job_factory()
                    if refresh_job_factory is not None
                    else {
                        "job_id": "test-refresh-job-id-1234567890",
                        "status": "succeeded",
                        "updated_month": self.current_month,
                        "message": "done",
                    }
                )
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"ok": True, "job": job}),
                )
                return
            route.fulfill(status=404, content_type="application/json", body='{"ok":false}')

        self.context.route("http://127.0.0.1:8765/**", route_local_service)
        self.page.goto(
            f"http://127.0.0.1:8765/{initial_fragment}",
            wait_until="domcontentloaded",
        )
        self.page.wait_for_function(
            """() => {
                const element = document.querySelector('#pendingCount');
                return Boolean(element && element.textContent.trim());
            }""",
            timeout=10_000,
        )
        self.current_month = self.page.evaluate("currentMonth")
        self.assertEqual(
            self.page.locator("#approvalServiceStatus").inner_text(),
            "本机审批服务：已连接",
        )
        return requests, expected

    def test_initial_counts_use_exact_selectable_rows_and_unique_ids(self):
        expected = self.page.evaluate(
            """() => {
                const groups = Object.values(APPROVAL_GROUPS);
                const countOpen = mode => {
                    const open = groups.filter(group =>
                        group.review_mode === mode && getGroupStatus(group.group_key) !== 'approved'
                    );
                    return {
                        selections: open.length,
                        ids: open.reduce((sum, group) => sum + (group.approve_ids || []).length, 0)
                    };
                };
                return {
                    month: currentMonth,
                    manual: countOpen('manual'),
                    auto: countOpen('auto'),
                    platformRows: CAT_DATA.five.items.length,
                    normalRows: CAT_DATA.six.items.length,
                    totalRows: ALL_DATA[currentMonth].enhanced.total_count
                };
            }"""
        )
        pending = self.page.locator("#pendingCount").inner_text()
        self.assertEqual(
            pending,
            f"需人工审核：{expected['manual']['selections']}条选择/{expected['manual']['ids']}个待审ID（已标记0） · "
            f"可审批候选：{expected['auto']['selections']}条选择/{expected['auto']['ids']}个待审ID（已选0）",
        )
        month_button = self.page.locator(f'.month-btn[data-month="{expected["month"]}"]')
        self.assertIn(f"{expected['manual']['selections']}人工", month_button.inner_text())
        self.assertIn(f"{expected['auto']['selections']}候选", month_button.inner_text())

        self.page.locator('.cat-nav-item[data-cat="five"]').click()
        platform_status = self.page.locator('.cat-nav-item[data-cat="five"] .cat-status').inner_text()
        self.assertIn(f"{expected['platformRows']}条明细", platform_status)
        self.page.locator('.cat-nav-item[data-cat="six"]').click()
        self.assertIn(
            f"{expected['normalRows']}条明细",
            self.page.locator('.cat-nav-item[data-cat="six"] .cat-status').inner_text(),
        )
        self.assertIn(f"{expected['totalRows']}条明细", self.page.locator("#statsMeta").inner_text())
        self.assert_no_page_errors()

    def test_category_navigation_colors_follow_unresolved_items(self):
        self.page.evaluate(
            """() => {
                CAT_DATA = {
                    one: {title: '一、漏报人员', items: [{person: 'A'}]},
                    two: {title: '二、请假', items: [{approval_group_key: 'auto-pending'}]},
                    three: {title: '三、工时异常', items: [{approval_group_key: 'manual-pending'}]},
                    four: {title: '四、项目归属异常', items: [{approval_group_key: 'already-approved'}]},
                    five: {title: '五、平台类', items: [{approval_group_key: 'selected-pending'}]},
                    six: {title: '六、正常申报', items: [{approval_unavailable_reason: 'cannot match'}]},
                    seven: {title: '七、其他待定', items: [{detail: 'needs review'}]}
                };
                APPROVAL_GROUPS = {
                    'auto-pending': {group_key: 'auto-pending', review_mode: 'auto', approve_ids: ['1']},
                    'manual-pending': {group_key: 'manual-pending', review_mode: 'manual', approve_ids: ['2']},
                    'already-approved': {group_key: 'already-approved', review_mode: 'manual', approve_ids: ['3'], status: 'approved'},
                    'selected-pending': {group_key: 'selected-pending', review_mode: 'auto', approve_ids: ['4']}
                };
                approvalState = {'selected-pending': 'selected'};
                renderCategoryNav();
            }"""
        )

        attention_keys = ["one", "two", "three", "five", "six", "seven"]
        for key in attention_keys:
            self.assertTrue(
                self.page.locator(f'.cat-nav-item[data-cat="{key}"]').evaluate(
                    "node => node.classList.contains('attention')"
                ),
                key,
            )
        self.assertTrue(
            self.page.locator('.cat-nav-item[data-cat="four"]').evaluate(
                "node => node.classList.contains('complete')"
            )
        )
        self.assertIn(
            "待处理1条", self.page.locator('.cat-nav-item[data-cat="one"] .cat-status').inner_text()
        )
        self.assertIn(
            "待处理1条", self.page.locator('.cat-nav-item[data-cat="seven"] .cat-status').inner_text()
        )
        self.assert_no_page_errors()

    def test_ui_refresh_uses_read_only_endpoint_and_clears_updated_month_selection(self):
        requests, _ = self.open_mock_local_service(lambda: {})
        group_key = self.page.evaluate(
            """() => {
                const group = Object.values(APPROVAL_GROUPS).find(value => value.review_mode === 'manual');
                localStorage.setItem(getStorageKey(currentMonth), JSON.stringify({[group.group_key]: 'selected'}));
                return group.group_key;
            }"""
        )

        self.page.locator("#btnRefreshDashboard").click()
        self.page.wait_for_timeout(1500)

        refresh_start = [item for item in requests if item["path"] == "/api/v1/dashboard/refresh"]
        refresh_poll = [
            item
            for item in requests
            if item["path"] == "/api/v1/dashboard/refresh/jobs/test-refresh-job-id-1234567890"
        ]
        self.assertEqual(1, len(refresh_start))
        self.assertEqual({}, refresh_start[0]["body"])
        self.assertGreaterEqual(len(refresh_poll), 1)
        self.assertFalse(any("/approval/" in item["path"] for item in requests))
        self.assertFalse(any("/credentials/" in item["path"] for item in requests))
        self.assertIsNone(
            self.page.evaluate("key => localStorage.getItem(key)", f"srdpm_approval_v3_{self.current_month}")
        )
        self.assertNotEqual("", group_key)
        self.assert_no_page_errors()

    def test_viewing_automatic_categories_does_not_mutate_local_state(self):
        storage_key = f"srdpm_approval_v3_{self.current_month}"
        self.assertIsNone(self.page.evaluate("key => localStorage.getItem(key)", storage_key))
        self.page.locator('.cat-nav-item[data-cat="five"]').click()
        self.page.locator('.cat-nav-item[data-cat="six"]').click()
        self.assertIsNone(self.page.evaluate("key => localStorage.getItem(key)", storage_key))
        self.assert_no_page_errors()

    def test_manual_selection_uses_stable_group_key_and_can_be_undone(self):
        self.page.locator('.cat-nav-item[data-cat="three"]').click()
        first_action = self.page.locator("#panel_three tbody .btn-approve").first
        first_action.click()
        state = json.loads(
            self.page.evaluate("key => localStorage.getItem(key)", f"srdpm_approval_v3_{self.current_month}")
        )
        self.assertEqual(len(state), 1)
        key, value = next(iter(state.items()))
        self.assertTrue(key.startswith("grp_"))
        self.assertEqual(value, "selected")
        self.assertIn("已标记1", self.page.locator("#pendingCount").inner_text())
        self.assertEqual(
            self.page.locator(".cat-nav-item.active").get_attribute("data-cat"), "three"
        )

        self.page.locator("#panel_three tbody .btn-approve.done").first.click()
        state = json.loads(
            self.page.evaluate("key => localStorage.getItem(key)", f"srdpm_approval_v3_{self.current_month}")
        )
        self.assertEqual(state, {})
        self.assert_no_page_errors()

    def test_bulk_auto_selection_exports_unique_offline_plan(self):
        expected = self.page.evaluate(
            """() => {
                const groups = Object.values(APPROVAL_GROUPS).filter(group =>
                    group.review_mode === 'auto' && getGroupStatus(group.group_key) !== 'approved'
                );
                return {
                    selections: groups.length,
                    ids: groups.reduce((sum, group) => sum + (group.approve_ids || []).length, 0)
                };
            }"""
        )
        self.page.locator('.cat-nav-item[data-cat="six"]').click()
        self.page.locator("button[onclick='selectAllAutoGroups()']").click()
        self.assertIn(f"已选{expected['selections']}", self.page.locator("#pendingCount").inner_text())
        self.assertIn(
            f"{expected['selections']}条选择/{expected['ids']}个待审ID",
            self.page.locator("#btnConfirm").inner_text(),
        )

        self.page.once("dialog", lambda dialog: dialog.accept())
        with self.page.expect_download() as download_info:
            self.page.locator("#btnConfirm").click()
        download = download_info.value
        self.assertEqual(
            download.suggested_filename,
            f"srdpm-approval-plan-{self.current_month}.json",
        )
        parsed_plan = load_plan(Path(download.path()))
        parsed_summary = summarize_plan(parsed_plan)
        plan = self.page.evaluate("window.__lastApprovalPlan")
        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["summary"]["selection_count"], expected["selections"])
        self.assertEqual(plan["summary"]["item_count"], expected["ids"])
        self.assertEqual(parsed_summary.group_count, plan["summary"]["group_count"])
        self.assertEqual(parsed_summary.id_count, expected["ids"])
        ids = [approve_id for group in plan["groups"] for approve_id in group["approve_ids"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assert_no_page_errors()

    def test_static_file_builds_minimal_local_service_handoff(self):
        self.page.locator('.cat-nav-item[data-cat="three"]').click()
        self.page.locator("#panel_three tbody .btn-approve").first.click()
        transfer_url = self.page.evaluate(
            "buildLocalServiceTransferUrl(buildSelectedApprovalPlan())"
        )
        parsed = urlparse(transfer_url)
        payload = json.loads(parse_qs(parsed.fragment)["approval_selection"][0])

        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}", "http://127.0.0.1:8765")
        self.assertEqual(set(payload), {"version", "month", "group_keys"})
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["month"], self.current_month)
        self.assertEqual(len(payload["group_keys"]), 1)
        self.assertNotIn("approve_ids", json.dumps(payload))
        self.assertNotIn("person", json.dumps(payload))
        self.assertNotIn("user_id", json.dumps(payload))
        self.assertEqual(
            self.page.locator("#approvalServiceStatus").inner_text(),
            "直接审批：点击按钮将自动连接后台服务",
        )
        self.assert_no_page_errors()

    def test_local_service_consumes_handoff_once_and_replaces_stale_selection(self):
        groups = self.page.evaluate(
            "Object.values(APPROVAL_GROUPS).filter(group => group.review_mode === 'manual').slice(0, 2)"
        )
        selected, stale = groups
        transfer_url = self.page.evaluate(
            """key => {
                approvalState = {[key]: 'selected'};
                return buildLocalServiceTransferUrl(buildSelectedApprovalPlan());
            }""",
            selected["group_key"],
        )
        row = {
            "group_key": selected["group_key"],
            "date": selected["date"],
            "person": selected["person"],
            "id_count": len(selected["approve_ids"]),
            "item_count": selected["item_count"],
            "work_hours": 8,
            "projects": ["P/HANDOFF"],
            "project_count": 1,
            "review_summary": "三、工时异常",
            "review_mode": selected["review_mode"],
        }
        stale_json = json.dumps({stale["group_key"]: "selected"})
        self.context.add_init_script(
            script=f"""if (location.hostname === '127.0.0.1') {{
                localStorage.setItem('srdpm_approval_v3_{self.current_month}', {json.dumps(stale_json)});
            }}"""
        )

        requests, _ = self.open_mock_local_service(
            lambda: {},
            initial_fragment="#" + urlparse(transfer_url).fragment,
            initial_groups=[row],
        )
        self.page.wait_for_selector("#approvalConfirmOverlay.show")

        self.assertEqual(urlparse(self.page.url).fragment, "")
        state = json.loads(
            self.page.evaluate("key => localStorage.getItem(key)", f"srdpm_approval_v3_{self.current_month}")
        )
        self.assertEqual(state, {selected["group_key"]: "selected"})
        prepare = next(item for item in requests if item["path"] == "/api/v1/approval/prepare")
        self.assertEqual(
            prepare["body"],
            {"month": self.current_month, "group_keys": [selected["group_key"]]},
        )
        self.assertFalse(any(item["path"] == "/api/v1/approval/execute" for item in requests))
        self.assertIn("P/HANDOFF", self.page.locator("#approvalConfirmOverlay").inner_text())
        self.page.locator("#approvalConfirmCancel").click()
        self.assert_no_page_errors()

    def test_first_ui_credential_setup_precedes_prepare_and_clears_password(self):
        requests, expected = self.open_mock_local_service(
            lambda: {}, credential_configured=False
        )
        selected = self.page.evaluate(
            """() => {
                const group = Object.values(APPROVAL_GROUPS).find(value => value.review_mode === 'manual');
                approvalState[group.group_key] = 'selected';
                saveState();
                updatePendingCount();
                return group;
            }"""
        )
        row = {
            "group_key": selected["group_key"],
            "date": selected["date"],
            "person": selected["person"],
            "id_count": len(selected["approve_ids"]),
            "item_count": selected["item_count"],
            "work_hours": 8,
            "projects": ["P/CREDENTIAL"],
            "review_summary": "三、工时异常",
            "review_mode": selected["review_mode"],
        }
        expected["id_counts"][selected["group_key"]] = len(selected["approve_ids"])
        expected["groups"][selected["group_key"]] = row

        self.page.locator("#btnExecute").click()
        self.page.wait_for_selector("#credentialSetupOverlay.show")
        self.assertFalse(any(item["path"] == "/api/v1/approval/prepare" for item in requests))
        username = "ui-offline-user"
        password = "ui-offline-secret"
        self.page.locator("#credentialUsername").fill(username)
        self.page.locator("#credentialPassword").fill(password)
        self.page.locator("#credentialSetupSave").click()
        self.page.wait_for_selector("#approvalConfirmOverlay.show")

        configured = next(
            item for item in requests if item["path"] == "/api/v1/credentials/configure"
        )
        self.assertEqual(
            configured["body"], {"username": username, "password": password}
        )
        self.assertEqual(self.page.locator("#credentialPassword").input_value(), "")
        self.assertNotIn(password, self.page.content())
        self.assertNotIn(password, self.page.evaluate("JSON.stringify(localStorage)"))
        self.assertTrue(any(item["path"] == "/api/v1/approval/prepare" for item in requests))
        self.assertFalse(any(item["path"] == "/api/v1/approval/execute" for item in requests))
        self.page.locator("#approvalConfirmCancel").click()
        self.assert_no_page_errors()

    def test_invalid_handoff_is_cleared_without_any_api_action(self):
        fragment = "#" + urlencode(
            {
                "approval_selection": json.dumps(
                    {
                        "version": 2,
                        "month": self.current_month,
                        "group_keys": ["grp_ffffffffffffffffffff"],
                    }
                )
            }
        )
        requests, _ = self.open_mock_local_service(
            lambda: {}, initial_fragment=fragment
        )

        self.assertEqual(urlparse(self.page.url).fragment, "")
        self.assertIn("无法导入所选明细", self.page.locator("#approvalFeedback").inner_text())
        self.assertEqual(requests, [])
        self.assert_no_page_errors()

    def test_cancelling_ui_credential_setup_never_prepares_or_executes(self):
        requests, _ = self.open_mock_local_service(
            lambda: {}, credential_configured=False
        )
        self.page.evaluate(
            """() => {
                const group = Object.values(APPROVAL_GROUPS).find(value => value.review_mode === 'manual');
                approvalState[group.group_key] = 'selected';
                saveState();
                updatePendingCount();
            }"""
        )
        self.page.locator("#btnExecute").click()
        self.page.wait_for_selector("#credentialSetupOverlay.show")
        self.page.locator("#credentialPassword").fill("cancelled-secret")
        self.page.locator("#credentialSetupCancel").click()

        self.assertFalse(any("/approval/" in item["path"] for item in requests))
        self.assertEqual(self.page.locator("#credentialPassword").input_value(), "")
        self.assertNotIn("cancelled-secret", self.page.content())
        self.assert_no_page_errors()

    def test_platform_selected_rows_can_be_cancelled_in_platform_view(self):
        expected_auto_selected = self.page.evaluate(
            """() => Object.values(APPROVAL_GROUPS).filter(group =>
                group.review_mode === 'auto' && getGroupStatus(group.group_key) !== 'approved'
            ).length"""
        )
        self.page.locator("button[onclick='selectAllAutoGroups()']").click()
        self.page.locator('.cat-nav-item[data-cat="five"]').click()

        self.assertGreater(
            self.page.locator("#panel_five .btn-approve.done").count(), 0
        )
        self.assertIn(
            "取消本类选择",
            self.page.locator("#panel_five .bulk-actions").inner_text(),
        )

        before = json.loads(
            self.page.evaluate("key => localStorage.getItem(key)", f"srdpm_approval_v3_{self.current_month}")
        )
        self.assertEqual(len(before), expected_auto_selected)
        self.page.locator("#panel_five .btn-approve.done").first.click()
        after = json.loads(
            self.page.evaluate("key => localStorage.getItem(key)", f"srdpm_approval_v3_{self.current_month}")
        )
        self.assertEqual(len(after), expected_auto_selected - 1)
        self.assertIn(
            f"已选{expected_auto_selected - 1}", self.page.locator("#pendingCount").inner_text()
        )
        self.assertEqual(
            self.page.locator(".cat-nav-item.active").get_attribute("data-cat"), "five"
        )

        self.page.locator("#panel_five .bulk-actions button").filter(
            has_text="取消本类选择"
        ).click()
        remaining_platform_selected = self.page.evaluate(
            """groupKeysForCategory('five', false).filter(groupKey =>
                APPROVAL_GROUPS[groupKey]?.review_mode === 'auto' &&
                getGroupStatus(groupKey) === 'selected'
            ).length"""
        )
        self.assertEqual(remaining_platform_selected, 0)
        self.assert_no_page_errors()

    def test_reset_keeps_current_tab_and_never_dereferences_missing_active_node(self):
        self.page.locator('.cat-nav-item[data-cat="six"]').click()
        self.page.locator("button[onclick='selectAllAutoGroups()']").click()
        self.page.once("dialog", lambda dialog: dialog.accept())
        self.page.locator("button[onclick='resetAll()']").click()
        self.assertEqual(
            self.page.locator(".cat-nav-item.active").get_attribute("data-cat"), "six"
        )
        self.assertIsNone(
            self.page.evaluate("key => localStorage.getItem(key)", f"srdpm_approval_v3_{self.current_month}")
        )
        self.assert_no_page_errors()

    def test_normal_items_support_multi_filter_and_filtered_hours_summary(self):
        self.page.locator('.cat-nav-item[data-cat="six"]').click()
        expected = self.page.evaluate(
            """() => {
                const rows = CAT_DATA.six.items.filter(item => item.chip);
                const persons = [...new Set(rows.map(item => item.person))].slice(0, 2);
                const chips = [...new Set(rows.filter(item => persons.includes(item.person)).map(item => item.chip))].slice(0, 2);
                filterState6.persons = persons;
                filterState6.chips = chips;
                filterState6.search = '';
                refilterSix();
                const filtered = CAT_DATA.six.items.filter(item =>
                    persons.includes(item.person) && chips.includes(item.chip)
                );
                return {
                    persons,
                    chips,
                    count: filtered.length,
                    total: filtered.reduce((sum, item) => sum + (Number(item.hours) || 0), 0),
                };
            }"""
        )
        self.assertGreaterEqual(len(expected["persons"]), 1)
        self.assertGreaterEqual(len(expected["chips"]), 1)
        self.assertEqual(
            self.page.locator("#panel_six .multi-filter input:checked").count(),
            len(expected["persons"]) + len(expected["chips"]),
        )
        self.assertIn(
            f"显示 {expected['count']} 条",
            self.page.locator("#panel_six #filterCount6").inner_text(),
        )
        summary_text = self.page.locator("#panel_six .hours-summary").inner_text()
        self.assertIn("当前筛选范围工时汇总", summary_text)
        self.assertIn("机芯合计", summary_text)
        self.assertIn("人员合计", summary_text)
        self.assertIn(
            self.page.evaluate("value => formatHours(value)", expected["total"]),
            summary_text,
        )
        self.assert_no_page_errors()

    def test_untrusted_archive_values_are_rendered_as_text(self):
        attack = '</script><img data-xss="1" src=x onerror="window.__xss=1">&"'
        self.page.evaluate(
            """attack => {
                window.__xss = 0;
                CAT_DATA.five.items[0].content = attack;
                CAT_DATA.five.items[0].person = attack;
                renderPanel('five');
            }""",
            attack,
        )

        self.assertEqual(self.page.evaluate("window.__xss"), 0)
        self.assertEqual(self.page.locator("img[data-xss='1']").count(), 0)
        self.assertIn(attack, self.page.locator("#panel_five").inner_text())
        self.assert_no_page_errors()

    def test_direct_approval_sends_only_month_and_group_keys_and_marks_verified(self):
        result = {"group_key": None}

        def job_factory():
            group = result["group"]
            return {
                "job_id": "test-job-id-1234567890",
                "status": "succeeded",
                "outcome": "succeeded",
                "message": "done",
                "groups": [{**group, "state": "verified_approved", "message": "verified"}],
            }

        requests, expected = self.open_mock_local_service(job_factory)
        selected = self.page.evaluate(
            """() => {
                const group = Object.values(APPROVAL_GROUPS).find(value => value.review_mode === 'manual');
                approvalState[group.group_key] = 'selected';
                saveState();
                renderCategoryNav();
                updatePendingCount();
                return group;
            }"""
        )
        group_key = selected["group_key"]
        result["group"] = {
            "group_key": group_key,
            "date": selected["date"],
            "person": selected["person"],
            "id_count": len(selected["approve_ids"]),
            "item_count": selected["item_count"],
            "work_hours": 8,
            "projects": ["P/TEST"],
            "review_summary": "三、工时异常",
            "review_mode": selected["review_mode"],
        }
        expected["id_counts"][group_key] = len(selected["approve_ids"])
        expected["groups"][group_key] = result["group"]

        self.page.locator("#btnExecute").click()
        self.page.wait_for_selector("#approvalConfirmOverlay.show")
        confirmation_text = self.page.locator("#approvalConfirmOverlay").inner_text()
        self.assertIn(selected["date"], confirmation_text)
        self.assertIn(selected["person"], confirmation_text)
        self.assertIn("P/TEST", confirmation_text)
        self.assertIn("8.00h", confirmation_text)
        self.assertFalse(any(item["path"] == "/api/v1/approval/execute" for item in requests))
        self.page.locator("#approvalConfirmAccept").click()
        self.page.wait_for_function(
            "document.querySelector('#approvalFeedback').textContent.includes('审批完成')"
        )

        prepare = next(item for item in requests if item["path"] == "/api/v1/approval/prepare")
        execute = next(item for item in requests if item["path"] == "/api/v1/approval/execute")
        self.assertEqual(
            prepare["body"], {"month": self.current_month, "group_keys": [group_key]}
        )
        self.assertEqual(execute["body"], {"ticket": "one-use-ticket"})
        self.assertTrue(all(item["csrf"] == "test-csrf-token-with-at-least-32-characters" for item in requests))
        self.assertEqual(
            self.page.evaluate("key => getGroupStatus(key)", group_key), "approved"
        )
        self.assertEqual(
            json.loads(
                self.page.evaluate(
                    "key => localStorage.getItem(key)",
                    f"srdpm_approval_v3_{self.current_month}",
                )
            ),
            {},
        )
        self.assert_no_page_errors()

    def test_partial_result_keeps_unknown_group_selected(self):
        result = {"rows": []}

        def job_factory():
            return {
                "job_id": "test-job-id-1234567890",
                "status": "failed",
                "outcome": "partial_success",
                "message": "partial",
                "groups": result["rows"],
            }

        requests, expected = self.open_mock_local_service(job_factory)
        selected_groups = self.page.evaluate(
            """() => {
                const groups = Object.values(APPROVAL_GROUPS)
                    .filter(value => value.review_mode === 'manual').slice(0, 2);
                for (const group of groups) approvalState[group.group_key] = 'selected';
                saveState();
                renderCategoryNav();
                updatePendingCount();
                return groups;
            }"""
        )
        for group in selected_groups:
            row = {
                "group_key": group["group_key"],
                "date": group["date"],
                "person": group["person"],
                "id_count": len(group["approve_ids"]),
                "item_count": group["item_count"],
                "work_hours": 4,
                "projects": ["P/PARTIAL"],
                "review_summary": "三、工时异常",
                "review_mode": group["review_mode"],
            }
            expected["id_counts"][group["group_key"]] = len(group["approve_ids"])
            expected["groups"][group["group_key"]] = row
        result["rows"] = [
            {**expected["groups"][selected_groups[0]["group_key"]], "state": "verified_approved", "message": "verified"},
            {**expected["groups"][selected_groups[1]["group_key"]], "state": "unknown", "message": "unknown"},
        ]

        self.page.locator("#btnExecute").click()
        self.page.wait_for_selector("#approvalConfirmOverlay.show")
        self.page.locator("#approvalConfirmAccept").click()
        self.page.wait_for_function(
            "document.querySelector('#approvalFeedback').textContent.includes('部分完成')"
        )

        self.assertEqual(
            self.page.evaluate("key => getGroupStatus(key)", selected_groups[0]["group_key"]),
            "approved",
        )
        self.assertEqual(
            self.page.evaluate("key => getGroupStatus(key)", selected_groups[1]["group_key"]),
            "selected",
        )
        self.assert_no_page_errors()


if __name__ == "__main__":
    unittest.main()
