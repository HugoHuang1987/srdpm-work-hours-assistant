"""校验、摘要并在严格确认后执行 SRDPM 审批计划。

默认模式完全离线。只有 ``--check-live`` 会进行只读登录校验；只有
``--execute`` 加月份确认和精确确认语后才可能提交审批。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, TextIO

from srdpm_client import SRDPMClient, SRDPMError


PLAN_SCHEMA_VERSION = 1
MAX_PLAN_BYTES = 5 * 1024 * 1024
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
ID_PATTERN = re.compile(r"^[1-9]\d*$")

OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_REJECTED_NO_CHANGE = "rejected_no_change"
OUTCOME_PARTIAL_SUCCESS = "partial_success"
OUTCOME_STATE_UNKNOWN = "state_unknown"

VERIFICATION_VERIFIED_APPROVED = "verified_approved"
VERIFICATION_NOT_ATTEMPTED = "not_attempted"
VERIFICATION_UNKNOWN = "unknown"

EXECUTION_MUTEX_NAME = r"Local\SRDPMApprovalExecutor-v1"


class PlanValidationError(ValueError):
    """审批计划格式或内容不满足安全约束。"""


class ConfirmationError(ValueError):
    """执行确认缺失或不匹配。"""


class ExecutionLockError(RuntimeError):
    """无法安全创建或操作真实审批执行锁。"""


class _ExecutionLockHandle(Protocol):
    def release(self) -> None: ...


class _ProcessExecutionLockHandle:
    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._lock.release()


class _WindowsExecutionMutexHandle:
    def __init__(self, kernel32: Any, handle: Any) -> None:
        self._kernel32 = kernel32
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if not handle:
            return
        self._handle = None
        try:
            self._kernel32.ReleaseMutex(handle)
        finally:
            self._kernel32.CloseHandle(handle)


_PROCESS_EXECUTION_LOCK = threading.Lock()


def _try_acquire_windows_execution_mutex() -> _ExecutionLockHandle | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateMutexW(None, False, EXECUTION_MUTEX_NAME)
    if not handle:
        raise ExecutionLockError(
            f"CreateMutexW 失败，error={ctypes.get_last_error()}"
        )

    wait_object_0 = 0x00000000
    wait_abandoned = 0x00000080
    wait_timeout = 0x00000102
    result = kernel32.WaitForSingleObject(handle, 0)
    if result == wait_object_0:
        return _WindowsExecutionMutexHandle(kernel32, handle)
    if result == wait_abandoned:
        # 上一个所有者异常退出时可能已经触发过不可撤回提交。当前线程会取得
        # abandoned mutex 的所有权，但本次必须先释放并安全失败，禁止直接续跑。
        try:
            kernel32.ReleaseMutex(handle)
        finally:
            kernel32.CloseHandle(handle)
        raise ExecutionLockError("检测到上一次审批执行异常终止")

    kernel32.CloseHandle(handle)
    if result == wait_timeout:
        return None
    raise ExecutionLockError(
        f"WaitForSingleObject 失败，result={result}, "
        f"error={ctypes.get_last_error()}"
    )


def _try_acquire_execution_lock() -> _ExecutionLockHandle | None:
    if os.name == "nt":
        return _try_acquire_windows_execution_mutex()
    if not _PROCESS_EXECUTION_LOCK.acquire(blocking=False):
        return None
    return _ProcessExecutionLockHandle(_PROCESS_EXECUTION_LOCK)


class ApprovalClient(Protocol):
    def check_live(self) -> bool: ...

    def get_pending_ids(
        self, person: str, date: str, user_id: str | None = None
    ) -> Sequence[str]: ...

    def approve_ids(self, approve_ids: Sequence[str]) -> None: ...

    def get_approved_statuses(
        self, person: str, date: str, user_id: str | None = None
    ) -> Mapping[str, str]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class ApprovalGroup:
    person: str
    date: str
    approve_ids: tuple[str, ...]
    user_id: str | None = None


@dataclass(frozen=True)
class ApprovalPlan:
    schema_version: int
    month: str
    groups: tuple[ApprovalGroup, ...]


@dataclass(frozen=True)
class PlanSummary:
    month: str
    group_count: int
    id_count: int
    person_count: int
    date_count: int
    sha256: str


@dataclass(frozen=True)
class GroupExecutionResult:
    person: str
    date: str
    id_count: int
    success: bool
    message: str
    phase: str
    submission_attempted: bool
    mutation_possible: bool
    verification_status: str


@dataclass(frozen=True)
class ExecutionReport:
    success: bool
    outcome: str
    group_results: tuple[GroupExecutionResult, ...]
    message: str


def _normalize_id(value: Any, location: str) -> str:
    if isinstance(value, bool) or value is None:
        raise PlanValidationError(f"{location} 不是合法审批 ID")
    text = str(value).strip()
    if not ID_PATTERN.fullmatch(text):
        raise PlanValidationError(f"{location} 必须是正整数审批 ID")
    return text


def _dedupe_plan_ids(values: Any, location: str) -> tuple[str, ...]:
    if not isinstance(values, list) or not values:
        raise PlanValidationError(f"{location} 必须是非空数组")
    seen: set[str] = set()
    result: list[str] = []
    for index, value in enumerate(values):
        approve_id = _normalize_id(value, f"{location}[{index}]")
        if approve_id in seen:
            continue
        seen.add(approve_id)
        result.append(approve_id)
    return tuple(result)


def _normalize_user_id(value: Any, location: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PlanValidationError(f"{location} 必须是字符串或整数")
    text = str(value).strip()
    if not text or len(text) > 128 or any(ord(ch) < 32 for ch in text):
        raise PlanValidationError(f"{location} 不合法")
    return text


def parse_plan(data: Any) -> ApprovalPlan:
    if not isinstance(data, Mapping):
        raise PlanValidationError("计划根节点必须是 JSON 对象")

    schema_version = data.get("schema_version")
    if schema_version != PLAN_SCHEMA_VERSION:
        raise PlanValidationError(
            f"schema_version 必须为 {PLAN_SCHEMA_VERSION}"
        )

    month = data.get("month")
    if not isinstance(month, str) or not MONTH_PATTERN.fullmatch(month):
        raise PlanValidationError("month 必须为合法 YYYY-MM")

    groups_data = data.get("groups")
    if not isinstance(groups_data, list) or not groups_data:
        raise PlanValidationError("groups 必须是非空数组")
    if len(groups_data) > 1_000:
        raise PlanValidationError("groups 超过安全上限 1000")

    groups: list[ApprovalGroup] = []
    group_keys: set[tuple[str, str]] = set()
    id_owners: dict[str, tuple[str, str]] = {}

    for index, raw_group in enumerate(groups_data):
        location = f"groups[{index}]"
        if not isinstance(raw_group, Mapping):
            raise PlanValidationError(f"{location} 必须是对象")

        person = raw_group.get("person")
        if not isinstance(person, str):
            raise PlanValidationError(f"{location}.person 必须是字符串")
        person = person.strip()
        if not person or len(person) > 100 or any(ch in person for ch in "\r\n\t"):
            raise PlanValidationError(f"{location}.person 不合法")

        user_id = _normalize_user_id(raw_group.get("user_id"), f"{location}.user_id")

        date = raw_group.get("date")
        if not isinstance(date, str):
            raise PlanValidationError(f"{location}.date 必须是 YYYY-MM-DD")
        try:
            parsed_date = datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise PlanValidationError(
                f"{location}.date 必须是合法 YYYY-MM-DD"
            ) from exc
        if parsed_date.strftime("%Y-%m") != month:
            raise PlanValidationError(f"{location}.date 不属于计划月份 {month}")

        group_key = (person, date)
        if group_key in group_keys:
            raise PlanValidationError(f"重复的人员+日期组：{person} {date}")
        group_keys.add(group_key)

        approve_ids = _dedupe_plan_ids(
            raw_group.get("approve_ids"), f"{location}.approve_ids"
        )
        if len(approve_ids) > 10_000:
            raise PlanValidationError(f"{location}.approve_ids 超过安全上限")
        for approve_id in approve_ids:
            owner = id_owners.get(approve_id)
            if owner is not None and owner != group_key:
                raise PlanValidationError(
                    f"审批 ID {approve_id} 同时属于多个人员+日期组"
                )
            id_owners[approve_id] = group_key

        groups.append(
            ApprovalGroup(
                person=person,
                date=date,
                approve_ids=approve_ids,
                user_id=user_id,
            )
        )

    return ApprovalPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        month=month,
        groups=tuple(groups),
    )


def load_plan(path: Path | str) -> ApprovalPlan:
    plan_path = Path(path)
    try:
        size = plan_path.stat().st_size
    except OSError as exc:
        raise PlanValidationError(f"无法读取计划文件：{plan_path}") from exc
    if size > MAX_PLAN_BYTES:
        raise PlanValidationError("计划文件超过 5 MiB 安全上限")
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlanValidationError("计划文件不是有效的 UTF-8 JSON") from exc
    return parse_plan(data)


def _id_sort_key(value: str) -> tuple[int, str]:
    return (len(value), value)


def canonical_plan_payload(plan: ApprovalPlan) -> dict[str, Any]:
    groups = sorted(plan.groups, key=lambda group: (group.date, group.person))
    return {
        "schema_version": plan.schema_version,
        "month": plan.month,
        "groups": [
            dict(
                {
                    "person": group.person,
                    "date": group.date,
                    "approve_ids": sorted(group.approve_ids, key=_id_sort_key),
                },
                **({"user_id": group.user_id} if group.user_id is not None else {}),
            )
            for group in groups
        ],
    }


def summarize_plan(plan: ApprovalPlan) -> PlanSummary:
    canonical = json.dumps(
        canonical_plan_payload(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return PlanSummary(
        month=plan.month,
        group_count=len(plan.groups),
        id_count=sum(len(group.approve_ids) for group in plan.groups),
        person_count=len({group.person for group in plan.groups}),
        date_count=len({group.date for group in plan.groups}),
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def confirmation_phrase(summary: PlanSummary) -> str:
    return (
        f"EXECUTE SRDPM APPROVAL {summary.month} "
        f"GROUPS={summary.group_count} IDS={summary.id_count} "
        f"SHA256={summary.sha256}"
    )


def format_summary(summary: PlanSummary) -> str:
    return "\n".join(
        [
            "审批计划离线校验通过",
            f"  月份: {summary.month}",
            f"  人员+日期组: {summary.group_count}",
            f"  唯一审批 ID: {summary.id_count}",
            f"  涉及人员: {summary.person_count}",
            f"  涉及日期: {summary.date_count}",
            f"  计划 SHA256: {summary.sha256}",
        ]
    )


def _remote_id_set(values: Sequence[str], label: str) -> set[str]:
    result: set[str] = set()
    for index, value in enumerate(values):
        result.add(_normalize_id(value, f"{label}[{index}]"))
    return result


def _drift_message(
    planned: set[str], actual: set[str], *, allow_partial: bool = False
) -> str:
    missing = sorted(planned - actual, key=_id_sort_key)
    unexpected = sorted(actual - planned, key=_id_sort_key)
    if allow_partial:
        return (
            "所选审批 ID 已不再全部处于待审状态，已拒绝执行："
            f"缺失={missing}"
        )
    return (
        "实时待审 ID 与计划不一致，已拒绝执行："
        f"缺失={missing}，计划外={unexpected}"
    )


def _pending_ids_match_plan(
    planned: set[str], actual: set[str], *, allow_partial: bool
) -> bool:
    """Strict CLI plans must equal a person-day; service selections may be subsets."""

    return planned.issubset(actual) if allow_partial else actual == planned


def _group_result(
    group: ApprovalGroup,
    *,
    success: bool,
    message: str,
    phase: str,
    submission_attempted: bool,
    mutation_possible: bool,
    verification_status: str,
) -> GroupExecutionResult:
    return GroupExecutionResult(
        person=group.person,
        date=group.date,
        id_count=len(group.approve_ids),
        success=success,
        message=message,
        phase=phase,
        submission_attempted=submission_attempted,
        mutation_possible=mutation_possible,
        verification_status=verification_status,
    )


def _stopped_report(
    results: Sequence[GroupExecutionResult],
    message: str,
    *,
    state_unknown: bool = False,
) -> ExecutionReport:
    if any(
        result.verification_status == VERIFICATION_VERIFIED_APPROVED
        for result in results
    ):
        outcome = OUTCOME_PARTIAL_SUCCESS
    elif state_unknown:
        outcome = OUTCOME_STATE_UNKNOWN
    else:
        outcome = OUTCOME_REJECTED_NO_CHANGE
    return ExecutionReport(
        success=False,
        outcome=outcome,
        group_results=tuple(results),
        message=message,
    )


def _failed_readback_ids(
    group: ApprovalGroup, statuses: Mapping[str, str]
) -> list[str]:
    return sorted(
        [
            approve_id
            for approve_id in group.approve_ids
            if statuses.get(approve_id) != "通过"
        ],
        key=_id_sort_key,
    )


def _execute_plan_with_lock_held(
    plan: ApprovalPlan, client: ApprovalClient, *, allow_partial: bool = False
) -> ExecutionReport:
    """逐组执行审批；任何漂移、API 异常或回读异常都停止后续提交。"""

    results: list[GroupExecutionResult] = []

    # 先对全部组做一次只读预检，尽量避免执行到中途才发现已有漂移。
    for group in plan.groups:
        planned = set(group.approve_ids)
        try:
            actual = _remote_id_set(
                client.get_pending_ids(group.person, group.date, group.user_id),
                f"实时待审 {group.person} {group.date}",
            )
        except Exception as exc:
            message = f"预检失败：{type(exc).__name__}"
            results.append(
                _group_result(
                    group,
                    success=False,
                    message=message,
                    phase="precheck",
                    submission_attempted=False,
                    mutation_possible=False,
                    verification_status=VERIFICATION_NOT_ATTEMPTED,
                )
            )
            return _stopped_report(results, message)
        if not _pending_ids_match_plan(
            planned, actual, allow_partial=allow_partial
        ):
            message = _drift_message(planned, actual, allow_partial=allow_partial)
            results.append(
                _group_result(
                    group,
                    success=False,
                    message=message,
                    phase="precheck",
                    submission_attempted=False,
                    mutation_possible=False,
                    verification_status=VERIFICATION_NOT_ATTEMPTED,
                )
            )
            return _stopped_report(results, message)

    for group in plan.groups:
        planned = set(group.approve_ids)
        try:
            # 紧邻提交再拉一次 status=0，防止预检后发生竞态变化。
            actual = _remote_id_set(
                client.get_pending_ids(group.person, group.date, group.user_id),
                f"提交前待审 {group.person} {group.date}",
            )
        except Exception as exc:
            message = f"提交前检查失败：{type(exc).__name__}"
            results.append(
                _group_result(
                    group,
                    success=False,
                    message=message,
                    phase="pre_submit",
                    submission_attempted=False,
                    mutation_possible=False,
                    verification_status=VERIFICATION_NOT_ATTEMPTED,
                )
            )
            return _stopped_report(results, message)

        if not _pending_ids_match_plan(
            planned, actual, allow_partial=allow_partial
        ):
            message = _drift_message(planned, actual, allow_partial=allow_partial)
            results.append(
                _group_result(
                    group,
                    success=False,
                    message=message,
                    phase="pre_submit",
                    submission_attempted=False,
                    mutation_possible=False,
                    verification_status=VERIFICATION_NOT_ATTEMPTED,
                )
            )
            return _stopped_report(results, message)

        try:
            client.approve_ids(group.approve_ids)
        except Exception as submit_exc:
            # 提交调用一旦开始，异常可能发生在服务端已落库之后。绝不重试提交，
            # 立即只读回查；只有逐 ID 全部通过才能把这一组视为成功。
            try:
                statuses = client.get_approved_statuses(
                    group.person, group.date, group.user_id
                )
            except Exception as readback_exc:
                message = (
                    "审批提交返回异常且只读回查失败："
                    f"submit={type(submit_exc).__name__}, "
                    f"readback={type(readback_exc).__name__}"
                )
                results.append(
                    _group_result(
                        group,
                        success=False,
                        message=message,
                        phase="submit",
                        submission_attempted=True,
                        mutation_possible=True,
                        verification_status=VERIFICATION_UNKNOWN,
                    )
                )
                return _stopped_report(results, message, state_unknown=True)

            failed_ids = _failed_readback_ids(group, statuses)
            if failed_ids:
                message = (
                    f"审批提交返回异常（{type(submit_exc).__name__}），"
                    f"回读未全部通过：{failed_ids}"
                )
                results.append(
                    _group_result(
                        group,
                        success=False,
                        message=message,
                        phase="submit",
                        submission_attempted=True,
                        mutation_possible=True,
                        verification_status=VERIFICATION_UNKNOWN,
                    )
                )
                return _stopped_report(results, message, state_unknown=True)

            results.append(
                _group_result(
                    group,
                    success=True,
                    message=(
                        f"审批提交返回异常（{type(submit_exc).__name__}），"
                        "但 SRDPM 回读状态全部为通过"
                    ),
                    phase="readback",
                    submission_attempted=True,
                    mutation_possible=True,
                    verification_status=VERIFICATION_VERIFIED_APPROVED,
                )
            )
            continue

        # 正常返回也必须重拉 status=2/3，并逐 ID 验证真实状态。
        try:
            statuses = client.get_approved_statuses(
                group.person, group.date, group.user_id
            )
        except Exception as exc:
            message = f"审批提交后回读失败：{type(exc).__name__}"
            results.append(
                _group_result(
                    group,
                    success=False,
                    message=message,
                    phase="readback",
                    submission_attempted=True,
                    mutation_possible=True,
                    verification_status=VERIFICATION_UNKNOWN,
                )
            )
            return _stopped_report(results, message, state_unknown=True)

        failed_ids = _failed_readback_ids(group, statuses)
        if failed_ids:
            message = f"审批提交后回读未全部通过：{failed_ids}"
            results.append(
                _group_result(
                    group,
                    success=False,
                    message=message,
                    phase="readback",
                    submission_attempted=True,
                    mutation_possible=True,
                    verification_status=VERIFICATION_UNKNOWN,
                )
            )
            return _stopped_report(results, message, state_unknown=True)

        results.append(
            _group_result(
                group,
                success=True,
                message="SRDPM 回读状态全部为通过",
                phase="readback",
                submission_attempted=True,
                mutation_possible=True,
                verification_status=VERIFICATION_VERIFIED_APPROVED,
            )
        )

    return ExecutionReport(
        success=True,
        outcome=OUTCOME_SUCCEEDED,
        group_results=tuple(results),
        message="全部组审批并回读成功",
    )


def execute_plan(
    plan: ApprovalPlan,
    client: ApprovalClient,
    *,
    allow_partial: bool = False,
    after_report: Callable[[ApprovalPlan, ExecutionReport], None] | None = None,
) -> ExecutionReport:
    """在共享执行锁保护下执行计划；锁忙时不访问客户端。

    ``allow_partial`` is reserved for the local service after it has rebuilt an
    exact category-row whitelist.  Standalone plans keep the original strict
    person-day equality requirement.

    ``after_report`` runs exactly once after an execution report has been
    produced while the shared execution lock is still held.  It is not called
    when the lock cannot be acquired because there is no protected execution
    report in that case.  Callback errors propagate after the lock is released.
    """

    try:
        execution_lock = _try_acquire_execution_lock()
    except Exception as exc:
        return ExecutionReport(
            success=False,
            outcome=OUTCOME_REJECTED_NO_CHANGE,
            group_results=(),
            message=(
                f"审批执行锁不可用（{type(exc).__name__}），"
                "未联网、未提交审批"
            ),
        )

    if execution_lock is None:
        return ExecutionReport(
            success=False,
            outcome=OUTCOME_REJECTED_NO_CHANGE,
            group_results=(),
            message="已有其他审批执行进程正在运行，未联网、未提交审批",
        )

    try:
        report = _execute_plan_with_lock_held(
            plan, client, allow_partial=allow_partial
        )
        if after_report is not None:
            after_report(plan, report)
        return report
    finally:
        execution_lock.release()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="默认离线校验 SRDPM 审批计划；显式确认后才允许执行"
    )
    parser.add_argument("plan", type=Path, help="审批计划 JSON 文件")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check-live",
        action="store_true",
        help="只读验证 SRDPM 登录，不执行审批",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="在全部安全校验和精确确认后执行真实审批",
    )
    parser.add_argument(
        "--confirm-month",
        metavar="YYYY-MM",
        help="执行时必须精确匹配计划月份",
    )
    parser.add_argument(
        "--confirmation",
        help="受控自动化可提供的精确确认语；必须匹配计划摘要",
    )
    return parser


def _stream_is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, io.UnsupportedOperation):
        return False


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], ApprovalClient] | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    error_stream = error_stream or sys.stderr
    client_factory = client_factory or SRDPMClient.from_env

    try:
        plan = load_plan(args.plan)
        summary = summarize_plan(plan)
        print(format_summary(summary), file=output_stream)

        if not args.execute and (args.confirm_month or args.confirmation):
            raise ConfirmationError(
                "--confirm-month 和 --confirmation 只能与 --execute 一起使用"
            )

        if not args.check_live and not args.execute:
            print("离线模式完成：未读取凭据、未联网、未执行审批。", file=output_stream)
            return 0

        if args.check_live:
            client = client_factory()
            try:
                if not client.check_live():
                    print("只读登录校验失败。", file=error_stream)
                    return 3
                print("只读登录校验通过；未执行审批。", file=output_stream)
                return 0
            finally:
                client.close()

        if args.confirm_month is None:
            raise ConfirmationError("--execute 必须同时提供 --confirm-month YYYY-MM")
        if args.confirm_month != plan.month:
            raise ConfirmationError(
                f"确认月份 {args.confirm_month} 与计划月份 {plan.month} 不一致"
            )

        expected = confirmation_phrase(summary)
        if args.confirmation is not None:
            supplied = args.confirmation
        else:
            if not _stream_is_tty(input_stream):
                raise ConfirmationError(
                    "非 TTY 环境拒绝交互执行；受控自动化必须显式提供 --confirmation"
                )
            print("真实审批不可撤回。请输入以下精确确认语：", file=output_stream)
            print(expected, file=output_stream)
            supplied = input_stream.readline().rstrip("\r\n")

        if supplied != expected:
            raise ConfirmationError("确认语与当前计划摘要不匹配，拒绝执行")

        # 确认全部通过后才创建客户端、读取凭据并联网。
        client = client_factory()
        try:
            report = execute_plan(plan, client)
        finally:
            client.close()

        for result in report.group_results:
            marker = "OK" if result.success else "FAIL"
            print(
                f"[{marker}] {result.date} {result.person} "
                f"{result.id_count} IDs - {result.message}",
                file=output_stream if result.success else error_stream,
            )
        if report.success:
            print(report.message, file=output_stream)
            return 0
        print(report.message, file=error_stream)
        return 4
    except (PlanValidationError, ConfirmationError) as exc:
        print(f"拒绝执行：{exc}", file=error_stream)
        return 2
    except SRDPMError as exc:
        print(f"SRDPM 操作失败：{exc}", file=error_stream)
        return 3 if args.check_live else 4
    except Exception as exc:
        print(f"安全失败：{type(exc).__name__}: {exc}", file=error_stream)
        return 3 if args.check_live else 4


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, io.UnsupportedOperation):
            pass
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
