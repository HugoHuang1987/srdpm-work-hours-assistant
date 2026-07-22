import copy
import unittest
from pathlib import Path

import build_multi_month_dashboard as dashboard
from approval_model import (
    assign_primary_categories,
    attach_groups_to_categories,
    build_approval_groups,
    iter_unique_children,
    manual_pairs_from_categories,
    summarize_groups,
)


class CurrentArchiveRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        archive = Path(dashboard.ARCHIVE_DIR)
        if not (archive / "2026-07" / "raw_data.json").exists():
            raise unittest.SkipTest("本地敏感回归归档未提供")

    def load_month(self, month):
        audit, raw, md = dashboard.load_month_audit(month)
        cats = dashboard.build_category_data(audit, md, raw)
        groups = build_approval_groups(raw, manual_pairs_from_categories(cats))
        assign_primary_categories(cats, groups)
        attach_groups_to_categories(cats, groups)
        dashboard.append_uncovered_pending_items(cats, groups, raw)
        return raw, cats, groups

    def test_uncovered_pending_detail_is_shown_in_other_pending(self):
        audit = {
            "platform_summary": {},
            "missed": {},
            "no_checkin_leave": [],
            "hours_over": [],
            "hours_low": [],
            "project_mismatch": [],
        }
        raw = {
            "daily_data": {
                "2026-07-01": {
                    "list": [
                        {
                            "cn_name": "离线测试人员",
                            "children": [
                                {
                                    "approve_id": "uncovered-pending-1",
                                    "items": "YF-CP-17",
                                    "title": "未分类平台项",
                                    "content": "离线测试",
                                    "work_hours": 1,
                                    "status": "待审",
                                }
                            ],
                        }
                    ]
                }
            }
        }
        cats = dashboard.build_category_data(audit, "", raw)
        groups = build_approval_groups(
            raw, manual_pairs_from_categories(cats), categories=cats
        )
        assign_primary_categories(cats, groups)
        attach_groups_to_categories(cats, groups)
        dashboard.append_uncovered_pending_items(cats, groups, raw)

        self.assertEqual([], cats["five"]["items"])
        self.assertEqual(1, len(cats["seven"]["items"]))
        item = cats["seven"]["items"][0]
        self.assertEqual("2026-07-01", item["date"])
        self.assertEqual("离线测试人员", item["person"])
        self.assertEqual("pending", item["status"])
        self.assertNotIn("approve_ids", item)

    def test_ambiguous_duplicate_details_all_approved_show_approved_without_ids(self):
        audit = {
            "platform_summary": {},
            "missed": {},
            "no_checkin_leave": [],
            "hours_over": [],
            "hours_low": [],
            "project_mismatch": [{
                "date": "2026-07-02",
                "person": "离线测试人员",
                "items": "P-01",
                "title": "项目归属异常",
                "content": "重复归档明细",
                "work_hours": 1,
                "chip_candidates": ["MT026D6S2AT"],
                "allowed_chips": ["MT9026"],
                "reason": "测试异常",
            }],
        }
        duplicate = {
            "items": "P-01",
            "title": "项目归属异常",
            "content": "重复归档明细",
            "work_hours": 1,
            "status": "通过",
        }
        raw = {
            "daily_data": {
                "2026-07-02": {
                    "list": [{
                        "cn_name": "离线测试人员",
                        "children": [
                            {"approve_id": "duplicate-approved-1", **duplicate},
                            {"approve_id": "duplicate-approved-2", **duplicate},
                        ],
                    }]
                }
            }
        }
        cats = dashboard.build_category_data(audit, "", raw)
        item = cats["four"]["items"][0]
        self.assertEqual("approved", item["status"])
        self.assertEqual("", item["approve_ids"])
        self.assertIn("多条", item["approval_unavailable_reason"])

    def test_month_rule_snapshot_is_copied_to_three_and_four_without_changing_approval_scope(self):
        base_audit = {
            "month": "2026-08",
            "platform_summary": {},
            "missed": {},
            "no_checkin_leave": [],
            "hours_over": [{
                "date": "2026-08-03",
                "person": "甲",
                "reported": 8,
                "checked": 7,
                "leave_hours": 0,
                "effective": 8,
                "ratio": 8 / 7,
            }],
            "hours_low": [],
            "project_mismatch": [{
                "date": "2026-08-03",
                "person": "甲",
                "customer": "G",
                "items": "P-01",
                "title": "项目归属异常",
                "content": "离线规则列测试",
                "work_hours": 8,
                "chip_candidates": ["AM963D5"],
                "allowed_chips": ["AM966D5", "MT9026L"],
                "reason": "测试异常",
            }],
        }
        raw = {
            "daily_data": {
                "2026-08-03": {
                    "list": [{
                        "cn_name": "甲",
                        "children": [{
                            "approve_id": "rule-columns-1",
                            "items": "P-01",
                            "title": "项目归属异常",
                            "content": "离线规则列测试",
                            "work_hours": 8,
                            "status": "待审",
                        }],
                    }]
                }
            }
        }
        audit_with_rules = copy.deepcopy(base_audit)
        audit_with_rules["authorization_rules"] = {
            "schema_version": 1,
            "work_month": "2026-08",
            "people": {
                "甲": {
                    "current": [{"chip": "AM966D5", "canonical_chip": "AM966D5", "customers": ["G"]}],
                    "historical_reasonable": [{
                        "chip": "MT9026L",
                        "canonical_chip": "MT9026L",
                        "removed_month": "2026-07",
                        "valid_through_month": "2026-09",
                    }],
                    "historical_expired": [{
                        "chip": "AM963D4",
                        "canonical_chip": "AM963D4",
                        "removed_month": "2026-04",
                        "valid_through_month": "2026-06",
                    }],
                }
            },
        }

        cats_without = dashboard.build_category_data(base_audit, "", raw)
        cats_with = dashboard.build_category_data(audit_with_rules, "", raw)

        for key in ("three", "four"):
            item = cats_with[key]["items"][0]
            self.assertTrue(item["authorization_rules_recorded"])
            self.assertTrue(item["authorization_person_listed"])
            self.assertEqual(["AM966D5"], [entry["chip"] for entry in item["current_rules"]])
            self.assertEqual(
                [("MT9026L", "2026-09")],
                [(entry["chip"], entry["valid_through_month"]) for entry in item["historical_reasonable_rules"]],
            )
            self.assertEqual(
                [("AM963D4", "2026-06")],
                [(entry["chip"], entry["valid_through_month"]) for entry in item["historical_expired_rules"]],
            )

        for key in ("three", "four"):
            missing = cats_without[key]["items"][0]
            self.assertFalse(missing["authorization_rules_recorded"])
            self.assertFalse(missing["authorization_person_listed"])
            self.assertEqual([], missing["current_rules"])
            self.assertEqual([], missing["historical_reasonable_rules"])
            self.assertEqual([], missing["historical_expired_rules"])

        self.assertEqual(
            manual_pairs_from_categories(cats_without),
            manual_pairs_from_categories(cats_with),
        )
        groups_without = build_approval_groups(raw, categories=cats_without)
        groups_with = build_approval_groups(raw, categories=cats_with)
        self.assertEqual(groups_without, groups_with)

        audit_with_rules["authorization_rules"]["people"] = {}
        unlisted = dashboard.build_category_data(audit_with_rules, "", raw)
        for key in ("three", "four"):
            self.assertTrue(unlisted[key]["items"][0]["authorization_rules_recorded"])
            self.assertFalse(unlisted[key]["items"][0]["authorization_person_listed"])

    def test_july_dedup_and_assignment_invariants(self):
        raw, cats, groups = self.load_month("2026-07")
        raw_rows = sum(
            len(parent.get("children", []))
            for day in raw["daily_data"].values()
            for parent in day.get("list", [])
        )
        unique_children = list(iter_unique_children(raw))
        unique_ids = {
            str(record["approve_id"])
            for record in unique_children
            if str(record.get("approve_id") or "").strip()
        }
        self.assertGreater(raw_rows, 0)
        self.assertLessEqual(len(unique_children), raw_rows)
        self.assertEqual(len(unique_children), len(unique_ids))
        self.assertGreater(dashboard.build_enhanced_stats(raw)["total_hours"], 0)

        summary = summarize_groups(groups)
        self.assertEqual(
            len(groups),
            summary["manual_pending_groups"]
            + summary["auto_pending_groups"]
            + summary["approved_groups"],
        )

        manual_ids = {
            approve_id
            for group in groups.values()
            if group["review_mode"] == "manual"
            for approve_id in group["approve_ids"]
        }
        auto_ids = {
            approve_id
            for group in groups.values()
            if group["review_mode"] == "auto"
            for approve_id in group["approve_ids"]
        }
        self.assertFalse(manual_ids & auto_ids)
        self.assertEqual(manual_ids | auto_ids, unique_ids)
        primary_counts = {
            key: sum(group["primary_category"] == key for group in groups.values())
            for key in ("two", "three", "four", "five", "six", "seven")
        }
        manual_group_count = sum(
            group["review_mode"] == "manual" for group in groups.values()
        )
        auto_group_count = sum(
            group["review_mode"] == "auto" for group in groups.values()
        )
        self.assertEqual(sum(primary_counts.values()), len(groups))
        self.assertEqual(
            manual_group_count,
            primary_counts["three"] + primary_counts["four"],
        )
        self.assertEqual(
            auto_group_count,
            primary_counts["two"] + primary_counts["five"] + primary_counts["six"],
        )

    def test_june_platform_and_normal_rows_are_not_lost_or_doubled(self):
        raw, cats, groups = self.load_month("2026-06")
        unique_children = list(iter_unique_children(raw))
        unique_ids = {str(record["approve_id"]) for record in unique_children}
        group_ids = {
            approve_id for group in groups.values() for approve_id in group["approve_ids"]
        }
        platform_keys = {
            (
                item.get("date"),
                item.get("person"),
                item.get("project"),
                item.get("title"),
                item.get("content"),
                item.get("hours"),
            )
            for item in cats["five"]["items"]
        }
        normal_rows = {
            (
                item.get("date"),
                item.get("person"),
                item.get("project"),
                item.get("title"),
                item.get("content"),
                item.get("hours"),
            )
            for item in cats["six"]["items"]
        }
        self.assertGreater(len(unique_children), 0)
        self.assertEqual(group_ids, unique_ids)
        self.assertEqual(len(platform_keys), len(cats["five"]["items"]))
        self.assertTrue(normal_rows)
        self.assertEqual(len(normal_rows), len(cats["six"]["items"]))

    def test_every_person_day_group_is_represented_in_a_visible_category(self):
        _, cats, groups = self.load_month("2026-07")
        represented = {
            item.get("approval_group_key")
            for key in ("two", "three", "four", "five", "six")
            for item in cats[key]["items"]
            if item.get("approval_group_key")
        }
        self.assertEqual(set(groups), represented)

    def test_summary_category_counts_each_raw_detail_once(self):
        raw, cats, _ = self.load_month("2026-07")
        unique_children = list(iter_unique_children(raw))
        summary_rows = cats["zero"]["items"]
        self.assertEqual(len(unique_children), len(summary_rows))
        raw_hours = sum(
            float(record["child"].get("work_hours", 0) or 0)
            for record in unique_children
        )
        summary_hours = sum(float(item.get("hours", 0) or 0) for item in summary_rows)
        self.assertAlmostEqual(raw_hours, summary_hours, places=6)
        buckets = {item["chip"] for item in summary_rows}
        self.assertIn("公共事务/平台", buckets)
        self.assertIn("请假/出差/休假", buckets)

    def test_t963d5_project_is_identified_in_normal_and_summary_views(self):
        audit_data = {
            "platform_summary": {},
            "missed": {},
            "no_checkin_leave": [],
            "hours_over": [],
            "hours_low": [],
            "project_mismatch": [],
        }
        project_name = (
            "预研 43D7100U 1AM96DA6ATAT FTV INX V430DJ1-Q01 "
            "D2(NOVA/FITI) TV 预研项目 （T963D5机芯）"
        )
        raw = {
            "daily_data": {
                "2026-07-01": {
                    "list": [{
                        "cn_name": "离线测试人员",
                        "children": [{
                            "approve_id": "offline-t963d5-1",
                            "items": "TVY25-N-004",
                            "project_name": project_name,
                            "title": "项目事务",
                            "content": "离线测试",
                            "work_hours": 2,
                            "status": "待审",
                        }],
                    }]
                }
            }
        }

        cats = dashboard.build_category_data(audit_data, "", raw)
        self.assertEqual("AM963D5", cats["six"]["items"][0]["chip"])
        self.assertEqual("AM963D5", cats["zero"]["items"][0]["chip"])
        group_names = [name for name, _stats in dashboard.build_enhanced_stats(raw)["group_ranking"]]
        self.assertIn("未分类/AM963D5", group_names)


if __name__ == "__main__":
    unittest.main()
