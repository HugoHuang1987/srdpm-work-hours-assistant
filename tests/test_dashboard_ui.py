import json
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import build_multi_month_dashboard as dashboard
from apply_approval_plan import load_plan, summarize_plan
from playwright.sync_api import sync_playwright


class DashboardUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dashboard.main()
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - depends on local browser install
            cls.playwright.stop()
            raise unittest.SkipTest(f"Playwright Chromium 不可用：{exc}")
        cls.dashboard_uri = Path(dashboard.OUTPUT_HTML).resolve().as_uri()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "playwright"):
            cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(accept_downloads=True)
        self.context.route("http://**", lambda route: route.abort())
        self.context.route("https://**", lambda route: route.abort())
        self.page = self.context.new_page()
        self.page_errors = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        self.page.goto(self.dashboard_uri, wait_until="domcontentloaded")
        self.page.wait_for_selector("#pendingCount")

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
            route.fulfill(status=404, content_type="application/json", body='{"ok":false}')

        self.context.route("http://127.0.0.1:8765/**", route_local_service)
        self.page.goto(
            f"http://127.0.0.1:8765/{initial_fragment}",
            wait_until="domcontentloaded",
        )
        self.page.wait_for_selector("#pendingCount")
        self.assertEqual(
            self.page.locator("#approvalServiceStatus").inner_text(),
            "本机审批服务：已连接",
        )
        return requests, expected

    def test_initial_counts_use_exact_selectable_rows_and_unique_ids(self):
        pending = self.page.locator("#pendingCount").inner_text()
        self.assertEqual(
            pending,
            "需人工审核：4条选择/36个待审ID（已标记0） · 可审批候选：448条选择/448个待审ID（已选0）",
        )
        july_button = self.page.locator('.month-btn[data-month="2026-07"]')
        self.assertIn("4人工", july_button.inner_text())
        self.assertIn("448候选", july_button.inner_text())

        self.page.locator('.cat-nav-item[data-cat="five"]').click()
        platform_status = self.page.locator('.cat-nav-item[data-cat="five"] .cat-status').inner_text()
        self.assertIn("52条", platform_status)
        self.assertIn("52条", platform_status)
        self.page.locator('.cat-nav-item[data-cat="six"]').click()
        self.assertIn("396条明细", self.page.locator('.cat-nav-item[data-cat="six"] .cat-status').inner_text())
        self.assertIn("484条明细", self.page.locator("#statsMeta").inner_text())
        self.assert_no_page_errors()

    def test_viewing_automatic_categories_does_not_mutate_local_state(self):
        storage_key = "srdpm_approval_v3_2026-07"
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
            self.page.evaluate("localStorage.getItem('srdpm_approval_v3_2026-07')")
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
            self.page.evaluate("localStorage.getItem('srdpm_approval_v3_2026-07')")
        )
        self.assertEqual(state, {})
        self.assert_no_page_errors()

    def test_bulk_auto_selection_exports_unique_offline_plan(self):
        self.page.locator('.cat-nav-item[data-cat="six"]').click()
        self.page.locator("button[onclick='selectAllAutoGroups()']").click()
        self.assertIn("已选448", self.page.locator("#pendingCount").inner_text())
        self.assertIn("448条选择/448个待审ID", self.page.locator("#btnConfirm").inner_text())

        self.page.once("dialog", lambda dialog: dialog.accept())
        with self.page.expect_download() as download_info:
            self.page.locator("#btnConfirm").click()
        download = download_info.value
        self.assertEqual(download.suggested_filename, "srdpm-approval-plan-2026-07.json")
        parsed_plan = load_plan(Path(download.path()))
        parsed_summary = summarize_plan(parsed_plan)
        plan = self.page.evaluate("window.__lastApprovalPlan")
        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["summary"]["selection_count"], 448)
        self.assertEqual(plan["summary"]["item_count"], 448)
        self.assertEqual(parsed_summary.group_count, plan["summary"]["group_count"])
        self.assertEqual(parsed_summary.id_count, 448)
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
        self.assertEqual(payload["month"], "2026-07")
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
                localStorage.setItem('srdpm_approval_v3_2026-07', {json.dumps(stale_json)});
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
            self.page.evaluate("localStorage.getItem('srdpm_approval_v3_2026-07')")
        )
        self.assertEqual(state, {selected["group_key"]: "selected"})
        prepare = next(item for item in requests if item["path"] == "/api/v1/approval/prepare")
        self.assertEqual(
            prepare["body"],
            {"month": "2026-07", "group_keys": [selected["group_key"]]},
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
                        "month": "2026-07",
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
            self.page.evaluate("localStorage.getItem('srdpm_approval_v3_2026-07')")
        )
        self.assertEqual(len(before), 448)
        self.page.locator("#panel_five .btn-approve.done").first.click()
        after = json.loads(
            self.page.evaluate("localStorage.getItem('srdpm_approval_v3_2026-07')")
        )
        self.assertEqual(len(after), 447)
        self.assertIn("已选447", self.page.locator("#pendingCount").inner_text())
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
            self.page.evaluate("localStorage.getItem('srdpm_approval_v3_2026-07')")
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
        self.assertEqual(prepare["body"], {"month": "2026-07", "group_keys": [group_key]})
        self.assertEqual(execute["body"], {"ticket": "one-use-ticket"})
        self.assertTrue(all(item["csrf"] == "test-csrf-token-with-at-least-32-characters" for item in requests))
        self.assertEqual(
            self.page.evaluate("key => getGroupStatus(key)", group_key), "approved"
        )
        self.assertEqual(
            json.loads(self.page.evaluate("localStorage.getItem('srdpm_approval_v3_2026-07')")),
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
