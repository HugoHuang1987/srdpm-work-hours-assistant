from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import approval_readback_store as store


MONTH = "2026-07"
HASH_A = "a" * 64
HASH_B = "b" * 64


def verified_entry(
    approve_id: str = "1001",
    *,
    date: str = "2026-07-08",
    person: str = "测试人员A",
    user_id: str | None = "u-a",
    verified_at: str = "2026-07-22T03:04:05Z",
    source: str = "srdpm_readback",
    plan_sha256: str = HASH_A,
) -> dict[str, object]:
    return {
        "approve_id": approve_id,
        "date": date,
        "person": person,
        "user_id": user_id,
        "verified_at": verified_at,
        "source": source,
        "plan_sha256": plan_sha256,
    }


def raw_child(
    approve_id: str,
    *,
    status: str = "待审",
    title: str = "测试事项",
) -> dict[str, object]:
    return {
        "approve_id": approve_id,
        "status": status,
        "title": title,
        "work_hours": 4,
    }


def raw_data() -> dict[str, object]:
    return {
        "fetch_time": "2026-07-22T10:00:00+08:00",
        "daily_data": {
            "2026-07-08": {
                "list": [
                    {
                        "cn_name": "测试人员A",
                        "uid": "u-a",
                        "children": [raw_child("1001"), raw_child("1002")],
                    }
                ]
            }
        },
    }


class ApprovalReadbackStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.month_dir = Path(self.temporary.name) / MONTH
        self.month_dir.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @property
    def path(self) -> Path:
        return self.month_dir / store.READBACK_FILENAME

    def test_missing_file_is_a_valid_empty_month_store(self) -> None:
        loaded = store.load_approval_readback(self.month_dir, MONTH)

        self.assertEqual(
            loaded,
            {"schema_version": 1, "month": MONTH, "entries": {}},
        )
        self.assertFalse(self.path.exists())

    def test_save_persists_entries_keyed_by_approve_id_and_loads_them(self) -> None:
        saved = store.save_approval_readback_entries(
            self.month_dir,
            MONTH,
            [verified_entry(), verified_entry("1002", user_id=None, plan_sha256=HASH_B)],
        )

        self.assertEqual(set(saved["entries"]), {"1001", "1002"})
        self.assertNotIn("approve_id", saved["entries"]["1001"])
        self.assertEqual(saved["entries"]["1001"]["date"], "2026-07-08")
        self.assertEqual(saved["entries"]["1001"]["person"], "测试人员A")
        self.assertEqual(saved["entries"]["1001"]["user_id"], "u-a")
        self.assertEqual(saved["entries"]["1001"]["verified_at"], "2026-07-22T03:04:05Z")
        self.assertEqual(saved["entries"]["1001"]["source"], "srdpm_readback")
        self.assertEqual(saved["entries"]["1001"]["plan_sha256"], HASH_A)
        self.assertEqual(store.load_approval_readback(self.month_dir, MONTH), saved)

    def test_repeated_save_merges_and_can_enrich_a_missing_user_id(self) -> None:
        store.save_approval_readback_entries(
            self.month_dir,
            MONTH,
            [verified_entry(user_id=None)],
        )

        saved = store.save_approval_readback_entries(
            self.month_dir,
            MONTH,
            [
                verified_entry(
                    user_id="u-a",
                    verified_at="2026-07-22T04:05:06+00:00",
                    plan_sha256=HASH_B,
                ),
                verified_entry("1002"),
            ],
        )

        self.assertEqual(saved["entries"]["1001"]["user_id"], "u-a")
        self.assertEqual(saved["entries"]["1001"]["plan_sha256"], HASH_A)
        self.assertEqual(
            saved["entries"]["1001"]["verified_at"], "2026-07-22T03:04:05Z"
        )
        self.assertEqual(set(saved["entries"]), {"1001", "1002"})

    def test_same_approve_id_with_conflicting_identity_is_rejected(self) -> None:
        store.save_approval_readback_entries(self.month_dir, MONTH, [verified_entry()])

        conflicting_entries = (
            verified_entry(date="2026-07-09"),
            verified_entry(person="测试人员B"),
            verified_entry(user_id="u-other"),
        )
        for entry in conflicting_entries:
            with self.subTest(entry=entry):
                with self.assertRaisesRegex(
                    store.ApprovalReadbackConflictError,
                    "1001.*身份冲突",
                ):
                    store.save_approval_readback_entries(
                        self.month_dir, MONTH, [entry]
                    )

    def test_load_rejects_corrupt_json_and_schema_or_month_mismatch(self) -> None:
        invalid_payloads: list[tuple[str, object]] = [
            ("broken JSON", "{not-json"),
            (
                "wrong schema",
                {"schema_version": 2, "month": MONTH, "entries": {}},
            ),
            (
                "wrong month",
                {"schema_version": 1, "month": "2026-06", "entries": {}},
            ),
            (
                "entries is not an object",
                {"schema_version": 1, "month": MONTH, "entries": []},
            ),
            (
                "unexpected root key",
                {
                    "schema_version": 1,
                    "month": MONTH,
                    "entries": {},
                    "extra": True,
                },
            ),
        ]

        for label, payload in invalid_payloads:
            with self.subTest(label=label):
                text = payload if isinstance(payload, str) else json.dumps(payload)
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(store.ApprovalReadbackValidationError):
                    store.load_approval_readback(self.month_dir, MONTH)

    def test_load_strictly_validates_every_entry_field(self) -> None:
        valid = verified_entry()
        approve_id = str(valid.pop("approve_id"))
        invalid_entries = {
            "missing field": {key: value for key, value in valid.items() if key != "source"},
            "unexpected field": {**valid, "extra": True},
            "date outside month": {**valid, "date": "2026-06-30"},
            "bad date": {**valid, "date": "2026-07-99"},
            "empty person": {**valid, "person": ""},
            "bad user id type": {**valid, "user_id": 123},
            "timestamp without timezone": {
                **valid,
                "verified_at": "2026-07-22T03:04:05",
            },
            "empty source": {**valid, "source": ""},
            "unexpected source": {**valid, "source": "browser_selection"},
            "bad hash": {**valid, "plan_sha256": "ABC"},
        }

        for label, entry in invalid_entries.items():
            with self.subTest(label=label):
                self.path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "month": MONTH,
                            "entries": {approve_id: entry},
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(store.ApprovalReadbackValidationError):
                    store.load_approval_readback(self.month_dir, MONTH)

    def test_failed_atomic_replace_preserves_previous_store_and_cleans_temp(self) -> None:
        original = store.save_approval_readback_entries(
            self.month_dir, MONTH, [verified_entry()]
        )
        original_bytes = self.path.read_bytes()

        with patch.object(store.os, "replace", side_effect=OSError("offline replace failure")):
            with self.assertRaisesRegex(
                store.ApprovalReadbackWriteError,
                "原子写入失败",
            ):
                store.save_approval_readback_entries(
                    self.month_dir, MONTH, [verified_entry("1002")]
                )

        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertEqual(store.load_approval_readback(self.month_dir, MONTH), original)
        self.assertEqual(list(self.month_dir.glob(".approval_readback.*.tmp")), [])

    def test_write_rejects_payload_larger_than_the_subsequent_read_limit(self) -> None:
        with patch.object(store, "MAX_READBACK_BYTES", 100):
            with self.assertRaisesRegex(
                store.ApprovalReadbackValidationError,
                "序列化后超过",
            ):
                store.save_approval_readback_entries(
                    self.month_dir,
                    MONTH,
                    [verified_entry(person="很长的测试人员名称")],
                )

        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.month_dir.glob(".approval_readback.*.tmp")), [])

    def test_overlay_marks_only_exact_existing_identity_on_a_deep_copy(self) -> None:
        original = raw_data()
        untouched = copy.deepcopy(original)
        readback = {
            "schema_version": 1,
            "month": MONTH,
            "entries": {
                "1001": {
                    key: value
                    for key, value in verified_entry().items()
                    if key != "approve_id"
                },
                "9999": {
                    key: value
                    for key, value in verified_entry("9999").items()
                    if key != "approve_id"
                },
            },
        }

        overlaid = store.overlay_approval_readback(original, readback)
        children = overlaid["daily_data"]["2026-07-08"]["list"][0]["children"]

        self.assertIsNot(overlaid, original)
        self.assertEqual(original, untouched)
        self.assertEqual(children[0]["status"], "通过")
        self.assertEqual(children[1]["status"], "待审")
        self.assertEqual(len(children), 2)

    def test_overlay_allows_match_when_only_one_side_has_user_id(self) -> None:
        original = raw_data()
        del original["daily_data"]["2026-07-08"]["list"][0]["uid"]
        entry = verified_entry()
        approve_id = str(entry.pop("approve_id"))

        overlaid = store.overlay_approval_readback(
            original,
            {"schema_version": 1, "month": MONTH, "entries": {approve_id: entry}},
        )

        child = overlaid["daily_data"]["2026-07-08"]["list"][0]["children"][0]
        self.assertEqual(child["status"], "通过")

    def test_overlay_normalizes_person_like_the_approval_plan(self) -> None:
        original = raw_data()
        original["daily_data"]["2026-07-08"]["list"][0]["cn_name"] = "  测试人员A  "
        entry = verified_entry()
        approve_id = str(entry.pop("approve_id"))

        overlaid = store.overlay_approval_readback(
            original,
            {"schema_version": 1, "month": MONTH, "entries": {approve_id: entry}},
        )

        child = overlaid["daily_data"]["2026-07-08"]["list"][0]["children"][0]
        self.assertEqual(child["status"], "通过")

    def test_overlay_rejects_same_id_with_conflicting_raw_identity(self) -> None:
        entry = verified_entry()
        approve_id = str(entry.pop("approve_id"))
        readback = {
            "schema_version": 1,
            "month": MONTH,
            "entries": {approve_id: entry},
        }
        conflicts = []

        wrong_date = raw_data()
        wrong_date["daily_data"]["2026-07-09"] = wrong_date["daily_data"].pop(
            "2026-07-08"
        )
        conflicts.append(wrong_date)

        wrong_person = raw_data()
        wrong_person["daily_data"]["2026-07-08"]["list"][0]["cn_name"] = "测试人员B"
        conflicts.append(wrong_person)

        wrong_user = raw_data()
        wrong_user["daily_data"]["2026-07-08"]["list"][0]["uid"] = "u-other"
        conflicts.append(wrong_user)

        for raw in conflicts:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(
                    store.ApprovalReadbackConflictError,
                    "1001.*身份冲突",
                ):
                    store.overlay_approval_readback(raw, readback)

    def test_overlay_rejects_an_explicit_non_approved_terminal_status(self) -> None:
        original = raw_data()
        original["daily_data"]["2026-07-08"]["list"][0]["children"][0][
            "status"
        ] = "拒绝"
        entry = verified_entry()
        approve_id = str(entry.pop("approve_id"))

        with self.assertRaisesRegex(
            store.ApprovalReadbackConflictError,
            "原始状态.*冲突",
        ):
            store.overlay_approval_readback(
                original,
                {"schema_version": 1, "month": MONTH, "entries": {approve_id: entry}},
            )

    def test_overlay_rejects_a_corrupt_readback_instead_of_trusting_it(self) -> None:
        with self.assertRaises(store.ApprovalReadbackValidationError):
            store.overlay_approval_readback(
                raw_data(),
                {"schema_version": 1, "month": MONTH, "entries": []},
            )


if __name__ == "__main__":
    unittest.main()
