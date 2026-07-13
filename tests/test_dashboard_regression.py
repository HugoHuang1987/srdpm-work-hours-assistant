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


if __name__ == "__main__":
    unittest.main()
