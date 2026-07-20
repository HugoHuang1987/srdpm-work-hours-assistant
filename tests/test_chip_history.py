import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fetch_and_audit as audit


class ChipHistoryTests(unittest.TestCase):
    def test_history_is_union_only_and_does_not_restore_old_permission(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            history_path = root / audit.CHIP_HISTORY_NAME
            history_path.write_text(
                json.dumps({"schema_version": 1, "chips": ["T963D4Z"]}),
                encoding="utf-8",
            )
            current_projects = {"测试人员": ["AM963D5"]}
            with patch.object(audit, "OUT_DIR", str(root)):
                history = audit.load_and_update_chip_history(current_projects)

            self.assertEqual(["AM963D5", "T963D4Z"], history)
            saved = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual(["AM963D5", "T963D4Z"], saved["chips"])

            candidates = audit.extract_chip_candidates("T963D4Z")
            ok, reason, matched, allowed, codes = audit.check_project_ownership(
                "测试人员",
                "G",
                "T963D4Z",
                candidates,
                current_projects,
                audit.build_chip_norm(["AM963D5"]),
                history,
                audit.build_chip_norm(history),
            )
            self.assertFalse(ok)
            self.assertEqual([], matched)
            self.assertEqual(["AM963D5"], allowed)
            self.assertEqual(["T963D4Z"], codes)
            self.assertIn("识别到历史机芯: T963D4Z", reason)
            self.assertIn("不在当前允许范围", reason)

    def test_explicit_d4_and_d5_revisions_never_match(self):
        norm = audit.build_chip_norm(["AM963D4", "AM963D5", "T963D4Z"])
        d4 = ("AM963D4",) + audit.chip_normalize("AM963D4")
        d5 = ("AM963D5",) + audit.chip_normalize("AM963D5")
        short_d = ("AM96D",) + audit.chip_normalize("AM96D")
        full_project_variant = ("AM95DD6ATAT",) + audit.chip_normalize("AM95DD6ATAT")

        self.assertEqual("AM963D4", audit.match_chip(d4, ["AM963D4"], norm))
        self.assertIsNone(audit.match_chip(d4, ["AM963D5"], norm))
        self.assertIsNone(audit.match_chip(d5, ["AM963D4"], norm))
        self.assertEqual("AM963D5", audit.match_chip(d5, ["AM963D5"], norm))
        self.assertEqual("AM963D5", audit.match_chip(short_d, ["AM963D5"], norm))
        self.assertEqual("T963D4Z", audit.match_chip(d4, ["T963D4Z"], norm))
        self.assertEqual(
            "AM950D5",
            audit.match_chip(
                full_project_variant,
                ["AM950D5"],
                audit.build_chip_norm(["AM950D5"]),
            ),
        )

    def test_t963_and_am963_are_aliases_but_explicit_revisions_stay_separate(self):
        title = (
            "预研 43D7100U 1AM96DA6ATAT FTV INX V430DJ1-Q01 "
            "D2(NOVA/FITI) TV 预研项目 （T963D5机芯）"
        )
        candidates = audit.extract_chip_candidates(title)
        selected = audit.select_chip_candidates_for_matching(candidates)
        norm = audit.build_chip_norm(["AM963D4", "AM963D5", "T963D4Z"])

        self.assertEqual(["T963D5"], [candidate[0] for candidate in selected])
        self.assertEqual("AM963D5", audit.match_chip(selected[0], ["AM963D5"], norm))
        self.assertIsNone(audit.match_chip(selected[0], ["AM963D4"], norm))
        self.assertIsNone(audit.match_chip(selected[0], ["T963D4Z"], norm))

        ok, _reason, matched, _allowed, _codes = audit.check_project_ownership(
            "离线测试人员",
            "G",
            title,
            candidates,
            {"离线测试人员": ["AM963D5"]},
            norm,
        )
        self.assertTrue(ok)
        self.assertEqual(["AM963D5"], matched)

        wrong_revision_ok, *_rest = audit.check_project_ownership(
            "离线测试人员",
            "G",
            title,
            candidates,
            {"离线测试人员": ["T963D4Z"]},
            norm,
        )
        self.assertFalse(wrong_revision_ok)

    def test_verified_t966_alias_matches_am966_with_same_explicit_revision(self):
        title = (
            "预研 55D6500 1AM966B6S2AT FTV CSOT TV 预研项目 "
            "（T966D5机芯）"
        )
        candidates = audit.extract_chip_candidates(title)
        selected = audit.select_chip_candidates_for_matching(candidates)
        norm = audit.build_chip_norm(["AM966D5", "AM966D4"])

        self.assertEqual(["T966D5"], [candidate[0] for candidate in selected])
        self.assertEqual("AM966D5", audit.match_chip(selected[0], ["AM966D5"], norm))
        self.assertIsNone(audit.match_chip(selected[0], ["AM966D4"], norm))

        t950d5 = ("T950D5",) + audit.chip_normalize("T950D5")
        norm_950 = audit.build_chip_norm(["AM950D5", "AM950D4"])
        self.assertEqual("AM950D5", audit.match_chip(t950d5, ["AM950D5"], norm_950))
        self.assertIsNone(audit.match_chip(t950d5, ["AM950D4"], norm_950))

    def test_chip_immediately_after_chinese_text_is_extracted(self):
        candidates = audit.extract_chip_candidates(
            "预研项目（新开MT9603L机芯匹配D3200模具）"
        )
        self.assertIn("MT9603L", [candidate[0] for candidate in candidates])

    def test_chip_alias_with_slash_is_not_mistaken_for_customer_separator(self):
        allowed = ["预研/MT9603/L"]
        norm = audit.build_chip_norm(["MT9603/L"])
        candidate = ("MT9603",) + audit.chip_normalize("MT9603")

        self.assertEqual("MT9603/L", audit.match_chip(candidate, allowed, norm))
        self.assertEqual({"MT9603/L"}, audit._chip_codes_from_person_projects({"测试人员": allowed}))

    def test_current_unassigned_wiki_chip_is_still_added_to_history(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            history_path = root / audit.CHIP_HISTORY_NAME
            history_path.write_text(
                json.dumps({"schema_version": 1, "chips": ["T963D4Z"]}),
                encoding="utf-8",
            )
            with patch.object(audit, "OUT_DIR", str(root)):
                history = audit.load_and_update_chip_history(
                    {"测试人员": ["G/AM963"]},
                    ["AM963", "MT9690"],
                )

            self.assertEqual(["AM963", "MT9690", "T963D4Z"], history)

    def test_missing_history_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            with patch.object(audit, "OUT_DIR", temporary_dir):
                with self.assertRaisesRegex(ValueError, "拒绝.*遗忘旧机芯"):
                    audit.load_and_update_chip_history({"测试人员": ["AM963D5"]})


if __name__ == "__main__":
    unittest.main()
