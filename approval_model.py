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


def make_group_key(
    date: Any, person: Any, approve_ids: Iterable[Any] | None = None
) -> str:
    """Create an opaque, stable key for an approval selection.

    The legacy two-argument form identifies a whole person-day and remains for
    backward-compatible offline consumers.  A category row must pass its exact
    approval IDs: the resulting key then represents *only that row's approved
    scope*, never every entry submitted by the person on the same day.
    """

    date_text = str(date).strip()
    person_text = str(person).strip()
    if approve_ids is None:
        canonical_text = f"day\0{date_text}\0{person_text}"
    else:
        normalized = sorted(normalize_approve_ids(approve_ids), key=str)
        canonical_text = f"selection\0{date_text}\0{person_text}\0" + "\0".join(
            normalized
        )
    return "grp_" + sha256(canonical_text.encode("utf-8")).hexdigest()[:20]


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
            # `id` 是人员当天审批父记录的 ID，真实归档中每天都会变化，不能
            # 当作人员身份传给实时 API。只有明确的 uid/user_id 才可用于严格匹配；
            # 缺失时由执行器按人员姓名做兼容查询并以待审 ID 全等校验兜底。
            user_id = parent.get("uid") or parent.get("user_id")
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


def _build_person_day_groups(
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


def _build_category_selection_groups(
    raw_data: Mapping[str, Any] | None,
    categories: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build exact-row approval selections from category data.

    The audit categories are the only source of the selectable scope.  A row
    without a unique approval ID deliberately receives no approval group rather
    than falling back to the person's full day.  This prevents a project
    anomaly from approving unrelated normal or platform rows.
    """

    records_by_id = {
        record["approve_id"]: record
        for record in iter_unique_children(raw_data)
        if record.get("approve_id")
    }
    groups: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    manual_categories = {"three", "four"}

    for category_key in ("two", "three", "four", "five", "six"):
        category = categories.get(category_key, {})
        for item in category.get("items", []):
            if not isinstance(item, dict):
                continue
            date = str(item.get("date") or "")
            person = str(item.get("person") or "").strip()
            approve_ids = normalize_approve_ids([item.get("approve_ids") or ""])
            if not date or not person or not approve_ids:
                item["approval_unavailable_reason"] = (
                    item.get("approval_unavailable_reason")
                    or "未能唯一定位到可审批的 SRDPM 明细"
                )
                continue

            records = [records_by_id.get(approve_id) for approve_id in approve_ids]
            if any(record is None for record in records):
                item["approval_unavailable_reason"] = "审批明细已不在当前原始归档中"
                continue
            if any(
                record["date"] != date or record["person"] != person
                for record in records
                if record is not None
            ):
                item["approval_unavailable_reason"] = "审批明细与当前行身份不一致"
                continue

            user_ids = {
                str(record.get("user_id") or "").strip()
                for record in records
                if record is not None and str(record.get("user_id") or "").strip()
            }
            if len(user_ids) > 1:
                item["approval_unavailable_reason"] = "审批明细的人员身份不一致"
                continue

            group_key = make_group_key(date, person, approve_ids)
            item["approval_group_key"] = group_key
            item.pop("approval_unavailable_reason", None)
            group = groups.get(group_key)
            if group is None:
                statuses = [
                    record["child"].get("status")
                    for record in records
                    if record is not None
                ]
                group = {
                    "group_key": group_key,
                    "date": date,
                    "person": person,
                    "user_id": next(iter(user_ids), None),
                    "approve_ids": approve_ids,
                    "item_count": len(approve_ids),
                    "review_mode": "manual"
                    if category_key in manual_categories
                    else "auto",
                    "status": (
                        "approved"
                        if statuses and all(is_approved_status(status) for status in statuses)
                        else "pending"
                    ),
                    "source_categories": [category_key],
                    "scope": "整日" if category_key == "three" else "明细",
                }
                groups[group_key] = group
            else:
                if category_key not in group["source_categories"]:
                    group["source_categories"].append(category_key)
                if category_key in manual_categories:
                    group["review_mode"] = "manual"
                if category_key == "three":
                    group["scope"] = "整日"

    for group in groups.values():
        group["source_categories"].sort()
        group["pending_item_count"] = (
            0 if group["status"] == "approved" else group["item_count"]
        )
    return dict(groups)


def build_approval_groups(
    raw_data: Mapping[str, Any] | None,
    manual_pairs: Iterable[tuple[Any, Any]] = (),
    *,
    categories: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build approval selections.

    Runtime callers pass ``categories`` so every selectable row is rebuilt as
    an exact ID whitelist.  The legacy person-day model is retained only for
    callers that do not supply category data, keeping older offline exports and
    their strict full-day validation behavior compatible.
    """

    if categories is not None:
        return _build_category_selection_groups(raw_data, categories)
    return _build_person_day_groups(raw_data, manual_pairs)


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
        source_categories = group.get("source_categories")
        if isinstance(source_categories, list):
            priority = ("three", "four", "two", "six", "five")
            group["primary_category"] = next(
                (key for key in priority if key in source_categories), "seven"
            )
            continue
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
            group_key = item.get("approval_group_key") or make_group_key(
                item.get("date", ""), item.get("person", "")
            )
            group = groups.get(group_key)
            if not group:
                continue
            item["approval_group_key"] = group_key
            item["approve_ids"] = ",".join(group.get("approve_ids", []))
            item["review_mode"] = group.get("review_mode", "manual")
            item["primary_category"] = group.get("primary_category", category_key)
            item["is_primary_approval_view"] = item["primary_category"] == category_key
            item["status"] = group.get("status", "pending")
            item["approval_scope"] = group.get("scope", "明细")


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
