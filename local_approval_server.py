"""仅供本机使用的 SRDPM 看板与审批桥接服务。

浏览器永远只提交月份和稳定的 ``group_key``。服务端会从当前本地归档重新
构建可审批白名单和 :class:`ApprovalPlan`，不会信任浏览器提供的人员、日期或
审批 ID。真实审批继续复用 ``execute_plan`` 的全量预检、提交前复检和提交后
逐 ID 回读。

安全边界：

* 固定监听 127.0.0.1，不提供修改监听地址的参数；
* 页面与 API 同源，严格校验 Host、Origin、Sec-Fetch-Site 和随机 CSRF；
* prepare 只产生两分钟有效的一次性票据，execute 原子消费票据；
* 每个后台任务创建并关闭自己的 SRDPMClient；
* 凭据仍只由 SRDPMClient.from_env() 从环境变量读取；
* 服务内任务锁之外，执行器还使用 Windows named mutex 与命令行真实审批互斥。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from apply_approval_plan import (
    ApprovalPlan,
    PlanSummary,
    PlanValidationError,
    execute_plan,
    parse_plan,
    summarize_plan,
)
from approval_model import (
    assign_primary_categories,
    build_approval_groups,
    iter_unique_children,
    manual_pairs_from_categories,
)
from build_multi_month_dashboard import build_category_data
from srdpm_client import SRDPMClient
from windows_credential_store import WindowsCredentialStore


PROJECT_DIR = Path(__file__).resolve().parent
DASHBOARD_NAME = "工时审批看板_多月.html"
SERVICE_BUILD_ID = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
GROUP_KEY_PATTERN = re.compile(r"^grp_[0-9a-f]{20}$")
JOB_PATH_PATTERN = re.compile(r"^/api/v1/approval/jobs/([A-Za-z0-9_-]{20,100})$")
REFRESH_JOB_PATH_PATTERN = re.compile(
    r"^/api/v1/dashboard/refresh/jobs/([A-Za-z0-9_-]{20,100})$"
)

LISTEN_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
TICKET_TTL_SECONDS = 120
MAX_HTTP_BODY_BYTES = 256 * 1024
MAX_ARCHIVE_JSON_BYTES = 100 * 1024 * 1024
MAX_SELECTED_GROUPS = 200
MAX_TOTAL_IDS = 5_000
MAX_IDS_PER_GROUP = 500
ACTIONABLE_CATEGORIES = frozenset({"two", "three", "four", "five", "six"})


class ServiceError(RuntimeError):
    """可安全返回给页面的预期错误。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RebuiltPlan:
    plan: ApprovalPlan
    summary: PlanSummary
    # Browser-facing keys identify exact category selections, not a whole day.
    group_keys: tuple[str, ...]
    group_rows: tuple[dict[str, Any], ...]
    identity_by_person_date: Mapping[tuple[str, str], tuple[str, str, str | None]]
    group_keys_by_identity: Mapping[tuple[str, str, str | None], tuple[str, ...]]


