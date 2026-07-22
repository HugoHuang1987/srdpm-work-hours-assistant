import copy
import unittest

import fetch_and_audit as audit
import wiki_project_mapping as wiki_mapping


def _source(month, attachment_id):
    compact = month.replace("-", "")
    year, month_number = month.split("-")
    return {
        "kind": "confluence_attachment",
        "page_id": wiki_mapping.WIKI_PAGE_ID,
        "attachment_id": str(attachment_id),
        "filename": f"团队成员项目负荷_新拆分-{compact}20.xlsx",
        "updated_at": f"{year}-{month_number}-20T10:00:00+08:00",
        "sha256": "a" * 64,
        "sheet": f"计算负荷用-{year}-{int(month_number)}月",
    }


def _mapping(month, person_projects, attachment_id="1"):
    chips = sorted(
        {
            wiki_mapping.mapping_entry_chip(entry)
            for entries in person_projects.values()
            for entry in entries
        }
    )
    return {
        "schema_version": 2,
        "source": _source(month, attachment_id),
        "mapping": [{"row": 3, "customer": "G", "project": "P", "chip": chips[0] if chips else "AM963D5", "spm": "甲", "bsp": "", "diag": ""}],
        "person_projects": copy.deepcopy(person_projects),
        "all_people": sorted(person_projects),
        "all_chips": chips or ["AM963D5"],
    }


