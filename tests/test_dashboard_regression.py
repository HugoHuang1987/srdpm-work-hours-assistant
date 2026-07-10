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
        return raw, cats, groups

    def test_july_known_duplicate_and_count_regression(self):
        raw, cats, groups = self.load_month("2026-07")
        raw_rows = sum(
            len(parent.get("children", []))
            for day in raw["daily_data"].values()
            for parent in day.get("list", [])
        )
        self.assertEqual(raw_rows, 968)
        self.assertEqual(len(iter_unique_children(raw)), 484)
        self.assertEqual(len(cats["three"]["items"]), 3)
        self.assertEqual(len(cats["four"]["items"]), 1)
        self.assertEqual(len(cats["five"]["items"]), 52)
        self.assertEqual(len(cats["six"]["items"]), 396)
        self.assertEqual(dashboard.build_enhanced_stats(raw)["total_hours"], 448.8)

        summary = summarize_groups(groups)
        self.assertEqual(summary["manual_pending_groups"], 4)
        self.assertEqual(summary["manual_pending_items"], 41)
        self.assertEqual(summary["auto_pending_groups"], 50)
        self.assertEqual(summary["auto_pending_items"], 443)

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
        self.assertEqual(len(manual_ids | auto_ids), 484)
        primary_counts = {
            key: sum(group["primary_category"] == key for group in groups.values())
            for key in ("two", "three", "four", "five", "six", "seven")
        }
        self.assertEqual(sum(primary_counts.values()), 54)
        self.assertEqual(primary_counts["three"] + primary_counts["four"], 4)
        self.assertEqual(primary_counts["two"] + primary_counts["five"] + primary_counts["six"], 50)

    def test_june_platform_and_normal_rows_are_not_lost_or_doubled(self):
        raw, cats, groups = self.load_month("2026-06")
        self.assertEqual(len(iter_unique_children(raw)), 3223)
        self.assertEqual(len(cats["five"]["items"]), 228)
        self.assertEqual(len(cats["six"]["items"]), 2861)
        self.assertEqual(len(groups), 315)

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