@dataclass
class PreparedTicket:
    token: str
    month: str
    group_keys: tuple[str, ...]
    plan_sha256: str
    expires_monotonic: float
    expires_at_utc: str
    used: bool = False


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _short_display_text(value: Any, limit: int = 100) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _impact_summaries(raw_data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return display summaries keyed by an exact approval ID."""

    summaries: dict[str, dict[str, Any]] = {}
    for record in iter_unique_children(raw_data):
        approve_id = str(record.get("approve_id") or "").strip()
        if not approve_id:
            continue
        child = record["child"]
        try:
            hours = float(child.get("work_hours", 0) or 0)
        except (TypeError, ValueError):
            hours = 0.0
        if not math.isfinite(hours):
            hours = 0.0
        project = _short_display_text(
            child.get("items")
            or child.get("items_name")
            or child.get("project_name")
            or "未标注项目"
        )
        summaries[approve_id] = {
            "work_hours": round(hours, 2),
            "projects": [project] if project else [],
            "project_count": 1 if project else 0,
        }
    return summaries


def confirm_approval_with_windows(summary: PlanSummary) -> bool:
    """要求当前 Windows 桌面上的用户对不可撤回提交做原生确认。"""

    if os.name != "nt":
        raise RuntimeError("Windows user-presence confirmation is unavailable")
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MessageBoxW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
    ]
    user32.MessageBoxW.restype = ctypes.c_int
    verification_code = summary.sha256[:12].upper()
    message = (
        "即将执行不可撤回的 SRDPM 真实审批。\n\n"
        f"月份：{summary.month}\n"
        f"执行人员日期批次：{summary.group_count}\n"
        f"待审 ID：{summary.id_count}\n"
        f"清单校验码：{verification_code}\n\n"
        "只有当你刚刚在 SRDPM 工时审批看板核对过相同校验码时，才点击“是”。\n"
        "如果这个弹窗不是你主动触发的，请点击“否”。"
    )
    flags = 0x00000004 | 0x00000030 | 0x00000100 | 0x00010000 | 0x00040000
    return user32.MessageBoxW(
        None, message, "SRDPM 不可撤回审批 — Windows 安全确认", flags
    ) == 6


def _read_json_file(path: Path, label: str, *, required: bool) -> Any:
    if not path.is_file():
        if required:
            raise ServiceError(409, "archive_incomplete", f"当前月份缺少{label}")
        return None
    try:
        size = path.stat().st_size
        if size > MAX_ARCHIVE_JSON_BYTES:
            raise ServiceError(413, "archive_too_large", f"{label}超过安全上限")
        return json.loads(path.read_text(encoding="utf-8"))
    except ServiceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ServiceError(409, "archive_invalid", f"{label}不是有效的 UTF-8 JSON") from exc


def rebuild_plan_from_archive(
    archive_root: Path, month: str, selected_group_keys: Sequence[str]
) -> RebuiltPlan:
    """从当前归档重建精确明细白名单，并生成不可变审批计划。"""

    if not isinstance(month, str) or not MONTH_PATTERN.fullmatch(month):
        raise ServiceError(400, "invalid_month", "month 必须为合法 YYYY-MM")
    if not isinstance(selected_group_keys, (list, tuple)):
        raise ServiceError(400, "invalid_group_keys", "group_keys 必须是数组")
    if not selected_group_keys:
        raise ServiceError(400, "empty_selection", "至少选择一条审批明细")
    if len(selected_group_keys) > MAX_SELECTED_GROUPS:
        raise ServiceError(413, "too_many_groups", "选择整单数超过安全上限 200")

    normalized_keys: list[str] = []
    seen_keys: set[str] = set()
    for value in selected_group_keys:
        if not isinstance(value, str) or not GROUP_KEY_PATTERN.fullmatch(value):
            raise ServiceError(400, "invalid_group_key", "group_keys 包含非法稳定明细 ID")
        if value in seen_keys:
            raise ServiceError(400, "duplicate_group_key", "group_keys 不允许重复")
        seen_keys.add(value)
        normalized_keys.append(value)

    root = archive_root.resolve()
    if (root.parent / ".srdpm-refresh.lock").exists():
        raise ServiceError(409, "refresh_in_progress", "数据刷新进行中，请稍后重新打开看板")
    month_dir = (root / month).resolve()
    try:
        month_dir.relative_to(root)
    except ValueError as exc:  # 纵深防御；月份正则本身已阻止路径穿越。
        raise ServiceError(400, "invalid_month", "月份路径不合法") from exc
    if not month_dir.is_dir():
        raise ServiceError(404, "month_not_found", "当前归档中不存在该月份")

    audit_data = _read_json_file(month_dir / "audit_report.json", "审核报告", required=True)
    raw_data = _read_json_file(month_dir / "raw_data.json", "原始审批数据", required=True)
    if not isinstance(audit_data, Mapping) or not isinstance(raw_data, Mapping):
        raise ServiceError(409, "archive_invalid", "当前月份归档根节点结构异常")
    md_path = month_dir / "audit_report.md"
    try:
        md_text = md_path.read_text(encoding="utf-8") if md_path.is_file() else ""
    except (OSError, UnicodeError) as exc:
        raise ServiceError(409, "archive_invalid", "审核说明不是有效的 UTF-8 文本") from exc

    try:
        categories = build_category_data(audit_data, md_text, raw_data)
        groups = build_approval_groups(
            raw_data,
            manual_pairs_from_categories(categories),
            categories=categories,
        )
        assign_primary_categories(categories, groups)
        impact_summaries = _impact_summaries(raw_data)
    except ServiceError:
        raise
    except Exception as exc:
        raise ServiceError(409, "archive_rebuild_failed", "无法从当前归档重建审批白名单") from exc

    selected_groups: list[tuple[str, Mapping[str, Any]]] = []
    for group_key in normalized_keys:
        group = groups.get(group_key)
        if not isinstance(group, Mapping):
            raise ServiceError(409, "group_not_allowed", "所选明细已不在当前归档白名单")
        if group.get("status") == "approved":
            raise ServiceError(409, "group_already_approved", "所选明细在当前归档中已审批")
        if group.get("primary_category") not in ACTIONABLE_CATEGORIES:
            raise ServiceError(409, "group_not_actionable", "所选明细不属于可审批分类")
        selected_groups.append((group_key, group))

    selected_groups.sort(
        key=lambda pair: (
            str(pair[1].get("date") or ""),
            str(pair[1].get("person") or ""),
            pair[0],
        )
    )

    # Multiple selected rows may belong to the same person-date.  They are
    # merged only for the single SRDPM request; each browser-selected row stays
    # independently visible in the confirmation and result payload.
    plan_groups_by_identity: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    group_keys_by_identity: dict[tuple[str, str, str | None], list[str]] = {}
    identity_by_person_date: dict[tuple[str, str], tuple[str, str, str | None]] = {}
    id_owners: dict[str, tuple[str, str, str | None]] = {}
    group_rows: list[dict[str, Any]] = []
    total_ids = 0
    for group_key, group in selected_groups:
        approve_ids = [str(value) for value in group.get("approve_ids") or []]
        if not approve_ids:
            raise ServiceError(409, "group_without_ids", "所选明细没有可执行审批 ID")
        if len(approve_ids) > MAX_IDS_PER_GROUP:
            raise ServiceError(413, "group_too_large", "单条选择超过 500 个审批 ID")

        date = str(group.get("date") or "")
        person = str(group.get("person") or "").strip()
        raw_user_id = group.get("user_id")
        user_id = str(raw_user_id).strip() if raw_user_id is not None else None
        user_id = user_id or None
        identity = (person, date, user_id)
        person_date = (person, date)
        existing_identity = identity_by_person_date.get(person_date)
        if existing_identity is not None and existing_identity != identity:
            raise ServiceError(409, "ambiguous_person_identity", "同名人员身份不一致，已停止审批")
        identity_by_person_date[person_date] = identity

        plan_group = plan_groups_by_identity.setdefault(
            identity,
            {"person": person, "date": date, "approve_ids": []},
        )
        if user_id is not None:
            plan_group["user_id"] = user_id
        for approve_id in approve_ids:
            owner = id_owners.get(approve_id)
            if owner is not None and owner != identity:
                raise ServiceError(409, "approval_id_identity_conflict", "审批明细身份冲突")
            if owner is None:
                id_owners[approve_id] = identity
                plan_group["approve_ids"].append(approve_id)
                total_ids += 1
        if total_ids > MAX_TOTAL_IDS:
            raise ServiceError(413, "plan_too_large", "审批计划超过 5000 个审批 ID")
        group_keys_by_identity.setdefault(identity, []).append(group_key)

        projects: list[str] = []
        project_seen: set[str] = set()
        work_hours = 0.0
        for approve_id in approve_ids:
            impact = impact_summaries.get(approve_id, {})
            try:
                work_hours += float(impact.get("work_hours", 0) or 0)
            except (TypeError, ValueError):
                pass
            for project in impact.get("projects", []):
                if project not in project_seen:
                    project_seen.add(project)
                    projects.append(project)
        source_categories = group.get("source_categories") or []
        category_titles = [
            _short_display_text(categories.get(key, {}).get("title"))
            for key in source_categories
            if key in categories
        ]
        group_rows.append(
            {
                "group_key": group_key,
                "date": date,
                "person": person,
                "id_count": len(approve_ids),
                "item_count": int(group.get("item_count") or len(approve_ids)),
                "work_hours": round(work_hours, 2),
                "projects": projects[:10],
                "project_count": len(projects),
                "review_summary": " / ".join(category_titles)
                or ("人工异常" if group.get("review_mode") == "manual" else "自动候选"),
                "review_mode": "manual"
                if group.get("review_mode") == "manual"
                else "auto",
                "scope": str(group.get("scope") or "明细"),
            }
        )

    try:
        plan = parse_plan(
            {
                "schema_version": 1,
                "month": month,
                "groups": list(plan_groups_by_identity.values()),
            }
        )
    except PlanValidationError as exc:
        raise ServiceError(409, "rebuilt_plan_invalid", "当前归档无法生成安全审批计划") from exc
    summary = summarize_plan(plan)
    return RebuiltPlan(
        plan=plan,
        summary=summary,
        group_keys=tuple(row["group_key"] for row in group_rows),
        group_rows=tuple(group_rows),
        identity_by_person_date=identity_by_person_date,
        group_keys_by_identity={
            identity: tuple(keys) for identity, keys in group_keys_by_identity.items()
        },
    )


class ApprovalService:
    """线程安全的票据、任务与执行锁状态。"""

    def __init__(
        self,
        *,
        project_dir: Path = PROJECT_DIR,
        archive_root: Path | None = None,
        client_factory: Callable[[], Any] | None = None,
        credential_store: Any | None = None,
        credential_client_factory: Callable[[str, str], Any] = SRDPMClient.from_credentials,
        approval_confirmer: Callable[[PlanSummary], bool] = confirm_approval_with_windows,
        refresh_runner: Callable[[Path], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.archive_root = (
            Path(archive_root).resolve()
            if archive_root is not None
            else self.project_dir / "srdpm_archive"
        )
        self.dashboard_path = self.project_dir / DASHBOARD_NAME
        self.credential_store = credential_store or WindowsCredentialStore()
        self.credential_client_factory = credential_client_factory
        self.approval_confirmer = approval_confirmer
        self.client_factory = client_factory or self._client_from_configured_credentials
        self.refresh_runner = refresh_runner or self._refresh_current_month
        self.monotonic = monotonic
        self.instance_id = secrets.token_urlsafe(18)
        self.csrf_token = secrets.token_urlsafe(32)
        self._tickets: dict[str, PreparedTicket] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._refresh_jobs: dict[str, dict[str, Any]] = {}
        self._refresh_active = False
        self._state_lock = threading.RLock()
        self._execution_lock = threading.Lock()

    @staticmethod
    def _environment_credentials_configured() -> bool:
        return bool(
            os.environ.get("SRDPM_USERNAME", "").strip()
            and os.environ.get("SRDPM_PASSWORD", "")
        )

    def credentials_status(self) -> dict[str, Any]:
        try:
            if self.credential_store.has_credentials():
                return {"configured": True, "source": "windows_credential_manager"}
        except Exception as exc:
            raise ServiceError(
                503, "credential_store_unavailable", "Windows 凭据管理器暂不可用"
            ) from exc
        if self._environment_credentials_configured():
            return {"configured": True, "source": "environment"}
        return {"configured": False, "source": None}

    def configure_credentials(self, username: Any, password: Any) -> dict[str, Any]:
        if not isinstance(username, str) or not isinstance(password, str):
            raise ServiceError(400, "invalid_credentials", "用户名和密码格式不合法")
        username = username.strip()
        if (
            not username
            or not password
            or len(username) > 256
            or len(password) > 4096
            or any(ord(ch) < 32 for ch in username)
            or any(ch in "\r\n\x00" for ch in password)
        ):
            raise ServiceError(400, "invalid_credentials", "用户名和密码格式不合法")

        client: Any | None = None
        try:
            client = self.credential_client_factory(username, password)
            if not client.check_live():
                raise RuntimeError("read-only login validation returned false")
        except Exception as exc:
            raise ServiceError(
                401, "credential_validation_failed", "SRDPM 登录校验失败，请检查用户名和密码"
            ) from exc
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

        try:
            self.credential_store.save(username, password)
        except Exception as exc:
            raise ServiceError(
                503, "credential_store_write_failed", "登录校验已通过，但 Windows 凭据保存失败"
            ) from exc
        return {"configured": True, "source": "windows_credential_manager"}

    def _client_from_configured_credentials(self) -> Any:
        credentials = self.credential_store.load()
        if credentials is not None:
            return self.credential_client_factory(
                credentials.username, credentials.password
            )
        return SRDPMClient.from_env()

    def _refresh_current_month(self, project_dir: Path) -> Any:
        """Run the only supported read-only refresh entrypoint lazily.

        Importing here keeps ordinary dashboard/approval startup free from the
        refresh module while ensuring that a UI request cannot choose a script,
        month, path, credential, or approval operation.
        """

        from refresh_dashboard import refresh_current_month

        return refresh_current_month(project_dir=project_dir)

    def _raise_if_refresh_active_locked(self) -> None:
        if self._refresh_active:
            raise ServiceError(
                409,
                "refresh_in_progress",
                "数据刷新进行中，请等待页面重新加载后再审批",
            )

    def rebuild(self, month: str, group_keys: Sequence[str]) -> RebuiltPlan:
        return rebuild_plan_from_archive(self.archive_root, month, group_keys)

    def prepare(self, month: str, group_keys: Sequence[str]) -> dict[str, Any]:
        with self._state_lock:
            self._raise_if_refresh_active_locked()
        rebuilt = self.rebuild(month, group_keys)
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=TICKET_TTL_SECONDS)
        prepared = PreparedTicket(
            token=token,
            month=rebuilt.plan.month,
            group_keys=rebuilt.group_keys,
            plan_sha256=rebuilt.summary.sha256,
            expires_monotonic=self.monotonic() + TICKET_TTL_SECONDS,
            expires_at_utc=expires_at.isoformat().replace("+00:00", "Z"),
        )
        with self._state_lock:
            self._purge_expired_tickets_locked()
            self._tickets[token] = prepared
        return {
            "ticket": token,
            "expires_at": prepared.expires_at_utc,
            "expires_in_seconds": TICKET_TTL_SECONDS,
            "summary": {
                "month": rebuilt.summary.month,
                "group_count": rebuilt.summary.group_count,
                "selection_count": len(rebuilt.group_rows),
                "id_count": rebuilt.summary.id_count,
                "person_count": rebuilt.summary.person_count,
                "date_count": rebuilt.summary.date_count,
                "sha256": rebuilt.summary.sha256,
            },
            "groups": [dict(row) for row in rebuilt.group_rows],
        }

    def _purge_expired_tickets_locked(self) -> None:
        now = self.monotonic()
        for token in list(self._tickets):
            ticket = self._tickets[token]
            if not ticket.used and ticket.expires_monotonic < now:
                del self._tickets[token]

    def start_job(self, ticket_token: str) -> dict[str, Any]:
        if not isinstance(ticket_token, str) or not ticket_token:
            raise ServiceError(400, "invalid_ticket", "ticket 必须是非空字符串")

        with self._state_lock:
            self._raise_if_refresh_active_locked()
            ticket = self._tickets.get(ticket_token)
            if ticket is None:
                raise ServiceError(409, "ticket_invalid", "审批票据不存在或已经失效")
            if ticket.used:
                raise ServiceError(409, "ticket_used", "审批票据已经使用，拒绝重复执行")
            if ticket.expires_monotonic < self.monotonic():
                del self._tickets[ticket_token]
                raise ServiceError(410, "ticket_expired", "审批票据已过期，请重新确认")
            if not self._execution_lock.acquire(blocking=False):
                raise ServiceError(409, "execution_busy", "已有真实审批任务正在执行")

            ticket.used = True
            reserved_ticket = copy.copy(ticket)

        try:
            rebuilt = self.rebuild(reserved_ticket.month, reserved_ticket.group_keys)
            if not hmac.compare_digest(
                rebuilt.summary.sha256, reserved_ticket.plan_sha256
            ):
                raise ServiceError(
                    409,
                    "archive_changed_before_confirmation",
                    "归档在清单确认前发生变化，未提交审批",
                )
            try:
                confirmed = self.approval_confirmer(rebuilt.summary)
            except Exception as exc:
                raise ServiceError(
                    503,
                    "native_confirmation_unavailable",
                    "无法显示 Windows 安全确认，未提交审批",
                ) from exc
            if not confirmed:
                raise ServiceError(
                    409,
                    "native_confirmation_rejected",
                    "Windows 安全确认已取消，未提交审批",
                )
        except Exception:
            self._execution_lock.release()
            raise

        with self._state_lock:
            job_id = secrets.token_urlsafe(24)
            job = {
                "job_id": job_id,
                "status": "queued",
                "outcome": None,
                "month": reserved_ticket.month,
                "plan_sha256": reserved_ticket.plan_sha256,
                "created_at": _utc_now_text(),
                "started_at": None,
                "finished_at": None,
                "message": "等待执行",
                "groups": [],
            }
            self._jobs[job_id] = job

        worker = threading.Thread(
            target=self._run_job,
            args=(job_id, reserved_ticket),
            name=f"srdpm-approval-{job_id[:8]}",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as exc:
            with self._state_lock:
                job["status"] = "failed"
                job["outcome"] = "rejected_no_change"
                job["message"] = "无法启动本机审批任务，未联网、未提交审批"
                job["finished_at"] = _utc_now_text()
            self._execution_lock.release()
            raise ServiceError(500, "job_start_failed", "无法启动本机审批任务") from exc
        return copy.deepcopy(job)

    def start_refresh_job(self) -> dict[str, Any]:
        """Start one read-only dashboard refresh without accepting any inputs.

        The service-level marker closes the small window before
        ``refresh_dashboard`` creates its cross-process mutex/file lock.  The
        refresh entrypoint then remains the authoritative guard against the
        scheduled task and any independently launched refresh.
        """

        with self._state_lock:
            if self._refresh_active:
                active_job = next(
                    (
                        job
                        for job in reversed(tuple(self._refresh_jobs.values()))
                        if job.get("status") in {"queued", "running"}
                    ),
                    None,
                )
                if active_job is not None:
                    return copy.deepcopy(active_job)
                # A terminal worker always clears this marker in ``finally``.
                # Heal an inconsistent in-memory marker instead of permanently
                # rejecting every later refresh until the service is restarted.
                self._refresh_active = False
            if self._execution_lock.locked():
                raise ServiceError(
                    409,
                    "approval_in_progress",
                    "真实审批正在执行，暂不能刷新数据",
                )

            job_id = secrets.token_urlsafe(24)
            job = {
                "job_id": job_id,
                "status": "queued",
                "created_at": _utc_now_text(),
                "started_at": None,
                "finished_at": None,
                "updated_month": None,
                "message": "等待读取当前自然月和前一个自然月 SRDPM 数据",
            }
            self._refresh_jobs[job_id] = job
            self._refresh_active = True

        worker = threading.Thread(
            target=self._run_refresh_job,
            args=(job_id,),
            name=f"srdpm-dashboard-refresh-{job_id[:8]}",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as exc:
            with self._state_lock:
                job["status"] = "failed"
                job["message"] = "无法启动本机数据刷新任务，当前看板未修改"
                job["finished_at"] = _utc_now_text()
                self._refresh_active = False
            raise ServiceError(500, "refresh_start_failed", "无法启动本机数据刷新任务") from exc
        return copy.deepcopy(job)

    @staticmethod
    def _refresh_failure_message(error: Exception) -> str:
        """Map refresh errors to safe browser-facing messages only."""

        try:
            from refresh_dashboard import RefreshBusyError, RefreshCredentialError
        except Exception:  # pragma: no cover - module is part of this project
            RefreshBusyError = ()  # type: ignore[assignment,misc]
            RefreshCredentialError = ()  # type: ignore[assignment,misc]
        if isinstance(error, RefreshBusyError):
            return "已有数据刷新或真实审批正在执行，当前看板未修改"
        if isinstance(error, RefreshCredentialError):
            return "未找到可用的 SRDPM 登录凭据，当前看板未修改"
        return "数据刷新失败，现有数据和看板均未修改，请稍后重试"

    def _run_refresh_job(self, job_id: str) -> None:
        try:
            with self._state_lock:
                job = self._refresh_jobs[job_id]
                job["status"] = "running"
                job["started_at"] = _utc_now_text()
                job["message"] = "正在读取当前自然月和前一个自然月数据、重新审计并生成看板；不会提交审批"

            result = self.refresh_runner(self.project_dir)
            updated_month = getattr(result, "month", None)
            if not isinstance(updated_month, str) or not MONTH_PATTERN.fullmatch(updated_month):
                raise RuntimeError("refresh runner returned an invalid month")

            with self._state_lock:
                job = self._refresh_jobs[job_id]
                job["status"] = "succeeded"
                job["updated_month"] = updated_month
                job["message"] = "数据已刷新，正在重新加载最新看板"
                job["finished_at"] = _utc_now_text()
        except Exception as exc:
            with self._state_lock:
                job = self._refresh_jobs[job_id]
                job["status"] = "failed"
                job["message"] = self._refresh_failure_message(exc)
                job["finished_at"] = _utc_now_text()
        finally:
            with self._state_lock:
                self._refresh_active = False

    def get_refresh_job(self, job_id: str) -> dict[str, Any]:
        with self._state_lock:
            job = self._refresh_jobs.get(job_id)
            if job is None:
                raise ServiceError(404, "refresh_job_not_found", "数据刷新任务不存在")
            return copy.deepcopy(job)

    def _initial_group_results(self, rebuilt: RebuiltPlan) -> list[dict[str, Any]]:
        return [
            {
                **dict(row),
                "state": "not_attempted",
                "message": "尚未提交",
            }
            for row in rebuilt.group_rows
        ]

    def _run_job(self, job_id: str, ticket: PreparedTicket) -> None:
        client: Any | None = None
        try:
            with self._state_lock:
                job = self._jobs[job_id]
                job["status"] = "running"
                job["started_at"] = _utc_now_text()
                job["message"] = "正在从当前归档重建并校验审批计划"

            rebuilt = self.rebuild(ticket.month, ticket.group_keys)
            group_results = self._initial_group_results(rebuilt)
            with self._state_lock:
                self._jobs[job_id]["groups"] = copy.deepcopy(group_results)

            if not hmac.compare_digest(rebuilt.summary.sha256, ticket.plan_sha256):
                with self._state_lock:
                    job = self._jobs[job_id]
                    job["status"] = "failed"
                    job["outcome"] = "rejected_no_change"
                    job["message"] = "归档在确认后发生变化，未创建客户端、未提交审批"
                    job["finished_at"] = _utc_now_text()
                return

            try:
                client = self.client_factory()
            except Exception:
                with self._state_lock:
                    job = self._jobs[job_id]
                    job["status"] = "failed"
                    job["outcome"] = "rejected_no_change"
                    job["message"] = "SRDPM 登录配置不可用，未联网、未提交审批"
                    job["finished_at"] = _utc_now_text()
                return
            report = execute_plan(rebuilt.plan, client, allow_partial=True)

            rows_by_key = {row["group_key"]: row for row in group_results}
            # 结果本身不携带浏览器字段。服务端按完整人员身份把执行批次
            # 映射回所有精确明细选择；不会按结果数组下标猜测。
            for result in report.group_results:
                identity = rebuilt.identity_by_person_date.get(
                    (result.person, result.date)
                )
                group_keys = (
                    rebuilt.group_keys_by_identity.get(identity, ())
                    if identity is not None
                    else ()
                )
                if not group_keys:
                    continue
                for group_key in group_keys:
                    row = rows_by_key[group_key]
                    verification_status = result.verification_status
                    if verification_status == "verified_approved":
                        row["state"] = "verified_approved"
                        row["message"] = "SRDPM 回读状态全部为通过"
                    elif verification_status == "unknown":
                        row["state"] = "unknown"
                        row["message"] = "审批最终状态需人工核对"
                    else:
                        row["state"] = "not_attempted"
                        row["message"] = "提交前校验失败，未尝试审批"

            if report.success:
                final_status = "succeeded"
                message = "全部所选明细审批并经 SRDPM 回读确认通过"
            elif report.outcome == "partial_success":
                final_status = "failed"
                message = "部分所选明细已确认通过，其余明细未执行或状态未知"
            elif report.outcome == "state_unknown":
                final_status = "failed"
                message = "审批结果存在未知状态，请在 SRDPM 中人工核对"
            else:
                final_status = "failed"
                message = "提交前校验失败，未执行审批"

            with self._state_lock:
                job = self._jobs[job_id]
                job["groups"] = copy.deepcopy(group_results)
                job["status"] = final_status
                job["outcome"] = report.outcome
                job["message"] = message
                job["finished_at"] = _utc_now_text()
        except ServiceError as exc:
            with self._state_lock:
                job = self._jobs[job_id]
                job["status"] = "failed"
                job["outcome"] = "rejected_no_change"
                job["message"] = exc.message
                job["finished_at"] = _utc_now_text()
        except Exception:
            with self._state_lock:
                job = self._jobs[job_id]
                job["status"] = "failed"
                job["outcome"] = "state_unknown"
                job["message"] = "本机审批任务异常，请在 SRDPM 中人工核对"
                job["finished_at"] = _utc_now_text()
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    # 关闭失败不能把已经逐 ID 回读成功的结果降级为未知。
                    pass
            self._execution_lock.release()

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._state_lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ServiceError(404, "job_not_found", "审批任务不存在")
            return copy.deepcopy(job)

    def render_dashboard(self) -> bytes:
        try:
            html = self.dashboard_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ServiceError(503, "dashboard_unavailable", "生成看板不存在或无法读取") from exc
        config_json = json.dumps(
            {
                "api_base": "/api/v1",
                "csrf_header": "X-SRDPM-CSRF",
                "csrf_token": self.csrf_token,
                "instance_id": self.instance_id,
                "service_build_id": SERVICE_BUILD_ID,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).replace("<", "\\u003c")
        injection = (
            '<script id="srdpm-local-service-config" type="application/json">'
            + config_json
            + "</script>"
        )
        marker = "</head>"
        if marker not in html:
            raise ServiceError(503, "dashboard_invalid", "生成看板缺少 head 结束标记")
        return html.replace(marker, injection + marker, 1).encode("utf-8")


class LocalApprovalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], service: ApprovalService) -> None:
        if address[0] != LISTEN_HOST:
            raise ValueError("本机审批服务只允许绑定 127.0.0.1")
        self.service = service
        super().__init__(address, LocalApprovalRequestHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """浏览器关闭 keep-alive 连接时不向控制台打印无害堆栈。"""

        error = sys.exc_info()[1]
        if isinstance(
            error, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)
        ):
            return
        super().handle_error(request, client_address)

    @property
    def expected_host(self) -> str:
        return f"{LISTEN_HOST}:{self.server_address[1]}"

    @property
    def origin(self) -> str:
        return f"http://{self.expected_host}"


class LocalApprovalRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SRDPMLocalApproval/1"
    sys_version = ""

    @property
    def app_server(self) -> LocalApprovalHTTPServer:
        return self.server  # type: ignore[return-value]

    @property
    def service(self) -> ApprovalService:
        return self.app_server.service

    def log_message(self, format: str, *args: Any) -> None:
        # 不记录请求头、请求体、CSRF、ticket 或凭据。
        return

    def _common_headers(self, content_type: str, content_length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self._common_headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_service_error(self, exc: ServiceError) -> None:
        self._send_json(
            exc.status,
            {"ok": False, "error": {"code": exc.code, "message": exc.message}},
        )

    def _single_header(self, name: str) -> str | None:
        values = self.headers.get_all(name, failobj=[])
        if len(values) != 1:
            return None
        return values[0]

    def _require_host(self) -> None:
        host = self._single_header("Host")
        if host is None or not hmac.compare_digest(host, self.app_server.expected_host):
            raise ServiceError(403, "invalid_host", "拒绝非本机精确 Host 请求")

    def _require_api_security(self, *, require_origin: bool) -> None:
        fetch_site = self._single_header("Sec-Fetch-Site")
        if fetch_site != "same-origin":
            raise ServiceError(403, "cross_site_forbidden", "拒绝非同源浏览器请求")
        if require_origin:
            origin = self._single_header("Origin")
            if origin is None or not hmac.compare_digest(origin, self.app_server.origin):
                raise ServiceError(403, "invalid_origin", "拒绝非同源 Origin 请求")
        csrf = self._single_header("X-SRDPM-CSRF")
        if csrf is None or not hmac.compare_digest(csrf, self.service.csrf_token):
            raise ServiceError(403, "invalid_csrf", "CSRF 校验失败")

    def _read_json_body(self) -> Any:
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            raise ServiceError(400, "transfer_encoding_forbidden", "不接受 Transfer-Encoding")
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1:
            raise ServiceError(411, "content_length_required", "必须提供唯一 Content-Length")
        raw_length = lengths[0]
        if not raw_length.isascii() or not raw_length.isdigit():
            raise ServiceError(400, "invalid_content_length", "Content-Length 不合法")
        length = int(raw_length)
        if length > MAX_HTTP_BODY_BYTES:
            raise ServiceError(413, "request_too_large", "请求体超过安全上限")
        content_type = self._single_header("Content-Type")
        if content_type is None or content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ServiceError(415, "json_required", "只接受 application/json")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ServiceError(400, "incomplete_body", "请求体不完整")
        try:
            text = body.decode("utf-8", errors="strict")
            return json.loads(text)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ServiceError(400, "invalid_json", "请求体不是有效的 UTF-8 JSON") from exc

    @staticmethod
    def _require_exact_object(data: Any, keys: set[str]) -> Mapping[str, Any]:
        if not isinstance(data, Mapping) or set(data) != keys:
            raise ServiceError(400, "invalid_request", "请求字段不符合接口契约")
        return data

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            self._require_host()
            path = urlsplit(self.path).path
            if path in {"/", "/index.html"}:
                body = self.service.render_dashboard()
                self.send_response(200)
                self._common_headers("text/html; charset=utf-8", len(body))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/v1/credentials/status":
                self._require_api_security(require_origin=False)
                self._send_json(
                    200,
                    {"ok": True, "credentials": self.service.credentials_status()},
                )
                return

            refresh_match = REFRESH_JOB_PATH_PATTERN.fullmatch(path)
            if refresh_match:
                self._require_api_security(require_origin=False)
                self._send_json(
                    200,
                    {"ok": True, "job": self.service.get_refresh_job(refresh_match.group(1))},
                )
                return

            match = JOB_PATH_PATTERN.fullmatch(path)
            if match:
                self._require_api_security(require_origin=False)
                self._send_json(200, {"ok": True, "job": self.service.get_job(match.group(1))})
                return
            raise ServiceError(404, "not_found", "请求路径不存在")
        except ServiceError as exc:
            self._send_service_error(exc)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            self._require_host()
            self._require_api_security(require_origin=True)
            path = urlsplit(self.path).path
            data = self._read_json_body()
            if path == "/api/v1/credentials/configure":
                request = self._require_exact_object(data, {"username", "password"})
                result = self.service.configure_credentials(
                    request["username"], request["password"]
                )
                self._send_json(200, {"ok": True, "credentials": result})
                return
            if path == "/api/v1/approval/prepare":
                request = self._require_exact_object(data, {"month", "group_keys"})
                result = self.service.prepare(request["month"], request["group_keys"])
                self._send_json(200, {"ok": True, "prepared": result})
                return
            if path == "/api/v1/approval/execute":
                request = self._require_exact_object(data, {"ticket"})
                job = self.service.start_job(request["ticket"])
                self._send_json(202, {"ok": True, "job": job})
                return
            if path == "/api/v1/dashboard/refresh":
                self._require_exact_object(data, set())
                job = self.service.start_refresh_job()
                self._send_json(202, {"ok": True, "job": job})
                return
            raise ServiceError(404, "not_found", "请求路径不存在")
        except ServiceError as exc:
            self._send_service_error(exc)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        # 明确拒绝跨源预检，且绝不发送 Access-Control-Allow-*。
        try:
            self._require_host()
            self._send_json(
                405,
                {"ok": False, "error": {"code": "method_not_allowed", "message": "不支持 OPTIONS"}},
            )
        except ServiceError as exc:
            self._send_service_error(exc)


def create_server(
    *,
    port: int = DEFAULT_PORT,
    project_dir: Path = PROJECT_DIR,
    archive_root: Path | None = None,
    client_factory: Callable[[], Any] | None = None,
    credential_store: Any | None = None,
    credential_client_factory: Callable[[str, str], Any] = SRDPMClient.from_credentials,
    approval_confirmer: Callable[[PlanSummary], bool] = confirm_approval_with_windows,
    refresh_runner: Callable[[Path], Any] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> LocalApprovalHTTPServer:
    if isinstance(port, bool) or not isinstance(port, int) or not (0 <= port <= 65_535):
        raise ValueError("port 必须为 0 到 65535 的整数")
    service = ApprovalService(
        project_dir=project_dir,
        archive_root=archive_root,
        client_factory=client_factory,
        credential_store=credential_store,
        credential_client_factory=credential_client_factory,
        approval_confirmer=approval_confirmer,
        refresh_runner=refresh_runner,
        monotonic=monotonic,
    )
    return LocalApprovalHTTPServer((LISTEN_HOST, port), service)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动仅供本机使用的 SRDPM 工时审批看板")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="本机端口，默认 8765")
    parser.add_argument("--open", action="store_true", help="启动后打开默认浏览器")
    parser.add_argument("--quiet", action="store_true", help="后台模式，不输出控制台信息")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not (1 <= args.port <= 65_535):
        if not args.quiet:
            print("启动失败：--port 必须为 1 到 65535")
        return 2
    try:
        server = create_server(port=args.port)
    except OSError as exc:
        if not args.quiet:
            print(f"启动失败：无法绑定 {LISTEN_HOST}:{args.port}（{type(exc).__name__}）")
        return 2

    url = server.origin + "/"
    if not args.quiet:
        print(f"SRDPM 本机审批看板已启动：{url}")
        print("仅监听 127.0.0.1；真实审批仍需页面二次确认。按 Ctrl+C 停止。")
        print("本机服务与命令行真实审批共享跨进程互斥锁，拒绝并行提交。")
    if args.open:
        timer = threading.Timer(0.25, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        if not args.quiet:
            print("\n正在停止本机审批看板...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
