import json
import unittest
from pathlib import Path

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
        self.context.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
        self.page = self.context.new_page()
        self.page_errors = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        self.page.goto(self.dashboard_uri, wait_until="domcontentloaded")
        self.page.wait_for_selector("#pendingCount")

    def tearDown(self):
        self.context.close()

    def assert_no_page_errors(self):
        self.assertEqual(self.page_errors, [])

    def test_initial_counts_use_person_day_groups_and_unique_ids(self):
        pending = self.page.locator("#pendingCount").inner_text()
        self.assertEqual(
            pending,
            "需人工审核：4个人日/41条明细（已标记0） · 可自动审批：50个人日/443条明细（已选0）",
        )
        july_button = self.page.locator('.month-btn[data-month="2026-07"]')
        self.assertIn("4人工", july_button.inner_text())
        self.assertIn("50自动", july_button.inner_text())

        self.page.locator('.cat-nav-item[data-cat="five"]').click()
        platform_status = self.page.locator('.cat-nav-item[data-cat="five"] .cat-status').inner_text()
        self.assertIn("52条", platform_status)
        self.assertIn("审批随所属整单", platform_status)
        self.page.locator('.cat-nav-item[data-cat="six"]').click()
        self.assertIn("396条明细", self.page.locator('.cat-nav-item[data-cat="six"] .cat-status').inner_text())
        self.assertIn("484条明细", self.page.locator("#statsMeta").inner_text())
        self.assert_no_page_errors()

    def test_viewing_automatic_categories_does_not_mutate_local_state(self):
        storage_key = "srdpm_approval_v2_2026-07"
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
            self.page.evaluate("localStorage.getItem('srdpm_approval_v2_2026-07')")
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
            self.page.evaluate("localStorage.getItem('srdpm_approval_v2_2026-07')")
        )
        self.assertEqual(state, {})
        self.assert_no_page_errors()

    def test_bulk_auto_selection_exports_unique_offline_plan(self):
        self.page.locator('.cat-nav-item[data-cat="six"]').click()
        self.page.locator("button[onclick='selectAllAutoGroups()']").click()
        self.assertIn("已选50", self.page.locator("#pendingCount").inner_text())
        self.assertIn("50个人日/443条明细", self.page.locator("#btnConfirm").inner_text())

        self.page.once("dialog", lambda dialog: dialog.accept())
        with self.page.expect_download() as download_info:
            self.page.locator("#btnConfirm").click()
        download = download_info.value
        self.assertEqual(download.suggested_filename, "srdpm-approval-plan-2026-07.json")
        parsed_plan = load_plan(Path(download.path()))
        parsed_summary = summarize_plan(parsed_plan)
        self.assertEqual(parsed_summary.group_count, 50)
        self.assertEqual(parsed_summary.id_count, 443)
        plan = self.page.evaluate("window.__lastApprovalPlan")
        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["summary"]["group_count"], 50)
        self.assertEqual(plan["summary"]["item_count"], 443)
        self.assertTrue(all(group["review_mode"] == "auto" for group in plan["groups"]))
        ids = [approve_id for group in plan["groups"] for approve_id in group["approve_ids"]]
        self.assertEqual(len(ids), len(set(ids)))
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
            self.page.evaluate("localStorage.getItem('srdpm_approval_v2_2026-07')")
        )
        self.assertEqual(len(before), 50)
        self.page.locator("#panel_five .btn-approve.done").first.click()
        after = json.loads(
            self.page.evaluate("localStorage.getItem('srdpm_approval_v2_2026-07')")
        )
        self.assertEqual(len(after), 49)
        self.assertIn("已选49", self.page.locator("#pendingCount").inner_text())
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
            self.page.evaluate("localStorage.getItem('srdpm_approval_v2_2026-07')")
        )
        self.assert_no_page_errors()


if __name__ == "__main__":
    unittest.main()
