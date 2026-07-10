"""SRDPM 审批数据的纯函数模型。

本模块不访问网络、不读凭据，也不执行审批。所有可执行审批必须先被
规范化为“人员 + 日期”唯一整单，避免按看板行重复提交或误批同日异常项。
"""

from __future__ import annotations

from collections import OrderedDict
from hashlib import sha256
from typing import Any, Iterable, Mapping


APPROVED_STATUSES = {"通过", "approved", "pass", "passed"}


def is_approved_status(value: Any) -> bool:
    """Return True only for an explicit server-approved status."""
    if value is None:
        return False
    text = str(value).strip()
    return text in APPROVED_STATUSES or text.casefold() in APPROVED_STATUSES


def normalize_approve_ids(values: Iterable[Any]) -> list[str]:
    """Deduplicate non-empty approval IDs while preserving first-seen order."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and "," in value:
            candidates = value.split(",")
        else:
            candidates = [value]
        for candidate in candidates:
            approve_id = str(candidate).strip()
            if not approve_id or approve_id in seen:
                continue
            seen.add(approve_id)
            result.append(approve_id)
    return result


def make_group_key(date: Any, person: Any) -> str:
    """Create a stable, non-index-based key for one person-day approval group."""
    canonical = f"{str(date).strip()}\0{str(person).strip()}".encode("utf-8")
    return "grp_" + sha256(canonical).hexdigest()[:20]


def _fallback_child_key(date: str, person: str, child: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        "fallback",
        date,
        person,
        str(child.get("items") or child.get("items_name") or ""),
        str(child.get("title") or ""),
        str(child.get("content") or "").strip(),
        str(child.get("work_hours") or ""),
    )


def iter_unique_children(raw_data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return unique child records from an SRDPM raw archive.

    SRDPM status queries can overlap. A real ``approve_id`` is therefore the
    primary identity. Records without an ID use stable business fields as a
    fallback. If duplicate copies disagree, an explicit approved status wins.
    """
    unique: "OrderedDict[tuple[Any, ...], dict[str, Any]]" = OrderedDict()
    daily_data = (raw_data or {}).get("daily_data", {})

    for date, day_info in daily_data.items():
        for parent in day_info.get("list", []):
            person = str(parent.get("cn_name") or "").strip()
            user_id = parent.get("uid") or parent.get("user_id") or parent.get("id")
            for child in parent.get("children", []):
                approve_id = str(child.get("approve_id") or "").strip()
                identity = ("approve_id", approve_id) if approve_id else _fallback_child_key(
                    str(date), person, child
                )
                record = {
                    "date": str(date),
                    "person": person,
                    "user_id": user_id,
                    "parent": parent,
                    "child": dict(child),
                    "approve_id": approve_id,
                }
                if identity not in unique:
                    unique[identity] = record
                    continue

                existing = unique[identity]
                if is_approved_status(child.get("status")) and not is_approved_status(
                    existing["child"].get("status")
                ):
                    unique[identity] = record

    return list(unique.values())


def build_approval_groups(
    raw_data: Mapping[str, Any] | None,
    manual_pairs: Iterable[tuple[Any, Any]] = (),
) -> dict[str, dict[str, Any]]:
    """Build mutually exclusive person-day approval groups.

    ``manual_pairs`` contains ``(date, person)`` values derived from every
    manual anomaly category. Manual always wins over automatic classification.
    """
    manual = {(str(date), str(person).strip()) for date, person in manual_pairs}
    groups: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    for record in iter_unique_children(raw_data):
        date = record["date"]
        person = record["person"]
        group_key = make_group_key(date, person)
        if group_key not in groups:
            groups[group_key] = {
                "group_key": group_key,
                "date": date,
                "person": person,
                "user_id": record.get("user_id"),
                "approve_ids": [],
                "item_count": 0,
                "review_mode": "manual" if (date, person) in manual else "auto",
                "status": "pending",
                "source_statuses": [],
            }

        group = groups[group_key]
        group["item_count"] += 1
        group["source_statuses"].append(record["child"].get("status"))
        if record["approve_id"]:
            group["approve_ids"].append(record["approve_id"])

    for group in groups.values():
        group["approve_ids"] = normalize_approve_ids(group["approve_ids"])
        statuses = group.pop("source_statuses")
        group["status"] = (
            "approved"
            if statuses and all(is_approved_status(status) for status in statuses)
            else "pending"
        )
        group["pending_item_count"] = 0 if group["status"] == "approved" else group["item_count"]

    return dict(groups)


def manual_pairs_from_categories(categories: Mapping[str, Any]) -> set[tuple[str, str]]:
    """Collect every person-day mentioned in the two manual anomaly categories."""
    result: set[tuple[str, str]] = set()
    for category_key in ("three", "four"):
        for item in categories.get(category_key, {}).get("items", []):
            date = str(item.get("date") or "")
            person = str(item.get("person") or "").strip()
            if date and person:
                result.add((date, person))
    return result


def assign_primary_categories(
    categories: Mapping[str, Any], groups: Mapping[str, dict[str, Any]]
) -> None:
    """Assign every actionable group to exactly one primary dashboard category.

    The same person-day can contain platform and normal rows, or multiple anomaly
    views. Primary ownership keeps navigation and bulk-action counts additive.
    """
    group_keys_by_category: dict[str, set[str]] = {}
    for category_key in ("two", "three", "four", "five", "six"):
        group_keys_by_category[category_key] = {
            make_group_key(item.get("date", ""), item.get("person", ""))
            for item in categories.get(category_key, {}).get("items", [])
            if item.get("date") and item.get("person")
        }

    for group_key, group in groups.items():
        if group.get("review_mode") == "manual":
            priority = ("three", "four")
        else:
            # Leave/travel is most specific. Mixed platform+normal work is owned
            # by normal申报; only platform-only days are owned by category five.
            priority = ("two", "six", "five")
        group["primary_category"] = next(
            (key for key in priority if group_key in group_keys_by_category[key]),
            "seven",
        )


def attach_groups_to_categories(
    categories: Mapping[str, Any], groups: Mapping[str, Mapping[str, Any]]
) -> None:
    """Attach stable group metadata to display rows in-place."""
    for category_key in ("two", "three", "four", "five", "six"):
        for item in categories.get(category_key, {}).get("items", []):
            group_key = make_group_key(item.get("date", ""), item.get("person", ""))
            group = groups.get(group_key)
            if not group:
                continue
            item["approval_group_key"] = group_key
            item["approve_ids"] = ",".join(group.get("approve_ids", []))
            item["review_mode"] = group.get("review_mode", "manual")
            item["primary_category"] = group.get("primary_category", category_key)
            item["is_primary_approval_view"] = item["primary_category"] == category_key
            item["status"] = group.get("status", "pending")


def summarize_groups(groups: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    """Return one canonical count source for every dashboard surface."""
    result = {
        "manual_pending_groups": 0,
        "manual_pending_items": 0,
        "auto_pending_groups": 0,
        "auto_pending_items": 0,
        "approved_groups": 0,
    }
    for group in groups.values():
        if group.get("status") == "approved":
            result["approved_groups"] += 1
            continue
        mode = "manual" if group.get("review_mode") == "manual" else "auto"
        result[f"{mode}_pending_groups"] += 1
        result[f"{mode}_pending_items"] += len(group.get("approve_ids", []))
    return result