class AuthorizationRetentionPolicyTests(unittest.TestCase):
    def test_authorization_rule_snapshot_separates_current_reasonable_and_expired(self):
        march = _mapping(
            "2026-03",
            {"甲": ["G/T963D4", "G/MT9026L"]},
        )
        april = wiki_mapping.reconcile_authorization_retention(
            march,
            _mapping("2026-04", {"甲": ["G/MT9026L"]}, "2"),
        )
        july = wiki_mapping.reconcile_authorization_retention(
            april,
            _mapping("2026-07", {"甲": ["G/AM966D5"]}, "3"),
        )

        snapshot = audit.build_authorization_rule_snapshot(july, 2026, 8)
        rules = snapshot["people"]["甲"]

        self.assertEqual(1, snapshot["schema_version"])
        self.assertEqual("2026-08", snapshot["work_month"])
        self.assertEqual(
            ["AM966D5"],
            [entry["chip"] for entry in rules["current"]],
        )
        self.assertEqual(
            [("MT9026L", "2026-09")],
            [
                (entry["chip"], entry["valid_through_month"])
                for entry in rules["historical_reasonable"]
            ],
        )
        self.assertEqual(
            [("AM963D4", "2026-06")],
            [
                (entry["chip"], entry["valid_through_month"])
                for entry in rules["historical_expired"]
            ],
        )

    def test_current_rule_wins_over_expired_episode_for_same_canonical_chip(self):
        march = _mapping("2026-03", {"甲": ["G/T963D5"]})
        april = wiki_mapping.reconcile_authorization_retention(
            march,
            _mapping("2026-04", {"乙": ["G/AM966D5"]}, "2"),
        )
        august = wiki_mapping.reconcile_authorization_retention(
            april,
            _mapping(
                "2026-08",
                {"甲": ["G/AM963D5"], "乙": ["G/AM966D5"]},
                "3",
            ),
        )

        rules = audit.build_authorization_rule_snapshot(august, 2026, 8)[
            "people"
        ]["甲"]

        self.assertEqual(["AM963D5"], [entry["chip"] for entry in rules["current"]])
        self.assertEqual([], rules["historical_reasonable"])
        self.assertEqual([], rules["historical_expired"])

    def test_removed_chip_is_valid_for_removal_month_and_two_following_months(self):
        previous = _mapping("2026-06", {"甲": ["G/AM963D5"]})
        current = _mapping("2026-07", {"乙": ["G/AM966D5"]}, "2")

        reconciled = wiki_mapping.reconcile_authorization_retention(previous, current)

        self.assertEqual(3, reconciled["schema_version"])
        self.assertNotIn("甲", reconciled["person_projects"])
        records = reconciled["authorization_retention"]["records"]
        self.assertEqual(1, len(records))
        self.assertEqual("2026-07", records[0]["removed_month"])
        self.assertEqual("2026-09", records[0]["valid_through_month"])

        for year, month in ((2026, 6), (2026, 7), (2026, 8), (2026, 9)):
            active = audit.active_authorization_retention(reconciled, year, month)
            self.assertEqual("AM963D5", active["甲"][0]["chip"])
        self.assertNotIn("甲", audit.active_authorization_retention(reconciled, 2026, 10))
        expired = audit.expired_authorization_retention(reconciled, 2026, 10)
        self.assertEqual("AM963D5", expired["甲"][0]["chip"])
        norm = audit.build_chip_norm(reconciled["all_chips"] + ["AM963D5"])
        result = audit.check_project_ownership(
            "甲",
            "G",
            "项目（T963D5机芯）",
            audit.extract_chip_candidates("项目（T963D5机芯）"),
            reconciled["person_projects"],
            norm,
            ["AM963D5", "AM966D5"],
            audit.build_chip_norm(["AM963D5", "AM966D5"]),
            expired_grace_records=expired,
        )
        self.assertFalse(result[0])
        self.assertIn("历史过期", result[1])
        self.assertIn("2026-09", result[1])

    def test_cross_year_boundary_uses_calendar_months(self):
        previous = _mapping("2026-11", {"甲": ["AM963D5"]})
        current = _mapping("2026-12", {"乙": ["AM966D5"]}, "2")

        reconciled = wiki_mapping.reconcile_authorization_retention(previous, current)

        record = reconciled["authorization_retention"]["records"][0]
        self.assertEqual("2027-02", record["valid_through_month"])
        self.assertIn("甲", audit.active_authorization_retention(reconciled, 2027, 2))
        self.assertNotIn("甲", audit.active_authorization_retention(reconciled, 2027, 3))

    def test_repeated_absence_does_not_extend_and_second_removal_adds_episode(self):
        june = _mapping("2026-06", {"甲": ["AM963D5"]})
        july = wiki_mapping.reconcile_authorization_retention(
            june, _mapping("2026-07", {"乙": ["AM966D5"]}, "2")
        )
        august = wiki_mapping.reconcile_authorization_retention(
            july, _mapping("2026-08", {"乙": ["AM966D5"]}, "3")
        )
        self.assertEqual(
            ["2026-09"],
            [
                record["valid_through_month"]
                for record in august["authorization_retention"]["records"]
                if record["person"] == "甲"
            ],
        )

        readded = wiki_mapping.reconcile_authorization_retention(
            august,
            _mapping("2026-09", {"甲": ["T963D5"], "乙": ["AM966D5"]}, "4"),
        )
        removed_again = wiki_mapping.reconcile_authorization_retention(
            readded, _mapping("2026-10", {"乙": ["AM966D5"]}, "5")
        )
        records = [
            record
            for record in removed_again["authorization_retention"]["records"]
            if record["person"] == "甲"
        ]
        self.assertEqual(["2026-07", "2026-10"], [r["removed_month"] for r in records])
        self.assertEqual(["2026-09", "2026-12"], [r["valid_through_month"] for r in records])
        self.assertEqual("2026-09", records[0]["readded_month"])
        self.assertEqual("2026-09", records[1]["valid_from_month"])

    def test_second_removal_does_not_authorize_gap_before_readdition(self):
        june = _mapping("2026-06", {"甲": ["AM963D5"]})
        july = wiki_mapping.reconcile_authorization_retention(
            june, _mapping("2026-07", {"乙": ["AM966D5"]}, "2")
        )
        readded_in_november = wiki_mapping.reconcile_authorization_retention(
            july,
            _mapping("2026-11", {"甲": ["AM963D5"], "乙": ["AM966D5"]}, "3"),
        )
        removed_in_december = wiki_mapping.reconcile_authorization_retention(
            readded_in_november,
            _mapping("2026-12", {"乙": ["AM966D5"]}, "4"),
        )

        october_active, october_expired = audit.authorization_retention_status(
            removed_in_december, 2026, 10
        )
        self.assertNotIn("甲", october_active)
        self.assertEqual("AM963D5", october_expired["甲"][0]["chip"])
        self.assertIn(
            "甲",
            audit.active_authorization_retention(removed_in_december, 2026, 11),
        )

    def test_same_month_remove_readd_remove_does_not_leave_stale_lower_bound(self):
        june = _mapping("2026-06", {"甲": ["AM963D5"]})
        july_removed = wiki_mapping.reconcile_authorization_retention(
            june, _mapping("2026-07", {"乙": ["AM966D5"]}, "2")
        )
        july_readded = wiki_mapping.reconcile_authorization_retention(
            july_removed,
            _mapping("2026-07", {"甲": ["AM963D5"], "乙": ["AM966D5"]}, "3"),
        )
        july_removed_again = wiki_mapping.reconcile_authorization_retention(
            july_readded, _mapping("2026-07", {"乙": ["AM966D5"]}, "4")
        )
        july_record = july_removed_again["authorization_retention"]["records"][0]
        self.assertNotIn("readded_month", july_record)

        november_readded = wiki_mapping.reconcile_authorization_retention(
            july_removed_again,
            _mapping("2026-11", {"甲": ["AM963D5"], "乙": ["AM966D5"]}, "5"),
        )
        december_removed = wiki_mapping.reconcile_authorization_retention(
            november_readded,
            _mapping("2026-12", {"乙": ["AM966D5"]}, "6"),
        )
        october_active, october_expired = audit.authorization_retention_status(
            december_removed, 2026, 10
        )
        self.assertNotIn("甲", october_active)
        self.assertIn("甲", october_expired)
        records = december_removed["authorization_retention"]["records"]
        self.assertEqual("2026-11", records[-1]["valid_from_month"])

    def test_same_month_older_wiki_source_cannot_replace_newer_mapping(self):
        previous = _mapping("2026-07", {"甲": ["AM963D5"]}, "20")
        previous["source"]["filename"] = "团队成员项目负荷_新拆分-20260720.xlsx"
        previous["source"]["updated_at"] = "2026-07-20T17:06:07+08:00"
        current = _mapping("2026-07", {"乙": ["AM966D5"]}, "3")
        current["source"]["filename"] = "团队成员项目负荷_新拆分-20260703.xlsx"
        current["source"]["updated_at"] = "2026-07-03T10:00:00+08:00"

        with self.assertRaises(wiki_mapping.WikiMappingValidationError):
            wiki_mapping.reconcile_authorization_retention(previous, current)

    def test_customer_and_t_am_format_changes_do_not_create_false_removal(self):
        previous = _mapping("2026-06", {"甲": ["AM963D5", "预研/MT9603/L"]})
        current = _mapping("2026-07", {"甲": ["G/T963D5", "G/MT9603/L"]}, "2")

        reconciled = wiki_mapping.reconcile_authorization_retention(previous, current)

        self.assertEqual([], reconciled["authorization_retention"]["records"])

    def test_grace_matches_removed_global_chip_but_keeps_d4_d5_distinct(self):
        previous = _mapping("2026-06", {"甲": ["AM963D4"]})
        reconciled = wiki_mapping.reconcile_authorization_retention(
            previous, _mapping("2026-07", {"乙": ["AM966D5"]}, "2")
        )
        active = audit.active_authorization_retention(reconciled, 2026, 9)
        effective_norm = audit.build_chip_norm(
            reconciled["all_chips"] + [record["chip"] for record in active["甲"]]
        )

        d4_result = audit.check_project_ownership(
            "甲",
            "G",
            "项目（T963D4机芯）",
            audit.extract_chip_candidates("项目（T963D4机芯）"),
            reconciled["person_projects"],
            effective_norm,
            ["AM963D4", "AM963D5", "AM966D5"],
            audit.build_chip_norm(["AM963D4", "AM963D5", "AM966D5"]),
            grace_records=active,
        )
        self.assertTrue(d4_result[0])
        self.assertIn("两个月宽限", d4_result[1])
        self.assertIn("2026-09", d4_result[1])

        d5_result = audit.check_project_ownership(
            "甲",
            "G",
            "项目（T963D5机芯）",
            audit.extract_chip_candidates("项目（T963D5机芯）"),
            reconciled["person_projects"],
            effective_norm,
            ["AM963D4", "AM963D5", "AM966D5"],
            audit.build_chip_norm(["AM963D4", "AM963D5", "AM966D5"]),
            grace_records=active,
        )
        self.assertFalse(d5_result[0])


if __name__ == "__main__":
    unittest.main()
