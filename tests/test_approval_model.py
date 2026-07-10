import unittest

from approval_model import (
    assign_primary_categories,
    attach_groups_to_categories,
    build_approval_groups,
    iter_unique_children,
    make_group_key,
    normalize_approve_ids,
    summarize_groups,
)


def parent(person, children, uid="u1"):
    return {"cn_name": person, "uid": uid, "children": children}


def child(approve_id, status="待审", project="P1", title="开发"):
    return {
        "approve_id": approve_id,
        "status": status,
        "items": project,
        "title": title,
        "content": "内容",
        "work_hours": "8",
    }


class ApprovalModelTests(unittest.TestCase):
    def test_duplicate_status_results_are_collapsed_by_approve_id(self):
        raw = {
            "daily_data": {
                "2026-07-01": {
                    "list": [
                        parent("甲", [child("11"), child("12")]),
                        parent("甲", [child("11"), child("12")]),
                    ]
                }
            }
        }
        records = iter_unique_children(raw)
        self.assertEqual([r["approve_id"] for r in records], ["11", "12"])

        groups = build_approval_groups(raw)
        group = groups[make_group_key("2026-07-01", "甲")]
        self.assertEqual(group["approve_ids"], ["11", "12"])
        self.assertEqual(group["item_count"], 2)

    def test_manual_pair_overrides_automatic_group(self):
        raw = {"daily_data": {"2026-07-02": {"list": [parent("乙", [child("21")])]}}}
        groups = build_approval_groups(raw, {("2026-07-02", "乙")})
        group = next(iter(groups.values()))
        self.assertEqual(group["review_mode"], "manual")
        self.assertEqual(group["status"], "pending")

    def test_only_all_server_approved_children_make_group_approved(self):
        raw = {
            "daily_data": {
                "2026-07-03": {
                    "list": [parent("丙", [child("31", "通过"), child("32", "待审")])]
                },
                "2026-07-04": {
                    "list": [parent("丙", [child("41", "通过"), child("42", "通过")])]
                },
            }
        }
        groups = build_approval_groups(raw)
        self.assertEqual(groups[make_group_key("2026-07-03", "丙")]["status"], "pending")
        self.assertEqual(groups[make_group_key("2026-07-04", "丙")]["status"], "approved")

    def test_duplicate_copy_prefers_explicit_approved_status(self):
        raw = {
            "daily_data": {
                "2026-07-05": {
                    "list": [parent("丁", [child("51", "待审")]), parent("丁", [child("51", "通过")])]
                }
            }
        }
        group = next(iter(build_approval_groups(raw).values()))
        self.assertEqual(group["status"], "approved")

    def test_category_rows_share_one_stable_group(self):
        raw = {"daily_data": {"2026-07-06": {"list": [parent("戊", [child("61"), child("62")])]}}}
        cats = {
            "three": {"items": [{"date": "2026-07-06", "person": "戊"}]},
            "four": {"items": [{"date": "2026-07-06", "person": "戊"}]},
            "two": {"items": []},
            "five": {"items": []},
            "six": {"items": []},
        }
        groups = build_approval_groups(raw, {("2026-07-06", "戊")})
        assign_primary_categories(cats, groups)
        attach_groups_to_categories(cats, groups)
        self.assertEqual(
            cats["three"]["items"][0]["approval_group_key"],
            cats["four"]["items"][0]["approval_group_key"],
        )
        self.assertEqual(cats["three"]["items"][0]["approve_ids"], "61,62")
        self.assertTrue(cats["three"]["items"][0]["is_primary_approval_view"])
        self.assertFalse(cats["four"]["items"][0]["is_primary_approval_view"])

    def test_auto_group_has_one_primary_category_when_views_overlap(self):
        raw = {"daily_data": {"2026-07-09": {"list": [parent("辛", [child("91")])]}}}
        row = {"date": "2026-07-09", "person": "辛"}
        cats = {
            "two": {"items": []},
            "three": {"items": []},
            "four": {"items": []},
            "five": {"items": [dict(row)]},
            "six": {"items": [dict(row)]},
        }
        groups = build_approval_groups(raw)
        assign_primary_categories(cats, groups)
        group = next(iter(groups.values()))
        self.assertEqual(group["primary_category"], "six")

    def test_summary_counts_groups_and_unique_items_separately(self):
        raw = {
            "daily_data": {
                "2026-07-07": {"list": [parent("己", [child("71"), child("72")])]},
                "2026-07-08": {"list": [parent("庚", [child("81")])]},
            }
        }
        groups = build_approval_groups(raw, {("2026-07-07", "己")})
        summary = summarize_groups(groups)
        self.assertEqual(summary["manual_pending_groups"], 1)
        self.assertEqual(summary["manual_pending_items"], 2)
        self.assertEqual(summary["auto_pending_groups"], 1)
        self.assertEqual(summary["auto_pending_items"], 1)

    def test_normalize_ids_handles_csv_and_preserves_order(self):
        self.assertEqual(normalize_approve_ids(["2,1", "2", 3, ""]), ["2", "1", "3"])


if __name__ == "__main__":
    unittest.main()
