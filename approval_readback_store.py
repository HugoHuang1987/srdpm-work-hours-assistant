"""Durable, identity-bound SRDPM approval read-back records.

The browser dashboard is generated from archived ``raw_data.json`` snapshots.
Immediately after SRDPM has confirmed an approval, that snapshot can still show
the former pending status until the next successful read-only refresh.  This
module stores only the verified read-back facts and can overlay them onto a copy
of an archived raw payload.  It never creates work records and never treats a
browser selection as approval evidence.
"""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from collections.abc import Iterable, Mapping, MutableMapping
from datetime import date, datetime
from pathlib import Path
from typing import Any


READBACK_FILENAME = "approval_readback.json"
READBACK_SCHEMA_VERSION = 1
READBACK_SOURCE = "srdpm_readback"
MAX_READBACK_BYTES = 5 * 1024 * 1024
MAX_READBACK_ENTRIES = 100_000

_MONTH_PATTERN = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_APPROVE_ID_PATTERN = re.compile(r"^[1-9][0-9]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ROOT_KEYS = frozenset({"schema_version", "month", "entries"})
_ENTRY_KEYS = frozenset(
    {"date", "person", "user_id", "verified_at", "source", "plan_sha256"}
)
_SAVE_ENTRY_KEYS = _ENTRY_KEYS | {"approve_id"}
_PENDING_STATUSES = frozenset({"", "0", "待审", "待审核", "pending"})
_APPROVED_STATUSES = frozenset({"通过", "approved", "pass", "passed"})


class ApprovalReadbackError(RuntimeError):
    """Base class for approval read-back persistence failures."""


class ApprovalReadbackValidationError(ApprovalReadbackError):
    """The read-back file or caller-supplied entry violates the schema."""


class ApprovalReadbackConflictError(ApprovalReadbackError):
    """One approve ID is associated with conflicting record identities."""


class ApprovalReadbackWriteError(ApprovalReadbackError):
    """The validated store could not be atomically published."""


class _DuplicateJsonKeyError(ValueError):
    pass


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(f"重复字段 {key}")
        result[key] = value
    return result


def _validate_month(month: Any, *, location: str = "month") -> str:
    if not isinstance(month, str) or not _MONTH_PATTERN.fullmatch(month):
        raise ApprovalReadbackValidationError(f"{location} 必须是合法 YYYY-MM")
    return month


def _validate_text(
    value: Any,
    *,
    location: str,
    max_length: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise ApprovalReadbackValidationError(f"{location} 必须是合法非空字符串")
    return value


def _validate_timestamp(value: Any, *, location: str) -> str:
    text = _validate_text(value, location=location, max_length=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovalReadbackValidationError(
            f"{location} 必须是带时区的 ISO 8601 时间"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApprovalReadbackValidationError(
            f"{location} 必须是带时区的 ISO 8601 时间"
        )
    return text


def _validate_entry(
    approve_id: Any,
    entry: Any,
    *,
    month: str,
    location: str,
) -> dict[str, Any]:
    if not isinstance(approve_id, str) or not _APPROVE_ID_PATTERN.fullmatch(approve_id):
        raise ApprovalReadbackValidationError(f"{location}.approve_id 不合法")
    if not isinstance(entry, Mapping):
        raise ApprovalReadbackValidationError(f"{location} 必须是对象")
    if set(entry) != _ENTRY_KEYS:
        raise ApprovalReadbackValidationError(
            f"{location} 字段必须严格为 {sorted(_ENTRY_KEYS)}"
        )

    record_date = entry["date"]
    if not isinstance(record_date, str) or not _DATE_PATTERN.fullmatch(record_date):
        raise ApprovalReadbackValidationError(f"{location}.date 必须是合法 YYYY-MM-DD")
    try:
        date.fromisoformat(record_date)
    except ValueError as exc:
        raise ApprovalReadbackValidationError(
            f"{location}.date 必须是合法 YYYY-MM-DD"
        ) from exc
    if record_date[:7] != month:
        raise ApprovalReadbackValidationError(
            f"{location}.date 不属于存档月份 {month}"
        )

    person = _validate_text(entry["person"], location=f"{location}.person", max_length=256)
    user_id = entry["user_id"]
    if user_id is not None:
        user_id = _validate_text(
            user_id, location=f"{location}.user_id", max_length=256
        )
    verified_at = _validate_timestamp(
        entry["verified_at"], location=f"{location}.verified_at"
    )
    source = _validate_text(
        entry["source"], location=f"{location}.source", max_length=128
    )
    if source != READBACK_SOURCE:
        raise ApprovalReadbackValidationError(
            f"{location}.source 必须为 {READBACK_SOURCE}"
        )
    plan_sha256 = entry["plan_sha256"]
    if not isinstance(plan_sha256, str) or not _SHA256_PATTERN.fullmatch(plan_sha256):
        raise ApprovalReadbackValidationError(
            f"{location}.plan_sha256 必须是小写 SHA-256"
        )

    return {
        "date": record_date,
        "person": person,
        "user_id": user_id,
        "verified_at": verified_at,
        "source": source,
        "plan_sha256": plan_sha256,
    }


def validate_approval_readback(
    payload: Any,
    *,
    expected_month: str | None = None,
) -> dict[str, Any]:
    """Validate and return a detached canonical read-back payload."""

    if not isinstance(payload, Mapping):
        raise ApprovalReadbackValidationError("审批回读存档根节点必须是对象")
    if set(payload) != _ROOT_KEYS:
        raise ApprovalReadbackValidationError(
            f"审批回读存档字段必须严格为 {sorted(_ROOT_KEYS)}"
        )
    schema_version = payload["schema_version"]
    if type(schema_version) is not int or schema_version != READBACK_SCHEMA_VERSION:
        raise ApprovalReadbackValidationError(
            f"schema_version 必须为 {READBACK_SCHEMA_VERSION}"
        )
    month = _validate_month(payload["month"])
    if expected_month is not None:
        expected = _validate_month(expected_month, location="expected_month")
        if month != expected:
            raise ApprovalReadbackValidationError(
                f"审批回读存档月份 {month} 与预期月份 {expected} 不一致"
            )
    raw_entries = payload["entries"]
    if not isinstance(raw_entries, Mapping):
        raise ApprovalReadbackValidationError("entries 必须是以 approve_id 为键的对象")
    if len(raw_entries) > MAX_READBACK_ENTRIES:
        raise ApprovalReadbackValidationError("entries 超过安全上限")

    entries: dict[str, dict[str, Any]] = {}
    for approve_id, entry in raw_entries.items():
        location = f"entries[{approve_id!r}]"
        entries[approve_id] = _validate_entry(
            approve_id, entry, month=month, location=location
        )
    return {
        "schema_version": READBACK_SCHEMA_VERSION,
        "month": month,
        "entries": entries,
    }


def empty_approval_readback(month: str) -> dict[str, Any]:
    return {
        "schema_version": READBACK_SCHEMA_VERSION,
        "month": _validate_month(month),
        "entries": {},
    }


def load_approval_readback(month_dir: str | os.PathLike[str], month: str) -> dict[str, Any]:
    """Load one month's store; a missing file is an empty valid store."""

    expected_month = _validate_month(month)
    path = Path(month_dir) / READBACK_FILENAME
    if not path.exists():
        return empty_approval_readback(expected_month)
    try:
        if not path.is_file():
            raise ApprovalReadbackValidationError("审批回读存档路径不是普通文件")
        if path.stat().st_size > MAX_READBACK_BYTES:
            raise ApprovalReadbackValidationError("审批回读存档超过 5 MiB 安全上限")
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8")
        payload = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except ApprovalReadbackValidationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKeyError) as exc:
        raise ApprovalReadbackValidationError(
            f"审批回读存档损坏或无法读取：{path}"
        ) from exc
    return validate_approval_readback(payload, expected_month=expected_month)


def _identities_conflict(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if left["date"] != right["date"] or left["person"] != right["person"]:
        return True
    left_user = left.get("user_id")
    right_user = right.get("user_id")
    return bool(left_user and right_user and left_user != right_user)


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    temporary_path = path.parent / f".approval_readback.{uuid.uuid4().hex}.tmp"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
        if len(serialized.encode("utf-8")) > MAX_READBACK_BYTES:
            raise ApprovalReadbackValidationError(
                "审批回读存档序列化后超过 5 MiB 安全上限"
            )
        with temporary_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        raise ApprovalReadbackWriteError(
            f"审批回读存档原子写入失败：{path}"
        ) from exc
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def save_approval_readback_entries(
    month_dir: str | os.PathLike[str],
    month: str,
    entries: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Merge verified entries and atomically publish the monthly store.

    ``entries`` is a sequence of exact objects containing ``approve_id`` plus
    the six persisted identity/evidence fields.  Reusing an ID for a different
    date, person, or non-empty user ID is rejected before any write.
    """

    expected_month = _validate_month(month)
    if isinstance(entries, (str, bytes, Mapping)) or not isinstance(entries, Iterable):
        raise ApprovalReadbackValidationError("待保存 entries 必须是对象序列")
    incoming = list(entries)
    if len(incoming) > MAX_READBACK_ENTRIES:
        raise ApprovalReadbackValidationError("待保存 entries 超过安全上限")

    validated_incoming: list[tuple[str, dict[str, Any]]] = []
    for index, raw_entry in enumerate(incoming):
        location = f"待保存 entries[{index}]"
        if not isinstance(raw_entry, Mapping):
            raise ApprovalReadbackValidationError(f"{location} 必须是对象")
        if set(raw_entry) != _SAVE_ENTRY_KEYS:
            raise ApprovalReadbackValidationError(
                f"{location} 字段必须严格为 {sorted(_SAVE_ENTRY_KEYS)}"
            )
        approve_id = raw_entry["approve_id"]
        persisted_entry = {key: raw_entry[key] for key in _ENTRY_KEYS}
        validated_incoming.append(
            (
                approve_id,
                _validate_entry(
                    approve_id,
                    persisted_entry,
                    month=expected_month,
                    location=location,
                ),
            )
        )

    current = load_approval_readback(month_dir, expected_month)
    merged = copy.deepcopy(current["entries"])
    for approve_id, entry in validated_incoming:
        previous = merged.get(approve_id)
        if previous is not None and _identities_conflict(previous, entry):
            raise ApprovalReadbackConflictError(
                f"approve_id {approve_id} 身份冲突，拒绝覆盖审批回读存档"
            )
        if previous is not None:
            # The first verified timestamp and plan digest are immutable audit
            # evidence.  A later identical read-back may only enrich a formerly
            # unavailable stable user ID.
            if not previous.get("user_id") and entry.get("user_id"):
                previous = {**previous, "user_id": entry["user_id"]}
            merged[approve_id] = previous
        else:
            merged[approve_id] = entry

    result = validate_approval_readback(
        {
            "schema_version": READBACK_SCHEMA_VERSION,
            "month": expected_month,
            "entries": merged,
        },
        expected_month=expected_month,
    )
    if validated_incoming:
        _atomic_write(Path(month_dir) / READBACK_FILENAME, result)
    return result


def _raw_user_id(child: Mapping[str, Any], parent: Mapping[str, Any]) -> str | None:
    for container in (child, parent):
        for key in ("user_id", "uid"):
            value = container.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def overlay_approval_readback(
    raw_data: Mapping[str, Any],
    readback: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a copied raw payload with exact verified records marked passed.

    A stored ID that is present under another date/person, or under a different
    non-empty user ID, is treated as an identity conflict rather than silently
    overlaid.  Stored IDs absent from the raw snapshot are ignored.
    """

    validated = validate_approval_readback(readback)
    if not isinstance(raw_data, Mapping):
        raise ApprovalReadbackValidationError("raw_data 根节点必须是对象")
    if not validated["entries"]:
        return dict(raw_data)
    result = copy.deepcopy(dict(raw_data))
    daily_data = result.get("daily_data")
    if daily_data is None:
        return result
    if not isinstance(daily_data, Mapping):
        raise ApprovalReadbackValidationError("raw_data.daily_data 必须是对象")

    seen_identities: dict[str, dict[str, str | None]] = {}
    for raw_date, day in daily_data.items():
        if not isinstance(day, Mapping):
            continue
        parents = day.get("list", [])
        if not isinstance(parents, list):
            continue
        for parent in parents:
            if not isinstance(parent, Mapping):
                continue
            parent_person = parent.get("cn_name")
            children = parent.get("children", [])
            if not isinstance(children, list):
                continue
            for child in children:
                if not isinstance(child, MutableMapping):
                    continue
                raw_approve_id = child.get("approve_id")
                if raw_approve_id is None:
                    continue
                approve_id = str(raw_approve_id).strip()
                stored = validated["entries"].get(approve_id)
                if stored is None:
                    continue
                person_value = child.get("cn_name", parent_person)
                person = str(person_value or "").strip()
                raw_identity: dict[str, str | None] = {
                    "date": raw_date if isinstance(raw_date, str) else str(raw_date),
                    "person": person,
                    "user_id": _raw_user_id(child, parent),
                }

                previous_raw = seen_identities.get(approve_id)
                if previous_raw is not None and _identities_conflict(
                    previous_raw, raw_identity
                ):
                    raise ApprovalReadbackConflictError(
                        f"approve_id {approve_id} 在 raw_data 中存在身份冲突"
                    )
                if previous_raw is None:
                    seen_identities[approve_id] = raw_identity
                elif not previous_raw.get("user_id") and raw_identity.get("user_id"):
                    seen_identities[approve_id] = raw_identity

                if _identities_conflict(stored, raw_identity):
                    raise ApprovalReadbackConflictError(
                        f"approve_id {approve_id} 与审批回读存档身份冲突"
                    )
                raw_status = child.get("status")
                normalized_status = str(raw_status or "").strip()
                folded_status = normalized_status.casefold()
                if (
                    normalized_status not in _PENDING_STATUSES
                    and folded_status not in _PENDING_STATUSES
                    and normalized_status not in _APPROVED_STATUSES
                    and folded_status not in _APPROVED_STATUSES
                ):
                    raise ApprovalReadbackConflictError(
                        f"approve_id {approve_id} 的原始状态与审批回读存档冲突"
                    )
                child["status"] = "通过"

    return result


__all__ = [
    "ApprovalReadbackConflictError",
    "ApprovalReadbackError",
    "ApprovalReadbackValidationError",
    "ApprovalReadbackWriteError",
    "READBACK_FILENAME",
    "READBACK_SCHEMA_VERSION",
    "READBACK_SOURCE",
    "empty_approval_readback",
    "load_approval_readback",
    "overlay_approval_readback",
    "save_approval_readback_entries",
    "validate_approval_readback",
]
