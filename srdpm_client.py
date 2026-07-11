"""SRDPM 的最小网络客户端。

本模块只负责认证、只读状态查询和显式审批请求。导入模块不会读取凭据、
启动浏览器或访问网络；只有调用 ``from_env().check_live()`` 或其他实例方法
时才会连接 SRDPM。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import requests


LOGIN_URL = "https://rd-mokadisplay.tcl.com/srdpm/#/work-list"
API_BASE = "https://rd-mokadisplay.tcl.com/srdpm-api/workload/approve"
SRDPM_SCHEME = "https"
SRDPM_HOST = "rd-mokadisplay.tcl.com"


class SRDPMError(RuntimeError):
    """SRDPM 客户端的安全、认证或 API 错误。"""


class ConfigurationError(SRDPMError):
    """本机配置缺失或不合法。"""


class AuthenticationError(SRDPMError):
    """无法建立通过认证的会话。"""


class APIError(SRDPMError):
    """SRDPM API 返回异常或不符合预期。"""


def _read_verify_tls() -> bool:
    """TLS 默认开启，只有精确设置 false 才允许关闭。"""

    raw = os.environ.get("SRDPM_VERIFY_TLS")
    if raw is None or not raw.strip():
        return True
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ConfigurationError("SRDPM_VERIFY_TLS 只允许设置为 true 或 false")


def _normalize_approve_id(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not text or not text.isdigit() or int(text) <= 0:
        return None
    return text


def _dedupe_ids(values: Iterable[Any]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        approve_id = _normalize_approve_id(value)
        if approve_id is None or approve_id in seen:
            continue
        seen.add(approve_id)
        result.append(approve_id)
    return tuple(result)


def _is_target_url(url: Any, path_prefix: str) -> bool:
    """只接受 SRDPM 的精确 HTTPS 主机、默认端口和预期路径。"""

    try:
        parsed = urlsplit(str(url))
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme.lower() == SRDPM_SCHEME
        and (parsed.hostname or "").lower() == SRDPM_HOST
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith(path_prefix)
    )


def _is_target_cookie_domain(value: Any) -> bool:
    domain = str(value or "").strip().lower().lstrip(".").rstrip(".")
    return domain == SRDPM_HOST


def _normalize_user_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise APIError("user_id 不合法")
    text = str(value).strip()
    if not text or len(text) > 128 or any(ord(ch) < 32 for ch in text):
        raise APIError("user_id 不合法")
    return text


@dataclass
class SRDPMClient:
    """由环境变量创建、延迟登录的 SRDPM 客户端。"""

    _username: str = field(repr=False)
    _password: str = field(repr=False)
    verify_tls: bool = True
    timeout_seconds: int = 30
    _session: requests.Session | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_env(cls) -> "SRDPMClient":
        username = os.environ.get("SRDPM_USERNAME", "").strip()
        password = os.environ.get("SRDPM_PASSWORD", "")
        if not username or not password:
            raise ConfigurationError(
                "缺少 SRDPM_USERNAME 或 SRDPM_PASSWORD 环境变量"
            )
        return cls(
            _username=username,
            _password=password,
            verify_tls=_read_verify_tls(),
        )

    def login(self) -> None:
        """通过 Playwright 登录并建立 requests 会话，不输出任何凭据。"""

        if self._session is not None:
            return

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 由部署环境决定
            raise ConfigurationError(
                "缺少 Playwright；请安装依赖并执行 playwright install chromium"
            ) from exc

        captured_token: dict[str, str] = {}
        cookies: list[Mapping[str, Any]] = []

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    ignore_https_errors=not self.verify_tls,
                    viewport={"width": 1440, "height": 900},
                )
                page = context.new_page()

                def capture_token(request: Any) -> None:
                    if not _is_target_url(request.url, "/srdpm-api/"):
                        return
                    token = request.headers.get("accesstoken", "")
                    if token and "value" not in captured_token:
                        captured_token["value"] = token

                page.on("request", capture_token)
                page.goto(
                    LOGIN_URL,
                    timeout=60_000,
                    wait_until="domcontentloaded",
                )
                if not _is_target_url(page.url, "/srdpm/"):
                    raise AuthenticationError("SRDPM 登录页跳转到了非目标主机")
                page.locator('input[name="username"]').fill(self._username)
                page.locator('input[name="password"]').fill(self._password)
                page.locator('button:has-text("登录")').click()
                page.wait_for_timeout(5_000)

                # 触发一次只读列表请求，以捕获应用实际使用的 accesstoken。
                filter_button = page.locator('button:has-text("筛选")')
                if filter_button.count() > 0:
                    filter_button.first.click()
                    page.wait_for_timeout(3_000)

                if not _is_target_url(page.url, "/srdpm/"):
                    raise AuthenticationError("SRDPM 登录后跳转到了非目标主机")

                cookies = [
                    cookie
                    for cookie in context.cookies()
                    if _is_target_cookie_domain(cookie.get("domain"))
                ]
                context.close()
                browser.close()
        except Exception as exc:
            raise AuthenticationError(
                f"SRDPM 登录失败（{type(exc).__name__}）"
            ) from exc

        token = captured_token.get("value", "")
        if not token or not cookies:
            raise AuthenticationError("SRDPM 登录后未获得有效认证会话")

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json;charset=UTF-8",
                "accesstoken": token,
                "Referer": "https://rd-mokadisplay.tcl.com/srdpm/",
                "Origin": "https://rd-mokadisplay.tcl.com",
            }
        )
        for cookie in cookies:
            name = str(cookie.get("name", ""))
            value = str(cookie.get("value", ""))
            if not name:
                continue
            session.cookies.set(
                name,
                value,
                domain=cookie.get("domain") or None,
                path=cookie.get("path") or "/",
            )
        self._session = session

    def check_live(self) -> bool:
        """只验证登录会话；不提交任何审批请求。"""

        self.login()
        return True

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def _post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.login()
        assert self._session is not None
        url = f"{API_BASE}/{path}"
        try:
            response = self._session.post(
                url,
                json=dict(payload),
                timeout=self.timeout_seconds,
                verify=self.verify_tls,
                allow_redirects=False,
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise APIError(
                f"SRDPM API 请求失败：{path}（{type(exc).__name__}）"
            ) from exc
        except ValueError as exc:
            raise APIError(f"SRDPM API 返回非 JSON：{path}") from exc

        if not isinstance(body, Mapping):
            raise APIError(f"SRDPM API 返回结构异常：{path}")
        if str(body.get("code")) != "200":
            raise APIError(f"SRDPM API 返回失败状态：{path} code={body.get('code')}")
        return body

    def _list_status(
        self, date: str, status: str, user_id: str | None = None
    ) -> list[Mapping[str, Any]]:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise APIError(f"日期格式必须为 YYYY-MM-DD：{date}") from exc
        if status not in {"0", "2", "3"}:
            raise APIError(f"不允许查询审批状态：{status}")
        normalized_user_id = _normalize_user_id(user_id)

        page = 1
        page_size = 200
        records: list[Mapping[str, Any]] = []
        while page <= 1_000:
            body = self._post(
                "list",
                {
                    "page": page,
                    "page_size": page_size,
                    "type": None,
                    "status": status,
                    "time": date,
                    "user_id": normalized_user_id,
                    "group_id": None,
                },
            )
            container = body.get("data")
            if not isinstance(container, Mapping):
                raise APIError("SRDPM list.data 结构异常")
            batch = container.get("data", [])
            if not isinstance(batch, list):
                raise APIError("SRDPM list.data.data 结构异常")
            if not all(isinstance(item, Mapping) for item in batch):
                raise APIError("SRDPM list 返回了非对象记录")
            records.extend(batch)

            try:
                total = int(container.get("total", 0) or 0)
                per_page = int(container.get("per_page", page_size) or page_size)
            except (TypeError, ValueError) as exc:
                raise APIError("SRDPM list 分页字段异常") from exc
            if not batch or len(records) >= total or len(batch) < per_page:
                return records
            page += 1

        raise APIError("SRDPM list 分页超过安全上限")

    @staticmethod
    def _children_for_person(
        records: Sequence[Mapping[str, Any]],
        person: str,
        user_id: str | None = None,
    ) -> Iterable[Mapping[str, Any]]:
        normalized_user_id = _normalize_user_id(user_id)
        for parent in records:
            if normalized_user_id is not None:
                parent_ids = {
                    str(parent.get(field)).strip()
                    for field in ("uid", "user_id")
                    if parent.get(field) is not None
                    and str(parent.get(field)).strip()
                }
                if len(parent_ids) > 1:
                    raise APIError("SRDPM 人员记录中的 uid/user_id 冲突")
                if normalized_user_id not in parent_ids:
                    continue
            elif str(parent.get("cn_name", "")).strip() != person:
                continue
            children = parent.get("children", [])
            if not isinstance(children, list):
                raise APIError("SRDPM children 结构异常")
            for child in children:
                if not isinstance(child, Mapping):
                    raise APIError("SRDPM child 结构异常")
                yield child

    def get_pending_ids(
        self, person: str, date: str, user_id: str | None = None
    ) -> tuple[str, ...]:
        """从 status=0 的实时结果中提取指定人员当天的待审 ID。"""

        pending_values = {"", "0", "待审", "待审核", "pending"}
        ids: list[str] = []
        records = self._list_status(date, "0", user_id)
        for child in self._children_for_person(records, person, user_id):
            state = str(child.get("status", "")).strip().lower()
            if state not in pending_values:
                continue
            approve_id = _normalize_approve_id(child.get("approve_id"))
            if approve_id is not None:
                ids.append(approve_id)
        return _dedupe_ids(ids)

    def approve_ids(self, approve_ids: Sequence[str]) -> None:
        """一次提交一个人员+日期组的 ID；调用方负责所有安全校验。"""

        ids = _dedupe_ids(approve_ids)
        if not ids:
            raise APIError("审批 ID 集合为空")
        self._post("approval", {"approve_id": ",".join(ids)})

    def get_approved_statuses(
        self, person: str, date: str, user_id: str | None = None
    ) -> dict[str, str]:
        """重拉 status=2/3，并返回 ID 到真实状态的映射。"""

        result: dict[str, str] = {}
        for status_filter in ("2", "3"):
            records = self._list_status(date, status_filter, user_id)
            for child in self._children_for_person(records, person, user_id):
                approve_id = _normalize_approve_id(child.get("approve_id"))
                if approve_id is None:
                    continue
                state = str(child.get("status", "")).strip()
                previous = result.get(approve_id)
                if previous is not None and previous != state:
                    result[approve_id] = "__CONFLICT__"
                else:
                    result[approve_id] = state
        return result


__all__ = [
    "APIError",
    "AuthenticationError",
    "ConfigurationError",
    "SRDPMClient",
    "SRDPMError",
]
