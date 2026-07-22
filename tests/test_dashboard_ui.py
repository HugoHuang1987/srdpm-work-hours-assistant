import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import build_multi_month_dashboard as dashboard
from apply_approval_plan import load_plan, summarize_plan
from approval_readback_store import save_approval_readback_entries
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
        self.available_months = self.page.evaluate("MONTHS")

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
        dashboard_path = Path(dashboard.OUTPUT_HTML)
        config = {
            "api_base": "/api/v1",
            "csrf_header": "X-SRDPM-CSRF",
            "csrf_token": "test-csrf-token-with-at-least-32-characters",
            "instance_id": "test-instance",
        }
        def served_html():
            return dashboard_path.read_text(encoding="utf-8").replace(
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
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=served_html())
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
                        "updated_months": [self.current_month],
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
                    display: ALL_DATA[currentMonth].display,
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
        self.assertEqual(
            expected["display"],
            self.page.locator("#monthSelector details.month-multi summary").inner_text(),
        )
        self.assertEqual([expected["month"]], self.page.evaluate("selectedMonths"))

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
                    four: {title: '四、项目归属异常', items: [
                        {approval_group_key: 'already-approved'},
                        {status: 'approved', approval_unavailable_reason: 'matched multiple approved rows'}
                    ]},
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
        fourth_status = self.page.locator('.cat-nav-item[data-cat="four"] .cat-status').inner_text()
        self.assertIn("待处理0", fourth_status)
        self.assertIn("1条重复明细已通过", fourth_status)
        self.assertEqual("approved", self.page.evaluate("getStatus('four', 1)"))
        self.assertIn(
            "待处理1条", self.page.locator('.cat-nav-item[data-cat="one"] .cat-status').inner_text()
        )
        self.assertIn(
            "待处理1条", self.page.locator('.cat-nav-item[data-cat="seven"] .cat-status').inner_text()
        )
        self.assert_no_page_errors()

    def test_pending_only_filter_is_available_for_categories_two_to_seven(self):
        self.page.evaluate(
            """() => {
                CAT_DATA = {
                    two: {title: '二、请假', items: [
                        {date: '2026-07-01', person: '待处理', project: 'P1', title: '请假', content: '', hours: 1, approval_group_key: 'pending-auto'},
                        {date: '2026-07-02', person: '已审批', project: 'P2', title: '请假', content: '', hours: 1, approval_group_key: 'approved-auto'}
                    ]},
                    three: {title: '三、工时异常', items: [
                        {date: '2026-07-01', person: '待处理', subtype: '低申报', reported: 1, checked: 2, leave: 0, effective: 1, ratio: '50%', detail: '', approval_group_key: 'pending-manual'},
                        {date: '2026-07-02', person: '已审批', subtype: '低申报', reported: 1, checked: 2, leave: 0, effective: 1, ratio: '50%', detail: '', approval_group_key: 'approved-manual'}
                    ]},
                    four: {title: '四、项目归属异常', items: [
                        {date: '2026-07-01', person: '待处理', customer: '', items: 'P1', title: '', content: '', hours: 1, chip: 'MT1', allowed: 'MT1', reason: '', approval_group_key: 'pending-manual'},
                        {date: '2026-07-02', person: '已审批', customer: '', items: 'P2', title: '', content: '', hours: 1, chip: 'MT2', allowed: 'MT2', reason: '', approval_group_key: 'approved-manual'}
                    ]},
                    five: {title: '五、公共事务/平台类', items: [
                        {date: '2026-07-01', person: '待处理', project: 'P1', title: '', content: '', hours: 1, approval_group_key: 'pending-auto'},
                        {date: '2026-07-02', person: '已审批', project: 'P2', title: '', content: '', hours: 1, approval_group_key: 'approved-auto'}
                    ]},
                    six: {title: '六、正常申报', items: [
                        {date: '2026-07-01', person: '待处理', project: 'P1', chip: 'MT1', title: '', content: '', hours: 1, approval_group_key: 'pending-auto'},
                        {date: '2026-07-02', person: '已审批', project: 'P2', chip: 'MT2', title: '', content: '', hours: 1, approval_group_key: 'approved-auto'}
                    ]},
                    seven: {title: '七、其他待定', items: [
                        {date: '2026-07-01', person: '待处理', detail: '需核对'},
                        {date: '2026-07-02', person: '已审批', detail: '信息行', status: 'approved'}
                    ]}
                };
                APPROVAL_GROUPS = {
                    'pending-auto': {group_key: 'pending-auto', review_mode: 'auto', approve_ids: ['a']},
                    'approved-auto': {group_key: 'approved-auto', review_mode: 'auto', approve_ids: ['b'], status: 'approved'},
                    'pending-manual': {group_key: 'pending-manual', review_mode: 'manual', approve_ids: ['c']},
                    'approved-manual': {group_key: 'approved-manual', review_mode: 'manual', approve_ids: ['d'], status: 'approved'}
                };
                approvalState = {};
                resetAllPageState();
            }"""
        )
        for key in ("two", "three", "four", "five", "six", "seven"):
            self.page.evaluate("key => switchTab(key)", key)
            self.page.locator(f"#pendingOnly_{key}").check()
            expected_visible = 2 if key == "seven" else 1
            self.assertEqual(expected_visible, self.page.locator(f"#panel_{key} .table-wrap tbody tr:visible").count(), key)
        self.assert_no_page_errors()

    def test_three_and_four_show_separate_authorization_rule_columns(self):
        self.page.evaluate(
            """() => {
                window.__ruleXss = 0;
                const rules = {
                    authorization_rules_recorded: true,
                    current_rules: [
                        {chip: 'AM966D5'},
                        {chip: '<img data-rule-xss="1" src=x onerror="window.__ruleXss=1">'}
                    ],
                    historical_reasonable_rules: [
                        {chip: 'AM963D5', valid_through_month: '2026-09'},
                        {chip: 'MT9026L', valid_through_month: '2026-10'}
                    ],
                    historical_expired_rules: [
                        {chip: 'AM963D4', valid_through_month: '2026-06'}
                    ]
                };
                CAT_DATA.three = {title: '三、工时异常', items: [{
                    date: '2026-08-03', person: '甲', subtype: '低申报',
                    reported: 4, checked: 8, leave: 0, effective: 4, ratio: '50%',
                    detail: '测试', status: 'pending', ...rules
                }]};
                CAT_DATA.four = {title: '四、项目归属异常', items: [
                    {date: '2026-08-03', person: '甲', customer: 'G', items: 'P-01',
                     title: '测试', content: '测试', hours: 4, chip: 'AM963D5',
                     reason: '异常', status: 'pending', ...rules},
                    {date: '2026-08-04', person: '旧归档人员', customer: 'G', items: 'P-02',
                     title: '旧归档', content: '旧归档', hours: 4, chip: 'MT1',
                     reason: '异常', status: 'pending', authorization_rules_recorded: false,
                     authorization_person_listed: false, current_rules: [],
                     historical_reasonable_rules: [], historical_expired_rules: []},
                    {date: '2026-08-05', person: 'Wiki缺失人员', customer: 'G', items: 'P-03',
                     title: '人员缺失', content: '人员缺失', hours: 4, chip: 'MT2',
                     reason: '异常', status: 'pending', authorization_rules_recorded: true,
                     authorization_person_listed: false, current_rules: [],
                     historical_reasonable_rules: [], historical_expired_rules: []}
                ]};
                APPROVAL_GROUPS = {};
                approvalState = {};
                resetAllPageState();
                switchTab('three');
            }"""
        )

        self.assertEqual(
            [
                "日期", "人员", "当前规则机芯", "历史合理机芯", "历史过期机芯",
                "类型", "申报(h)", "打卡(h)", "休假(h)", "有效申报(h)",
                "比例", "工作内容", "审核状态", "操作",
            ],
            self.page.locator("#panel_three thead th").all_inner_texts(),
        )
        three_text = self.page.locator("#panel_three tbody").inner_text()
        self.assertIn("AM966D5", three_text)
        self.assertIn("AM963D5（有效至 2026-09）", three_text)
        self.assertIn("MT9026L（有效至 2026-10）", three_text)
        self.assertIn("AM963D4（已于 2026-06 到期）", three_text)
        self.assertEqual(0, self.page.evaluate("window.__ruleXss"))
        self.assertEqual(0, self.page.locator("img[data-rule-xss='1']").count())

        self.page.evaluate("switchTab('four')")
        self.assertEqual(
            [
                "日期 ⇅", "人员", "客户", "项目代码 ⇅", "标题", "工作内容", "工时", "机芯 ⇅",
                "当前规则机芯", "历史合理机芯", "历史过期机芯", "问题", "审核状态", "操作",
            ],
            self.page.locator("#panel_four thead th").all_inner_texts(),
        )
        four_text = self.page.locator("#panel_four tbody").inner_text()
        self.assertIn("旧归档未记录（需重新审计该月）", four_text)
        self.assertIn("Wiki 未列此人", four_text)
        self.assertNotIn("undefined", four_text)
        self.assertNotIn("null", four_text)
        self.assertEqual(0, self.page.evaluate("window.__ruleXss"))
        self.assertEqual(0, self.page.locator("img[data-rule-xss='1']").count())
        self.assert_no_page_errors()

    def test_three_rule_columns_do_not_break_type_and_person_filters(self):
        self.page.evaluate(
            """() => {
                const rules = {
                    authorization_rules_recorded: true,
                    current_rules: [{chip: 'AM966D5'}],
                    historical_reasonable_rules: [],
                    historical_expired_rules: []
                };
                CAT_DATA.three = {title: '三、工时异常', items: [
                    {date: '2026-08-01', person: '甲', subtype: '低申报', reported: 1,
                     checked: 2, leave: 0, effective: 1, ratio: '50%', detail: '', status: 'pending', ...rules},
                    {date: '2026-08-02', person: '乙', subtype: '超打卡', reported: 3,
                     checked: 2, leave: 0, effective: 3, ratio: '150%', detail: '', status: 'pending', ...rules}
                ]};
                APPROVAL_GROUPS = {};
                approvalState = {};
                resetAllPageState();
                switchTab('three');
            }"""
        )
        self.page.locator("#filter_subtype").select_option("低申报")
        self.page.locator("#filter_person3").select_option("甲")
        self.assertEqual(1, self.page.locator("#panel_three tbody tr:visible").count())
        self.assertIn("甲", self.page.locator("#panel_three tbody tr:visible").inner_text())
        self.assert_no_page_errors()

    def test_ui_refresh_uses_read_only_endpoint_and_clears_both_refreshed_months(self):
        self.assertGreaterEqual(len(self.available_months), 3)
        previous_month = self.available_months[-2]
        older_month = self.available_months[-3]
        requests, _ = self.open_mock_local_service(
            lambda: {},
            refresh_job_factory=lambda: {
                "job_id": "test-refresh-job-id-1234567890",
                "status": "succeeded",
                "updated_month": self.current_month,
                "updated_months": [previous_month, self.current_month],
                "mapping_updated": True,
                "message": "done",
            },
        )
        group_key = self.page.evaluate(
            """months => {
                const group = Object.values(APPROVAL_GROUPS).find(value =>
                    value.review_mode === 'manual' && getGroupStatus(value.group_key) !== 'approved'
                );
                months.forEach(month => {
                    localStorage.setItem(getStorageKey(month), JSON.stringify({[group.group_key]: 'selected'}));
                });
                return group.group_key;
            }""",
            [older_month, previous_month, self.current_month],
        )

        with self.page.expect_navigation(wait_until="domcontentloaded", timeout=10_000):
            self.page.locator("#btnRefreshDashboard").click()
        self.page.wait_for_function(
            "document.querySelector('#pendingCount')?.textContent.trim()",
            timeout=10_000,
        )

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
        self.assertIsNone(
            self.page.evaluate("key => localStorage.getItem(key)", f"srdpm_approval_v3_{previous_month}")
        )
        self.assertIsNotNone(
            self.page.evaluate("key => localStorage.getItem(key)", f"srdpm_approval_v3_{older_month}")
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
            "Object.values(APPROVAL_GROUPS).filter(group => group.review_mode === 'manual' && getGroupStatus(group.group_key) !== 'approved').slice(0, 2)"
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
                const group = Object.values(APPROVAL_GROUPS).find(value =>
                    value.review_mode === 'manual' && getGroupStatus(value.group_key) !== 'approved'
                );
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
                const group = Object.values(APPROVAL_GROUPS).find(value =>
                    value.review_mode === 'manual' && getGroupStatus(value.group_key) !== 'approved'
                );
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
        self.page.evaluate(
            """() => {
                CAT_DATA = {
                    one: {title: '一、漏报人员', items: []},
                    two: {title: '二、请假', items: []},
                    three: {title: '三、工时异常', items: []},
                    four: {title: '四、项目归属异常', items: []},
                    five: {title: '五、公共事务/平台类', items: [
                        {date: '2026-07-01', person: '平台甲', project: 'P1', title: '', content: '', hours: 1, approval_group_key: 'platform-one'},
                        {date: '2026-07-02', person: '平台乙', project: 'P2', title: '', content: '', hours: 1, approval_group_key: 'platform-two'}
                    ]},
                    six: {title: '六、正常申报', items: []},
                    seven: {title: '七、其他待定', items: []}
                };
                APPROVAL_GROUPS = {
                    'platform-one': {group_key: 'platform-one', review_mode: 'auto', approve_ids: ['p1']},
                    'platform-two': {group_key: 'platform-two', review_mode: 'auto', approve_ids: ['p2']}
                };
                approvalState = {};
                resetAllPageState();
                currentCatKey = 'five';
                renderCategoryNav();
                switchTab('five');
            }"""
        )
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

    def test_summary_category_precedes_one_and_filters_all_source_buckets(self):
        nav_keys = self.page.locator("#categoryNav .cat-nav-item").evaluate_all(
            "buttons => buttons.map(button => button.dataset.cat)"
        )
        self.assertEqual(nav_keys[:2], ["zero", "one"])
        self.page.locator('.cat-nav-item[data-cat="zero"]').click()
        expected = self.page.evaluate(
            """() => {
                const chips = ['请假/出差/休假', '公共事务/平台'].filter(chip =>
                    CAT_DATA.zero.items.some(item => item.chip === chip)
                );
                const persons = [...new Set(chips.map(chip =>
                    CAT_DATA.zero.items.find(item => item.chip === chip)?.person
                ).filter(Boolean))];
                filterState0.persons = persons;
                filterState0.chips = chips;
                refilterZero();
                const rows = CAT_DATA.zero.items.filter(item =>
                    persons.includes(item.person) && chips.includes(item.chip)
                );
                return {persons, chips, count: rows.length};
            }"""
        )
        self.assertEqual(
            self.page.locator("#panel_zero .multi-filter input:checked").count(),
            len(expected["persons"]) + len(expected["chips"]),
        )
        self.assertIn(
            f"纳入 {expected['count']} 条明细",
            self.page.locator("#panel_zero .filter-count").inner_text(),
        )
        summary_text = self.page.locator("#panel_zero .hours-summary").inner_text()
        for chip in expected["chips"]:
            self.assertIn(chip, summary_text)
        self.assert_no_page_errors()

    def test_zero_and_six_hours_summaries_are_one_and_half_times_larger(self):
        for key in ("zero", "six"):
            self.page.locator(f'.cat-nav-item[data-cat="{key}"]').click()
            summary = self.page.locator(f"#panel_{key} .hours-summary")
            summary_box = summary.bounding_box()
            layout = summary.evaluate(
                """element => {
                    const style = getComputedStyle(element);
                    const contentStyle = getComputedStyle(document.querySelector('#contentArea'));
                    return {
                        minWidth: style.minWidth,
                        minHeight: style.minHeight,
                        maxHeight: style.maxHeight,
                        contentMaxWidth: contentStyle.maxWidth,
                        expanded: document.querySelector('#contentArea').classList.contains('summary-expanded')
                    };
                }"""
            )

            self.assertIsNotNone(summary_box)
            self.assertGreaterEqual(summary_box["width"], 1170, key)
            self.assertGreaterEqual(summary_box["height"], 720, key)
            self.assertEqual("1170px", layout["minWidth"], key)
            self.assertEqual("720px", layout["minHeight"], key)
            self.assertEqual("1170px", layout["maxHeight"], key)
            self.assertEqual("2100px", layout["contentMaxWidth"], key)
            self.assertTrue(layout["expanded"], key)
        self.assert_no_page_errors()

    def test_month_selector_supports_multiple_months_and_select_all(self):
        month_labels = self.page.locator("#monthSelector .month-menu label:not(.month-all)").all_text_contents()
        self.assertEqual(len(month_labels), self.page.evaluate("MONTHS.length"))
        month_menu = self.page.locator("#monthSelector details.month-multi")
        month_menu.locator("summary").click()
        self.page.locator("#monthSelector .month-menu label:not(.month-all) input").first.check()
        self.assertTrue(self.page.locator("#monthSelector details.month-multi").evaluate("element => element.open"))
        self.assertEqual(self.page.evaluate("selectedMonths.length"), 2)
        self.page.evaluate("toggleAllMonths(true)")
        self.assertEqual(self.page.evaluate("selectedMonths"), self.page.evaluate("MONTHS"))
        self.assertEqual(self.page.evaluate("currentMonth"), "__selection__")
        self.assertTrue(self.page.evaluate("IS_AGGREGATE_VIEW"))
        self.page.evaluate("selectAllAutoGroups()")
        self.assertEqual(self.page.evaluate("Object.keys(approvalState).length"), 0)
        self.assertIsNone(self.page.evaluate("buildSelectedApprovalPlan()"))
        self.assertTrue(self.page.locator("#btnExecute").is_disabled())
        self.assertTrue(self.page.locator("#btnConfirm").is_disabled())
        self.assertFalse(self.page.locator("#btnSelectAllAuto").is_visible())
        self.assertIn("当前筛选范围工时汇总", self.page.locator("#panel_zero .hours-summary").inner_text())
        self.page.evaluate("applyMonthSelection(MONTHS.slice(0, 2))")
        self.assertEqual(self.page.evaluate("selectedMonths.length"), 2)
        self.assertTrue(self.page.evaluate("IS_AGGREGATE_VIEW"))
        self.assert_no_page_errors()

    def test_project_mismatch_table_is_paginated(self):
        self.page.evaluate("switchMonth('2026-01')")
        self.page.locator('.cat-nav-item[data-cat="four"]').click()
        total = self.page.evaluate("CAT_DATA.four.items.length")
        self.assertGreater(total, 20)
        self.assertTrue(self.page.evaluate("PAGINATED_CATS.includes('four')"))
        self.assertEqual(20, self.page.locator("#panel_four tbody tr").count())
        self.assertIn("页", self.page.locator("#panel_four .pagination-bar").first.inner_text())
        self.page.evaluate("gotoPage('four', 1)")
        self.assertEqual(20, self.page.locator("#panel_four tbody tr").count())
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
                "local_persistence": "succeeded",
                "message": "done",
                "groups": [{**group, "state": "verified_approved", "message": "verified"}],
            }

        requests, expected = self.open_mock_local_service(job_factory)
        selected = self.page.evaluate(
            """() => {
                const group = Object.values(APPROVAL_GROUPS).find(value =>
                    value.review_mode === 'manual' && getGroupStatus(value.group_key) !== 'approved'
                );
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
        self.page.wait_for_function("approvalExecutionActive === false")

        feedback = self.page.locator("#approvalFeedback").inner_text()
        self.assertIn("本地归档已保存", feedback)
        self.assertIn("刷新页面后仍会保留", feedback)
        self.assertFalse(
            any(item["path"].startswith("/api/v1/dashboard/refresh") for item in requests)
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

    def test_verified_readback_survives_full_page_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            month = "2026-07"
            month_dir = project / "srdpm_archive" / month
            month_dir.mkdir(parents=True)
            (month_dir / "audit_report.json").write_text(
                json.dumps(
                    {
                        "month": month,
                        "platform_summary": {},
                        "missed": {},
                        "no_checkin_leave": [],
                        "hours_over": [],
                        "hours_low": [],
                        "project_mismatch": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (month_dir / "audit_report.md").write_text("# offline\n", encoding="utf-8")
            (month_dir / "raw_data.json").write_text(
                json.dumps(
                    {
                        "daily_data": {
                            "2026-07-08": {
                                "list": [
                                    {
                                        "cn_name": "测试人员A",
                                        "uid": "u-a",
                                        "children": [
                                            {
                                                "approve_id": "91001",
                                                "items": "G/TEST",
                                                "title": "离线刷新回归",
                                                "content": "仅测试本地状态持久化",
                                                "work_hours": 4,
                                                "status": "待审",
                                            }
                                        ],
                                    }
                                ]
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            old_values = (dashboard.OUT_DIR, dashboard.ARCHIVE_DIR, dashboard.OUTPUT_HTML)
            dashboard.OUT_DIR = str(project)
            dashboard.ARCHIVE_DIR = str(project / "srdpm_archive")
            dashboard.OUTPUT_HTML = str(project / "dashboard.html")
            try:
                dashboard.main()
                result = {"group": None, "rebuilt": False}

                def job_factory():
                    group = result["group"]
                    if not result["rebuilt"]:
                        save_approval_readback_entries(
                            month_dir,
                            month,
                            [
                                {
                                    "approve_id": "91001",
                                    "date": "2026-07-08",
                                    "person": "测试人员A",
                                    "user_id": "u-a",
                                    "verified_at": "2026-07-22T03:04:05Z",
                                    "source": "srdpm_readback",
                                    "plan_sha256": "a" * 64,
                                }
                            ],
                        )
                        dashboard.main()
                        result["rebuilt"] = True
                    return {
                        "job_id": "test-job-id-1234567890",
                        "status": "succeeded",
                        "outcome": "succeeded",
                        "local_persistence": "succeeded",
                        "message": "done",
                        "groups": [
                            {**group, "state": "verified_approved", "message": "verified"}
                        ],
                    }

                _requests, expected = self.open_mock_local_service(job_factory)
                selected = self.page.evaluate(
                    """() => Object.values(APPROVAL_GROUPS).find(
                        group => getGroupStatus(group.group_key) !== 'approved'
                    )"""
                )
                self.assertIsNotNone(selected)
                group_key = selected["group_key"]
                result["group"] = {
                    "group_key": group_key,
                    "date": selected["date"],
                    "person": selected["person"],
                    "id_count": len(selected["approve_ids"]),
                    "item_count": selected["item_count"],
                    "work_hours": 4,
                    "projects": ["G/TEST"],
                    "review_summary": "六、正常申报",
                    "review_mode": selected["review_mode"],
                }
                expected["id_counts"][group_key] = len(selected["approve_ids"])
                expected["groups"][group_key] = result["group"]
                self.page.evaluate(
                    """key => {
                        approvalState[key] = 'selected';
                        saveState();
                        renderCategoryNav();
                        updatePendingCount();
                    }""",
                    group_key,
                )

                self.page.locator("#btnExecute").click()
                self.page.wait_for_selector("#approvalConfirmOverlay.show")
                self.page.locator("#approvalConfirmAccept").click()
                self.page.wait_for_function(
                    "document.querySelector('#approvalFeedback').textContent.includes('刷新页面后仍会保留')"
                )
                self.page.reload(wait_until="domcontentloaded")
                self.page.wait_for_function(
                    """key => typeof getGroupStatus === 'function' &&
                        getGroupStatus(key) === 'approved'""",
                    arg=group_key,
                )

                self.assertEqual(
                    "approved",
                    self.page.evaluate("key => getGroupStatus(key)", group_key),
                )
                self.assertEqual(
                    "approved",
                    self.page.evaluate("key => APPROVAL_GROUPS[key].status", group_key),
                )
                stored_state = self.page.evaluate(
                    "key => localStorage.getItem(key)",
                    f"srdpm_approval_v3_{month}",
                )
                self.assertTrue(stored_state is None or group_key not in json.loads(stored_state))
                self.assert_no_page_errors()
            finally:
                dashboard.OUT_DIR, dashboard.ARCHIVE_DIR, dashboard.OUTPUT_HTML = old_values

    def test_old_service_without_persistence_result_gets_version_warning(self):
        result = {"group": None}

        def job_factory():
            return {
                "job_id": "test-job-id-1234567890",
                "status": "succeeded",
                "outcome": "succeeded",
                "message": "done",
                "groups": [
                    {**result["group"], "state": "verified_approved", "message": "verified"}
                ],
            }

        _requests, expected = self.open_mock_local_service(job_factory)
        selected = self.page.evaluate(
            """() => Object.values(APPROVAL_GROUPS).find(value =>
                value.review_mode === 'manual' &&
                getGroupStatus(value.group_key) !== 'approved'
            )"""
        )
        group_key = selected["group_key"]
        result["group"] = {
            "group_key": group_key,
            "date": selected["date"],
            "person": selected["person"],
            "id_count": len(selected["approve_ids"]),
            "item_count": selected["item_count"],
            "work_hours": 4,
            "projects": ["P/OLD-SERVICE"],
            "review_summary": "三、工时异常",
            "review_mode": selected["review_mode"],
        }
        expected["id_counts"][group_key] = len(selected["approve_ids"])
        expected["groups"][group_key] = result["group"]
        self.page.evaluate(
            """key => {
                approvalState[key] = 'selected';
                saveState();
                renderCategoryNav();
                updatePendingCount();
            }""",
            group_key,
        )

        self.page.locator("#btnExecute").click()
        self.page.wait_for_selector("#approvalConfirmOverlay.show")
        self.page.locator("#approvalConfirmAccept").click()
        self.page.wait_for_function(
            "document.querySelector('#approvalFeedback').textContent.includes('后台服务版本较旧')"
        )

        feedback = self.page.locator("#approvalFeedback").inner_text()
        self.assertNotIn("本地审批状态保存失败", feedback)
        self.assertIn("请勿重批", feedback)
        self.assert_no_page_errors()

    def test_partial_result_keeps_unknown_group_selected(self):
        result = {"rows": []}

        def job_factory():
            return {
                "job_id": "test-job-id-1234567890",
                "status": "failed",
                "outcome": "partial_success",
                "local_persistence": "succeeded",
                "message": "partial",
                "groups": result["rows"],
            }

        requests, expected = self.open_mock_local_service(job_factory)
        selected_groups = self.page.evaluate(
            """() => {
                const groups = Object.values(APPROVAL_GROUPS)
                    .filter(value =>
                        value.review_mode === 'manual' && getGroupStatus(value.group_key) !== 'approved'
                    ).slice(0, 2);
                for (const group of groups) approvalState[group.group_key] = 'selected';
                saveState();
                renderCategoryNav();
                updatePendingCount();
                return groups;
            }"""
        )
        self.assertEqual(2, len(selected_groups))
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
        self.page.wait_for_function("approvalExecutionActive === false")

        feedback = self.page.locator("#approvalFeedback").inner_text()
        self.assertIn("本地归档已保存", feedback)
        self.assertIn("刷新页面后仍会保留", feedback)
        self.assertFalse(
            any(item["path"].startswith("/api/v1/dashboard/refresh") for item in requests)
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
