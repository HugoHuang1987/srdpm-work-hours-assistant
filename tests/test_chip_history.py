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
        self.assertIsNone(audit.match_chip(d4, ["T963D4Z"], norm))
        self.assertEqual(
            "AM950D5",
            audit.match_chip(
                full_project_variant,
                ["AM950D5"],
                audit.build_chip_norm(["AM950D5"]),
            ),
        )

    def test_missing_history_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            with patch.object(audit, "OUT_DIR", temporary_dir):
                with self.assertRaisesRegex(ValueError, "拒绝.*遗忘旧机芯"):
                    audit.load_and_update_chip_history({"测试人员": ["AM963D5"]})


if __name__ == "__main__":
    unittest.main()
