import unittest

from fetch_and_audit import dedupe_parent_records


class FetchBoundaryDedupeTests(unittest.TestCase):
    def test_same_ids_from_overlapping_statuses_are_kept_once(self):
        records = [
            {
                "id": "parent-pending",
                "cn_name": "甲",
                "children": [
                    {"approve_id": "101", "status": "待审", "items": "P1"},
                    {"approve_id": "102", "status": "待审", "items": "P2"},
                ],
            },
            {
                "id": "parent-other-status",
                "cn_name": "甲",
                "children": [
                    {"approve_id": "101", "status": "待审", "items": "P1"},
                    {"approve_id": "102", "status": "待审", "items": "P2"},
                ],
            },
        ]

        merged = dedupe_parent_records(records)
        children = [child for parent in merged for child in parent["children"]]
        self.assertEqual([child["approve_id"] for child in children], ["101", "102"])

    def test_approved_copy_wins_when_duplicate_statuses_disagree(self):
        records = [
            {"id": "a", "cn_name": "乙", "children": [{"approve_id": "201", "status": "待审"}]},
            {"id": "b", "cn_name": "乙", "children": [{"approve_id": "201", "status": "通过"}]},
        ]
        merged = dedupe_parent_records(records)
        children = [child for parent in merged for child in parent["children"]]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["status"], "通过")


if __name__ == "__main__":
    unittest.main()
