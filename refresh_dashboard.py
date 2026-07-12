#!/usr/bin/env python3
"""安全刷新当前月 SRDPM 数据并重新生成本地看板。

这个入口故意不导入、也不调用任何审批执行层。它只做以下事情：

1. 从当前 Windows 用户的 Credential Manager 读取已验证的凭据；
2. 复用 ``fetch_and_audit.py`` 的只读抓取和本地审计；
3. 复用 ``build_multi_month_dashboard.py`` 生成看板；
4. 所有工作先在项目内临时目录完成，全部成功后才在刷新锁中发布。

命令行没有审批参数；默认且只能刷新当前本地日历月。
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Any, Callable, Iterator, Protocol

from apply_approval_plan import _try_acquire_execution_lock
from windows_credential_store import WindowsCredentialStore


PROJECT_DIR = Path(__file__).resolve().parent
ARCHIVE_DIR_NAME = "srdpm_archive"
DASHBOARD_NAME = "工时审批看板_多月.html"
MAPPING_NAME = "project_mapping.json"
REFRESH_FILE_LOCK_NAME = ".srdpm-refresh.lock"
REFRESH_MUTEX_NAME = r"Local\SRDPMDashboardRefresh-v1"
READ_ONLY_FETCH_PATHS = frozenset({"userList", "list", "statistics", "detail"})
REQUIRED_MONTH_ARTIFACTS = ("raw_data.json", "audit_report.json", "audit_report.md")


class RefreshError(RuntimeError):
    """可安全显示给本地操作者的刷新失败。"""


class RefreshBusyError(RefreshError):
    """已有刷新在运行，拒绝并行覆盖本地归档。"""


class RefreshCredentialError(RefreshError):
    """当前 Windows 用户没有可供刷新使用的凭据。"""


class RefreshPublishError(RefreshError):
    """暂存已完成，但原子发布或回滚失败。"""


class RefreshLockHandle(Protocol):
    def release(self) -> None: ...


class _WindowsRefreshMutexHandle:
    def __init__(self, kernel32: Any, handle: Any) -> None:
        self._kernel32 = kernel32
        self._handle = handle
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            self._kernel32.ReleaseMutex(handle)
        finally:
            self._kernel32.CloseHandle(handle)


class _ProcessRefreshLockHandle:
    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._lock.release()


_FALLBACK_REFRESH_LOCK = threading.Lock()


def try_acquire_refresh_lock() -> RefreshLockHandle | None:
    """Acquire the per-user refresh lock without waiting.

    Windows uses a named mutex so a scheduled task and an interactive run cannot
    publish over each other.  The non-Windows fallback only exists for offline
    tests and is intentionally process-local.
    """

    if os.name != "nt":
        if not _FALLBACK_REFRESH_LOCK.acquire(blocking=False):
            return None
        return _ProcessRefreshLockHandle(_FALLBACK_REFRESH_LOCK)

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

    handle = kernel32.CreateMutexW(None, False, REFRESH_MUTEX_NAME)
    if not handle:
        raise RefreshError("无法创建本机刷新锁")

    wait_object_0 = 0x00000000
    wait_abandoned = 0x00000080
    wait_timeout = 0x00000102
    result = kernel32.WaitForSingleObject(handle, 0)
    if result == wait_object_0:
        return _WindowsRefreshMutexHandle(kernel32, handle)
    if result == wait_timeout:
        kernel32.CloseHandle(handle)
        return None
    if result == wait_abandoned:
        # 上次刷新可能在发布中异常退出；取得锁后仍安全失败，不能继续覆盖。
        try:
            kernel32.ReleaseMutex(handle)
        finally:
            kernel32.CloseHandle(handle)
        raise RefreshError("检测到上一次刷新异常终止，请先人工核对本地归档")

    error_code = ctypes.get_last_error()
    kernel32.CloseHandle(handle)
    raise RefreshError(f"无法获取本机刷新锁（error={error_code}）")


@dataclass(frozen=True)
class RefreshResult:
    month: str
    published_paths: tuple[Path, ...]


@dataclass(frozen=True)
class _Artifact:
    source: Path
    target: Path


@dataclass(frozen=True)
class _RollbackRecord:
    artifact: _Artifact
    existed: bool
    backup: Path | None


def current_month(now: datetime | None = None) -> tuple[int, int, str]:
    """Return the local calendar month used by the weekly task."""

    current = now or datetime.now()
    if not isinstance(current, datetime):
        raise TypeError("now 必须是 datetime")
    return current.year, current.month, f"{current.year:04d}-{current.month:02d}"


@contextmanager
def _temporary_module_attributes(module: Any, values: dict[str, Any]) -> Iterator[None]:
    previous = {name: getattr(module, name) for name in values}
    try:
        for name, value in values.items():
            setattr(module, name, value)
        yield
    finally:
        for name, value in previous.items():
            setattr(module, name, value)


def _load_refresh_credentials(credential_store: Any) -> Any:
    try:
        credentials = credential_store.load()
    except Exception as exc:
        raise RefreshCredentialError("Windows 凭据管理器暂不可用，未开始刷新") from exc

    if credentials is None:
        raise RefreshCredentialError(
            "未配置 SRDPM 凭据；请先在本机看板 UI 中完成一次账号校验"
        )
    username = getattr(credentials, "username", "")
    password = getattr(credentials, "password", "")
    if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
        raise RefreshCredentialError("Windows 凭据内容无效，未开始刷新")
    return credentials


def _copy_stage_snapshot(project_dir: Path, stage_root: Path) -> Path:
    archive_root = project_dir / ARCHIVE_DIR_NAME
    stage_archive = stage_root / ARCHIVE_DIR_NAME
    if archive_root.exists():
        if not archive_root.is_dir():
            raise RefreshError("本地归档路径不是目录，未开始刷新")
        shutil.copytree(archive_root, stage_archive)
    else:
        stage_archive.mkdir(parents=True)

    mapping_source = project_dir / MAPPING_NAME
    if not mapping_source.is_file():
        raise RefreshError("项目映射文件不存在，未开始刷新")
    shutil.copy2(mapping_source, stage_root / MAPPING_NAME)
    return stage_archive


def _validate_staged_month(stage_archive: Path, month: str) -> None:
    month_dir = stage_archive / month
    raw_path = month_dir / "raw_data.json"
    audit_path = month_dir / "audit_report.json"
    md_path = month_dir / "audit_report.md"

    for path in (raw_path, audit_path, md_path):
        if not path.is_file() or path.stat().st_size <= 0:
            raise RefreshError("暂存数据不完整，未发布旧归档")

    try:
        raw_data = json.loads(raw_path.read_text(encoding="utf-8"))
        audit_data = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RefreshError("暂存 JSON 校验失败，未发布旧归档") from exc
    if not isinstance(raw_data, dict) or not isinstance(raw_data.get("daily_data"), dict):
        raise RefreshError("暂存原始数据结构异常，未发布旧归档")
    if not isinstance(audit_data, dict) or audit_data.get("month") != month:
        raise RefreshError("暂存审计结果结构异常，未发布旧归档")


def _validate_staged_dashboard(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RefreshError("暂存看板未生成，未发布旧页面")
    try:
        prefix = path.read_text(encoding="utf-8")[:256].lower()
    except (OSError, UnicodeError) as exc:
        raise RefreshError("暂存看板无法读取，未发布旧页面") from exc
    if "<html" not in prefix:
        raise RefreshError("暂存看板格式异常，未发布旧页面")


def _assert_fetch_read_only_contract(fetch_module: Any) -> None:
    declared = getattr(fetch_module, "READ_ONLY_API_PATHS", READ_ONLY_FETCH_PATHS)
    try:
        declared_paths = frozenset(declared)
    except TypeError as exc:
        raise RefreshError("抓取模块读取接口声明无效") from exc
    if declared_paths != READ_ONLY_FETCH_PATHS:
        raise RefreshError("抓取模块读取接口声明不符合只读约束")


def _run_staged_fetch_and_audit(
    *,
    fetch_module: Any,
    stage_root: Path,
    stage_archive: Path,
    year: int,
    month: int,
    credentials: Any,
) -> None:
    _assert_fetch_read_only_contract(fetch_module)
    session: Any | None = None
    try:
        with _temporary_module_attributes(
            fetch_module,
            {
                "OUT_DIR": str(stage_root),
                "ARCHIVE_DIR": str(stage_archive),
            },
        ):
            # Credentials are passed as in-memory function arguments.  They are never
            # placed in os.environ, staging files, task arguments, or log messages.
            session = fetch_module.login_srdpm(credentials.username, credentials.password)
            result = fetch_module.fetch_month(session, year, month)
            if not result:
                raise RefreshError("抓取未完成，未发布旧归档")
            fetch_module.run_audit(year, month, result)
    finally:
        if session is not None:
            close = getattr(session, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    # Closing a read-only local HTTP session cannot change archive state.
                    pass


def _run_staged_dashboard(
    *, dashboard_module: Any, stage_root: Path, stage_archive: Path
) -> Path:
    output_path = stage_root / DASHBOARD_NAME
    with _temporary_module_attributes(
        dashboard_module,
        {
            "OUT_DIR": str(stage_root),
            "ARCHIVE_DIR": str(stage_archive),
            "OUTPUT_HTML": str(output_path),
        },
    ):
        dashboard_module.main()
    _validate_staged_dashboard(output_path)
    return output_path


def _atomic_copy_replace(source: Path, target: Path) -> None:
    """Copy a staged file to a same-directory temp file, then atomically replace."""

    if not source.is_file():
        raise RefreshPublishError("待发布的暂存文件不存在")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.refresh-",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output_file:
            shutil.copyfileobj(input_file, output_file)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, target)
    finally:
        if temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def _restore_published_artifact(record: _RollbackRecord) -> None:
    target = record.artifact.target
    if record.existed:
        assert record.backup is not None
        _atomic_copy_replace(record.backup, target)
    else:
        target.unlink(missing_ok=True)


def _publish_staged_artifacts(artifacts: tuple[_Artifact, ...], stage_root: Path) -> None:
    """Publish files with rollback if a later replacement fails."""

    rollback_root = stage_root / "rollback"
    rollback_root.mkdir(parents=True, exist_ok=False)
    records: list[_RollbackRecord] = []
    for index, artifact in enumerate(artifacts):
        if artifact.target.exists() and not artifact.target.is_file():
            raise RefreshPublishError("发布目标不是普通文件")
        backup: Path | None = None
        existed = artifact.target.is_file()
        if existed:
            backup = rollback_root / f"{index:02d}-{artifact.target.name}"
            shutil.copy2(artifact.target, backup)
        records.append(_RollbackRecord(artifact, existed, backup))

    published: list[_RollbackRecord] = []
    try:
        for record in records:
            _atomic_copy_replace(record.artifact.source, record.artifact.target)
            published.append(record)
    except Exception as original_error:
        rollback_failures: list[Exception] = []
        for record in reversed(published):
            try:
                _restore_published_artifact(record)
            except Exception as rollback_error:  # pragma: no cover - exceptional I/O path
                rollback_failures.append(rollback_error)
        if rollback_failures:
            raise RefreshPublishError("发布失败，且无法完整回滚，请人工核对本地归档") from original_error
        raise RefreshPublishError("发布失败，已回滚到刷新前数据") from original_error


@contextmanager
def _refresh_file_lock(project_dir: Path) -> Iterator[None]:
    """Expose a non-sensitive, full-refresh guard for the approval service.

    The named mutex serializes refresh processes and the shared execution mutex
    excludes real submissions. This visible lock additionally lets the local
    approval service reject a new prepare request throughout fetch, audit, page
    generation, and publish, so an old snapshot cannot overwrite a newer approval.
    """

    lock_path = project_dir / REFRESH_FILE_LOCK_NAME
    payload = json.dumps(
        {
            "schema_version": 1,
            "pid": os.getpid(),
            "created_at": datetime.now().astimezone().isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        descriptor = os.open(
            lock_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise RefreshBusyError("检测到刷新锁，本次未开始") from exc
    except OSError as exc:
        raise RefreshError("无法创建刷新锁，未开始") from exc

    try:
        with os.fdopen(descriptor, "wb") as lock_file:
            lock_file.write(payload)
            lock_file.flush()
            os.fsync(lock_file.fileno())
        yield
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            # A remaining lock is fail-closed: the approval service will keep
            # rejecting new prepares until the operator investigates it.
            pass


def refresh_current_month(
    *,
    project_dir: Path | str = PROJECT_DIR,
    now: datetime | None = None,
    credential_store: Any | None = None,
    fetch_module: Any | None = None,
    dashboard_module: Any | None = None,
    lock_factory: Callable[[], RefreshLockHandle | None] = try_acquire_refresh_lock,
    execution_lock_factory: Callable[[], RefreshLockHandle | None] = _try_acquire_execution_lock,
) -> RefreshResult:
    """Refresh the current calendar month, or leave every published file unchanged."""

    if fetch_module is None:
        import fetch_and_audit as fetch_module
    if dashboard_module is None:
        import build_multi_month_dashboard as dashboard_module

    resolved_project = Path(project_dir).resolve()
    if not resolved_project.is_dir():
        raise RefreshError("项目目录不存在，未开始刷新")
    year, month_number, month = current_month(now)

    try:
        lock = lock_factory()
    except RefreshError:
        raise
    except Exception as exc:
        raise RefreshError("无法获取本机刷新锁") from exc
    if lock is None:
        raise RefreshBusyError("已有刷新任务正在运行，本次未开始")

    try:
        try:
            execution_lock = execution_lock_factory()
        except Exception as exc:
            raise RefreshError("无法获取审批执行互斥锁，未开始刷新") from exc
        if execution_lock is None:
            raise RefreshBusyError("真实审批任务正在执行，本次未开始")

        try:
            # The file lock is deliberately created before credential access and
            # remains until every staged artifact is either published or discarded.
            with _refresh_file_lock(resolved_project):
                selected_store = (
                    credential_store if credential_store is not None else WindowsCredentialStore()
                )
                credentials = _load_refresh_credentials(selected_store)
                with tempfile.TemporaryDirectory(
                    prefix=".srdpm-refresh-stage-", dir=resolved_project
                ) as temporary_dir:
                    stage_root = Path(temporary_dir)
                    stage_archive = _copy_stage_snapshot(resolved_project, stage_root)
                    try:
                        _run_staged_fetch_and_audit(
                            fetch_module=fetch_module,
                            stage_root=stage_root,
                            stage_archive=stage_archive,
                            year=year,
                            month=month_number,
                            credentials=credentials,
                        )
                        _validate_staged_month(stage_archive, month)
                        staged_dashboard = _run_staged_dashboard(
                            dashboard_module=dashboard_module,
                            stage_root=stage_root,
                            stage_archive=stage_archive,
                        )
                    except RefreshError:
                        raise
                    except Exception as exc:
                        raise RefreshError("抓取、审计或看板生成失败，旧数据未修改") from exc

                    target_month_dir = resolved_project / ARCHIVE_DIR_NAME / month
                    artifacts = tuple(
                        _Artifact(stage_archive / month / name, target_month_dir / name)
                        for name in REQUIRED_MONTH_ARTIFACTS
                    ) + (_Artifact(staged_dashboard, resolved_project / DASHBOARD_NAME),)
                    _publish_staged_artifacts(artifacts, stage_root)
                    return RefreshResult(month=month, published_paths=tuple(a.target for a in artifacts))
        finally:
            execution_lock.release()
    finally:
        lock.release()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="刷新当前月 SRDPM 数据并生成本地看板")
    parser.add_argument("--quiet", action="store_true", help="仅通过退出码返回结果")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = refresh_current_month()
    except RefreshError as exc:
        if not args.quiet:
            print(f"刷新失败：{exc}")
        return 1
    if not args.quiet:
        print(f"刷新完成：{result.month}；已更新本地归档和 {DASHBOARD_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
