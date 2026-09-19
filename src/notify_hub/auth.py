"""M-A 访问认证：为整个 ASGI 应用提供统一的凭据校验。

见 ``openspec/changes/add-public-access/architecture.md`` 第 3 节。

契约要点（冻结）：

- 「URL 查询串里的令牌」或「会话 Cookie」二者其一成立即视为已认证，否则一律 ``401``。
- ``/healthz`` 是**唯一**免鉴权路径；未配置令牌（守卫关闭）时全量透传。
- ``AuthGuard`` 是**纯 ASGI 中间件**（不继承 ``BaseHTTPMiddleware``），透传分支一律
  ``await self.app(scope, receive, send)``。
- 该模块**不写日志**：凭据系统里没有任何日志调用，避免自身成为泄漏源
  （因此不得 ``import logging``，也不得出现任何 ``logging.*`` 调用）。
- 依赖仅限标准库，不得导入 ``fastapi`` / ``starlette``，也不得耦合业务路由
  （``notify_hub.web`` / ``notify_hub.api``）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qsl, urlencode

__all__ = [
    "COOKIE_NAME",
    "SESSION_MESSAGE",
    "HEALTH_PATH",
    "session_value",
    "strip_token_param",
    "AuthGuard",
]

#: 会话 Cookie 名。
COOKIE_NAME = "nh_session"

#: HMAC 的固定消息（v1）。
SESSION_MESSAGE = b"notify-hub-session-v1"

#: 唯一免鉴权路径。
HEALTH_PATH = "/healthz"

#: 401 HTML 分型的响应体（不回显请求携带的任何令牌）。
_UNAUTHORIZED_HTML = (
    "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
    "<title>401 Unauthorized</title></head><body>"
    "<h1>401 Unauthorized</h1>"
    "<p>需要有效的访问令牌才能访问本服务。</p>"
    "</body></html>"
).encode("utf-8")

#: 401 JSON 分型的响应体。
_UNAUTHORIZED_JSON = json.dumps({"detail": "unauthorized"}).encode("utf-8")


def session_value(token: str) -> str:
    """把访问令牌换成定长的会话值：``HMAC-SHA256(token, SESSION_MESSAGE)`` 的小写十六进制。

    纯函数——同一 ``token`` 恒返回同一值（64 位小写十六进制），且**不是**令牌本体的
    任何可逆编码。``token`` 为空串时抛 ``ValueError``（调用方必须先判空）。
    """
    if not token:
        raise ValueError("token 不得为空串")
    return hmac.new(token.encode("utf-8"), SESSION_MESSAGE, hashlib.sha256).hexdigest()


def strip_token_param(query_string: str) -> str:
    """去掉查询串中**所有** ``key == "token"`` 的键值对，保留其余键值对的原有顺序。

    入参是不带 ``?`` 的原始查询串。用 ``parse_qsl`` 解析、``urlencode`` 重新编码，
    因此编码形式可能被规范化（这是允许的）。无 ``token`` 键时返回等价重编码；
    输入为空串返回 ``""``。
    """
    pairs = [
        (key, value)
        for key, value in parse_qsl(query_string, keep_blank_values=True)
        if key != "token"
    ]
    return urlencode(pairs)


def _first_query_token(query_string: str) -> str | None:
    """取查询串中**第一个** ``key == "token"`` 的值；空串视为未提供（返回 ``None``）。"""
    for key, value in parse_qsl(query_string, keep_blank_values=True):
        if key == "token":
            return value or None
    return None


def _cookie_value(headers: list[tuple[bytes, bytes]]) -> str | None:
    """把全部 ``cookie`` 请求头用 ``"; "`` 连接后解析，取 :data:`COOKIE_NAME` 的值。

    解析异常一律捕获并视为未提供。空值同样视为未提供。
    """
    parts = [
        value.decode("latin-1")
        for name, value in headers
        if name.lower() == b"cookie"
    ]
    if not parts:
        return None
    try:
        cookies = SimpleCookie()
        cookies.load("; ".join(parts))
        morsel = cookies.get(COOKIE_NAME)
    except Exception:  # noqa: BLE001 - 畸形 Cookie 头必须降级为「未提供」
        return None
    if morsel is None:
        return None
    return morsel.value or None


def _header_values(headers: list[tuple[bytes, bytes]], name: bytes) -> list[str]:
    """取全部同名请求头的值（按 ``latin-1`` 解码）。"""
    return [
        value.decode("latin-1")
        for key, value in headers
        if key.lower() == name
    ]


@dataclass(frozen=True)
class AuthGuard:
    """统一凭据校验中间件（纯 ASGI）。

    ``app`` 字段**必须排在第一**：Starlette 的 ``add_middleware`` 最终以
    ``cls(app, *args, **kwargs)`` **位置传参**构造中间件，被包裹的应用必然落在第一个
    字段上。一切构造一律用关键字（``AuthGuard(token=T)``），禁止位置传参。

    ``token`` 在**构造时**完成一次 ``strip()`` 归一化：``None`` 或去空白后为空 →
    字段置 ``None``、守卫关闭；否则字段置 ``strip()`` 之后的值。一经构造即不可变。
    """

    app: Any = None
    token: str | None = None

    def __post_init__(self) -> None:
        raw = self.token
        normalized = raw.strip() if isinstance(raw, str) else None
        if normalized == "":
            normalized = None
        object.__setattr__(self, "token", normalized)

    @property
    def enabled(self) -> bool:
        """``token`` 非空即启用守卫。"""
        return self.token is not None

    def expected_cookie(self) -> str:
        """返回本守卫期望的会话 Cookie 值；守卫关闭时抛 ``RuntimeError``。"""
        if self.token is None:
            raise RuntimeError("守卫已关闭（未配置令牌），没有期望的会话 Cookie")
        return session_value(self.token)

    def is_authorized(self, *, cookie_value: str | None, query_token: str | None) -> bool:
        """判定一次请求是否已认证（守卫关闭时恒 ``True``）。

        比较前双方都 ``.encode("utf-8")`` 成 bytes：``hmac.compare_digest`` 对非 ASCII
        的 ``str`` 会抛 ``TypeError``。
        """
        expected = self.token
        if expected is None:
            return True
        if query_token and hmac.compare_digest(
            query_token.encode("utf-8"), expected.encode("utf-8")
        ):
            return True
        return bool(cookie_value) and hmac.compare_digest(
            cookie_value.encode("utf-8"), self.expected_cookie().encode("utf-8")
        )

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        """ASGI 入口：按规格第 3.2 节的判定顺序 1–7 处理一次请求。"""
        # 1. 非 HTTP（websocket / lifespan）直接透传。
        if scope.get("type") != "http":
            await self._forward(scope, receive, send)
            return

        # 2. 守卫关闭 → 透传（不做任何凭据解析，零开销路径）。
        if self.token is None:
            await self._forward(scope, receive, send)
            return

        # 3. 唯一免鉴权路径。
        if scope.get("path") == HEALTH_PATH:
            await self._forward(scope, receive, send)
            return

        headers = scope.get("headers") or []
        query_string = (scope.get("query_string") or b"").decode("latin-1")
        query_token = _first_query_token(query_string)
        cookie_value = _cookie_value(headers)

        authenticated = self.is_authorized(
            cookie_value=cookie_value, query_token=query_token
        )

        if authenticated:
            accept = ", ".join(_header_values(headers, b"accept"))
            method = scope.get("method", "")
            # 5. 浏览器导航信号：查询令牌 + GET + Accept 含 text/html → 303 + 种 Cookie。
            if (
                query_token
                and method.upper() == "GET"
                and "text/html" in accept.lower()
            ):
                await self._redirect(scope, query_string, send)
                return
            # 6. 已认证但不满足第 5 步（严格补集）→ 透传，不种 Cookie。
            await self._forward(scope, receive, send)
            return

        # 7. 未认证 → 401，按 Accept 分型。
        await self._unauthorized(headers, send)

    async def _forward(self, scope: dict, receive: Any, send: Any) -> None:
        """透传到内层应用；``app is None`` 时抛 ``RuntimeError``，不得静默放行。"""
        if self.app is None:
            raise RuntimeError("AuthGuard 未绑定内层应用（app is None），无法透传")
        await self.app(scope, receive, send)

    async def _redirect(self, scope: dict, query_string: str, send: Any) -> None:
        """第 5 步：303 回原路径并种下会话 Cookie（**不得**带 ``Secure``）。"""
        path = scope.get("path", "")
        remaining = strip_token_param(query_string)
        location = f"{path}?{remaining}" if remaining else path
        headers = [
            (b"location", location.encode("latin-1")),
            (
                b"set-cookie",
                f"{COOKIE_NAME}={self.expected_cookie()}; HttpOnly; Path=/; SameSite=Lax".encode(
                    "latin-1"
                ),
            ),
            (b"content-length", b"0"),
        ]
        await send(
            {"type": "http.response.start", "status": 303, "headers": headers}
        )
        await send({"type": "http.response.body", "body": b""})

    async def _unauthorized(self, headers: list[tuple[bytes, bytes]], send: Any) -> None:
        """第 7 步：401；``Accept`` 含 ``text/html`` 走 HTML，否则一律 JSON。"""
        accept = ", ".join(_header_values(headers, b"accept")).lower()
        if "text/html" in accept:
            content_type = b"text/html; charset=utf-8"
            body = _UNAUTHORIZED_HTML
        else:
            content_type = b"application/json"
            body = _UNAUTHORIZED_JSON
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", content_type),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
