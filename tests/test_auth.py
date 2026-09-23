"""M-A 模块测试：「访问认证」。

依据（**唯一来源**）：``openspec/changes/add-public-access/architecture.md``
第 2 节（阶段 0 共享文件冻结语义）、第 3.1/3.2 节（功能与接口契约）、第 3.4 节（验证方法）。

实现 ``src/notify_hub/auth.py`` 在本阶段**尚不存在**，这是刻意的：本文件先于实现写好，
红的结果是「实现缺失」的证据。因此：

- 顶部**不做**对 ``notify_hub.auth`` 的模块级导入（否则 pytest 会在**收集阶段**报
  ImportError，连带影响其他测试文件）。所有对 ``auth`` 的导入写在测试函数体内。
- ``_auth()`` 只捕获 ``ModuleNotFoundError``：模块不存在是预期的红；模块存在但符号
  缺失（如漏了 ``AuthGuard``）**不**被转成 ``pytest.fail``，而是照常报错。

模块级测试的装配方式照抄 §3.4 的冻结配方（``FastAPI`` + 真实 ``create_api_router`` /
``create_web_router`` + ``add_middleware(AuthGuard, token=...)``），**不**使用 ``create_app``：
后者要到阶段 B 末尾才装载本中间件。

**规格修订 R1（§6）落实点**：① 一切 ``AuthGuard`` 构造与 ``is_authorized`` 调用一律用关键字
（禁止位置传参——Starlette 以 ``cls(app, *args, **kwargs)`` 构造，位置传参会静默失效）；
② 第 5 步（已认证 + 查询令牌 + GET + ``Accept`` 含 ``text/html`` → 303）**与路径无关**，
第 6 步是其严格补集；③ 401 分型中 ``Accept: */*`` 与缺省 ``Accept`` 均归 JSON；
④ 畸形 ``Cookie`` 头只断言最终 ``401`` 且非 ``5xx``，不区分内部分支。

**规格修订 R2（§6）落实点**：① 构造时一次 ``strip()`` 归一化令牌（与
``config._parse_auth_token`` 一致，不得推迟到请求期）；② 不可变性＝对字段赋值抛
``dataclasses.FrozenInstanceError``；③ 「不写日志」＝对 ``auth.__file__`` 源码做 AST 检查
（无 ``import logging``、无任何 ``logging.*`` 调用）。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import quote

import pytest

# 共享脱敏常量：用于断言响应体不回显凭据（见 tests/conftest.py）。
from conftest import FAKE_TOKEN

TOKEN = "SECRETTOKEN"
OTHER_TOKEN = "OTHER-TOKEN-xyz"
NON_ASCII_TOKEN = "令牌-secret"
# 合法但非 ASCII 的 Cookie 值（urlsafe base64 形状的 HMAC 摘要），用于验证比较前双方
# 都 .encode("utf-8") 成 bytes —— 直接 compare_digest(str, str) 会抛 TypeError。
NON_ASCII_COOKIE = "abc-_DEF.ghi"

#: 必须留空（不得含 token=）的 HTTP 响应头。
_SECRET_HEADERS = ("set-cookie", "location")

V1 = b"notify-hub-session-v1"


# --------------------------------------------------------------------------- #
# 惰性导入与 ASGI 观测工具
# --------------------------------------------------------------------------- #
def _auth():
    """惰性导入 M-A；仅有「模块不存在」被转成带说明的失败。"""
    try:
        import notify_hub.auth as mod  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover - 阶段 A 必然走这条
        pytest.fail(
            f"src/notify_hub/auth.py 尚不存在（阶段 A 的预期红）：{exc}",
            pytrace=False,
        )
    return mod


class _SentinelApp:
    """记录是否被透传调用的内层 ASGI 应用。"""

    def __init__(self, status: int = 200, body: bytes = b"INNER-APP") -> None:
        self.calls: list[dict] = []
        self._status = status
        self._body = body

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        self.calls.append(scope)
        await send(
            {
                "type": "http.response.start",
                "status": self._status,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": self._body})


class _RecordingApp:
    """合规的内层 ASGI 应用：只记录 ``(scope, receive, send)`` 三者身份，从不发送消息。

    用于观测非 http scope 的「原样透传」：``lifespan`` / ``websocket`` 应用本就不该发
    ``http.response.*``，所以守卫**自己不包装 ``send``** 时，外层不会看到任何消息。
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        self.calls.append((scope, receive, send))


def _scope(
    path: str = "/todos",
    *,
    query: bytes = b"",
    headers=(),
    method: str = "GET",
    type_: str = "http",
) -> dict:
    return {
        "type": type_,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": query,
        "root_path": "",
        "headers": [(k.lower(), v) for k, v in headers],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }


async def _drive(guard, scope) -> list[dict]:
    """驱动一次 ASGI 调用，返回全部 send 消息。"""
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await guard(scope, receive, send)
    return sent


def _run(guard, scope) -> list[dict]:
    """同步化包装：本仓未安装 pytest-asyncio，测试函数用 ``asyncio.run`` 驱动 ASGI 调用。

    每个测试函数内部仍是一条协程链、单个事件循环，因此「内层应用是否被调用」这类
    顺序断言与真实运行一致。
    """
    return asyncio.run(_drive(guard, scope))


def _status(sent: list[dict]) -> int:
    starts = [m["status"] for m in sent if m.get("type") == "http.response.start"]
    assert starts, f"没有发出 http.response.start：{sent!r}"
    return starts[0]


def _header(sent: list[dict], name: str) -> str:
    for message in sent:
        if message.get("type") != "http.response.start":
            continue
        for key, value in message.get("headers", []):
            if key.decode("latin-1").lower() == name.lower():
                return value.decode("latin-1")
    raise AssertionError(f"响应头 {name!r} 不存在：{sent!r}")


def _body(sent: list[dict]) -> bytes:
    return b"".join(
        m.get("body", b"") for m in sent if m.get("type") == "http.response.body"
    )


def _start_headers(sent: list[dict]) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for message in sent:
        if message.get("type") == "http.response.start":
            out.extend((k.decode("latin-1").lower(), v) for k, v in message.get("headers", []))
    return out


def _parse_set_cookie(raw: str) -> tuple[str, set[str]]:
    """把 ``Set-Cookie`` 头拆成 (cookie 值, 规范化属性集合)。"""
    parts = [p.strip() for p in raw.split(";")]
    name, _, value = parts[0].partition("=")
    assert name.strip() == "nh_session", f"cookie 名不是 nh_session：{raw!r}"
    attrs = {p.split("=", 1)[0].strip().lower() for p in parts[1:]}
    return value.strip(), attrs


# --------------------------------------------------------------------------- #
# §3.4 单元测试：session_value / strip_token_param
# --------------------------------------------------------------------------- #
def test_session_value_is_deterministic_fixed_length_and_not_the_token() -> None:
    auth = _auth()

    first = auth.session_value(TOKEN)
    second = auth.session_value(TOKEN)

    assert first == second, "同一 token 必须恒返回同一值（确定性）"
    assert len(first) == 64, f"应为定长 64 的小写十六进制，实际 {first!r}"
    assert re.fullmatch(r"[0-9a-f]{64}", first), f"必须是小写十六进制：{first!r}"
    assert first != TOKEN, "不得是令牌本体的可逆编码"
    assert TOKEN not in first


def test_session_value_differs_for_different_tokens() -> None:
    auth = _auth()

    assert auth.session_value(TOKEN) != auth.session_value(OTHER_TOKEN)


def test_session_value_of_empty_token_raises_value_error() -> None:
    auth = _auth()

    with pytest.raises(ValueError):
        auth.session_value("")


def test_module_constants_are_frozen_by_spec() -> None:
    auth = _auth()

    assert auth.COOKIE_NAME == "nh_session"
    assert auth.SESSION_MESSAGE == V1
    assert isinstance(auth.SESSION_MESSAGE, bytes)
    assert auth.HEALTH_PATH == "/healthz"


def test_strip_token_param_removes_every_token_pair_keeping_order() -> None:
    auth = _auth()

    assert auth.strip_token_param("a=1&token=X&b=2") == "a=1&b=2"
    assert auth.strip_token_param("token=X") == ""
    assert auth.strip_token_param("a=1") == "a=1"
    assert auth.strip_token_param("") == ""
    # 多个 token 键值对全部移除；保留项的原有顺序不变
    assert auth.strip_token_param("a=1&token=X&token=Y&b=2") == "a=1&b=2"
    assert auth.strip_token_param("a=1&b=2") == "a=1&b=2"


# --------------------------------------------------------------------------- #
# §3.4 单元测试：AuthGuard 纯单元行为
# --------------------------------------------------------------------------- #
def test_auth_guard_dataclass_field_order_and_defaults() -> None:
    """字段顺序固定 ``app`` 在前：Starlette 的 ``add_middleware`` 最终是
    ``cls(app, *args, **kwargs)``——**位置传参**，所以被包裹的应用必然落在第一个字段上。

    一切构造一律用关键字（§3.2）：``AuthGuard(token=T)``，禁止 ``AuthGuard(T)``
    （那会把令牌绑到 ``app`` 上，守卫静默失效）。
    """
    import dataclasses

    auth = _auth()

    fields = [f.name for f in dataclasses.fields(auth.AuthGuard)]
    assert fields[:2] == ["app", "token"], f"字段顺序必须是 app 在前：{fields}"
    assert getattr(auth.AuthGuard, "__dataclass_params__").frozen is True

    guard = auth.AuthGuard(token=None)
    assert guard.app is None
    assert guard.token is None


def test_token_is_stripped_at_construction_time() -> None:
    """§3.2 R2（冻结）：``token`` 一律在**构造时**完成一次 ``strip()`` 归一化，与
    ``config._parse_auth_token`` 的语义一致；**不得**把 strip 推迟到每次请求里做。

    期望值：``AuthGuard(token=" T ").token == "T"``（构造后立刻读字段即为归一化值，
    无需任何请求），且 ``AuthGuard(token=" T ")`` 与 ``AuthGuard(token="T")`` 完全等价——
    连 ``expected_cookie()`` 的返回值都相同。
    """
    auth = _auth()

    padded_literal = auth.AuthGuard(token=" T ")
    plain_literal = auth.AuthGuard(token="T")
    assert padded_literal.token == "T", "首尾空白必须在构造时即 strip，字段不得残留 ' T '"
    assert padded_literal.expected_cookie() == plain_literal.expected_cookie(), (
        "归一化后两者必须完全等价（含 expected_cookie()）"
    )

    padded_token = auth.AuthGuard(token=f"  {TOKEN}\t")
    plain_token = auth.AuthGuard(token=TOKEN)
    assert padded_token.token == TOKEN, "构造后立即读到归一化值，说明 strip 不依赖请求期"
    assert padded_token.expected_cookie() == plain_token.expected_cookie()
    assert padded_token.is_authorized(cookie_value=None, query_token=TOKEN) is True
    assert (
        padded_token.is_authorized(cookie_value=plain_token.expected_cookie(), query_token=None)
        is True
    )

    # 纯空白归一化为 None（字段置 None、守卫关闭），与 token=None 等价
    assert auth.AuthGuard(token="   ").token is None
    assert auth.AuthGuard(token=" \t ").enabled is False


def test_guard_fields_are_frozen_and_assignment_raises_frozen_instance_error() -> None:
    """§3.2 R2（冻结）：``AuthGuard`` 一经构造即不可变——对字段赋值抛
    ``dataclasses.FrozenInstanceError``（规格已点名异常类型）。
    """
    import dataclasses

    auth = _auth()

    guard = auth.AuthGuard(token=TOKEN)
    for field_name, value in (("token", OTHER_TOKEN), ("app", _SentinelApp())):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(guard, field_name, value)

    assert guard.token == TOKEN, "失败的赋值不得留下副作用"
    assert guard.app is None


def test_guard_disabled_when_token_is_none_or_blank() -> None:
    auth = _auth()

    assert auth.AuthGuard(token=None).enabled is False
    assert auth.AuthGuard(app=None, token=None).enabled is False
    assert auth.AuthGuard(token="").enabled is False
    assert auth.AuthGuard(token="   ").enabled is False
    assert auth.AuthGuard(token=TOKEN).enabled is True


def test_disabled_guard_authorizes_everything() -> None:
    auth = _auth()

    guard = auth.AuthGuard(token=None)
    assert guard.is_authorized(cookie_value=None, query_token=None) is True
    assert guard.is_authorized(cookie_value="garbage", query_token="garbage") is True


def test_disabled_guard_expected_cookie_raises_runtime_error() -> None:
    auth = _auth()

    with pytest.raises(RuntimeError):
        auth.AuthGuard(token=None).expected_cookie()


def test_enabled_guard_authorization_matrix() -> None:
    auth = _auth()

    guard = auth.AuthGuard(token=TOKEN)
    expected = guard.expected_cookie()
    assert expected == auth.session_value(TOKEN)

    assert guard.is_authorized(cookie_value=None, query_token=TOKEN) is True
    assert guard.is_authorized(cookie_value=expected, query_token=None) is True
    assert guard.is_authorized(cookie_value=expected, query_token=TOKEN) is True

    # 错误 Cookie
    assert guard.is_authorized(cookie_value="deadbeef" * 8, query_token=None) is False
    assert guard.is_authorized(cookie_value=auth.session_value(OTHER_TOKEN), query_token=None) is False
    # 错误令牌
    assert guard.is_authorized(cookie_value=None, query_token=OTHER_TOKEN) is False
    # 两者都错
    assert guard.is_authorized(cookie_value="deadbeef" * 8, query_token=OTHER_TOKEN) is False
    # 都没有
    assert guard.is_authorized(cookie_value=None, query_token=None) is False
    # 空串等同未提供
    assert guard.is_authorized(cookie_value="", query_token="") is False
    # 正确令牌 + 错误 cookie 仍通过（令牌优先，两者等价）
    assert guard.is_authorized(cookie_value="bad", query_token=TOKEN) is True


def test_guard_comparison_is_bytes_safe_for_non_ascii() -> None:
    """比较前双方都 .encode("utf-8")：否则 compare_digest 对非 ASCII str 抛 TypeError。"""
    auth = _auth()

    guard = auth.AuthGuard(token=NON_ASCII_TOKEN)
    assert guard.is_authorized(cookie_value=None, query_token=NON_ASCII_TOKEN) is True
    assert guard.is_authorized(cookie_value=guard.expected_cookie(), query_token=None) is True
    assert guard.is_authorized(cookie_value="令牌-别的", query_token=None) is False


# --------------------------------------------------------------------------- #
# §3.2 判定顺序：逐步单独观测（用哨兵 app 精确断言「是否透了传」）
# --------------------------------------------------------------------------- #
def test_non_http_scope_is_passed_through() -> None:
    auth = _auth()

    inner = _RecordingApp()
    guard = auth.AuthGuard(app=inner, token=TOKEN)

    passed: dict[str, tuple] = {}
    for type_ in ("lifespan", "websocket"):
        scope = _scope(path="/", type_=type_)
        sent: list[dict] = []

        async def receive() -> dict:
            return {"type": f"{type_}.receive"}

        async def send(message: dict) -> None:
            sent.append(message)

        async def drive() -> None:
            await guard(scope, receive, send)

        asyncio.run(drive())
        passed[type_] = (scope, receive, send)

        # 合规的内层应用什么都不发 → 守卫若原样透传，守卫自身也就没有发出任何消息。
        assert sent == [], f"{type_} 不得产生 HTTP 响应（应直接透传）"

    assert len(inner.calls) == 2, "websocket/lifespan 必须透传到内层应用"
    for type_, (got_scope, got_receive, got_send) in zip(("lifespan", "websocket"), inner.calls):
        scope, receive, send = passed[type_]
        # 「原样透传」的最强可观测编码：三件套必须是同一对象，send 不得被包装。
        assert got_scope is scope, f"{type_}: scope 未被原样透传"
        assert got_receive is receive, f"{type_}: receive 未被原样透传"
        assert got_send is send, f"{type_}: send 被包装或未被原样透传"


def test_disabled_guard_passes_through_and_does_not_parse() -> None:
    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=None)

    sent = _run(guard, _scope(query=b"token=whatever"))
    assert _status(sent) == 200 and _body(sent) == b"INNER-APP"
    assert len(inner.calls) == 1


def test_health_path_is_passed_through_even_without_credentials() -> None:
    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=TOKEN)

    sent = _run(guard, _scope(path=auth.HEALTH_PATH))
    assert _status(sent) == 200
    assert len(inner.calls) == 1, "/healthz 是唯一免鉴权路径，必须透传"
    assert not any(k == "set-cookie" for k, _ in _start_headers(sent))


def test_request_without_credentials_gets_401_json_and_never_reaches_inner() -> None:
    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=TOKEN)

    sent = _run(guard, _scope(path="/api/v1/todos", headers=[(b"accept", b"application/json")]))
    assert _status(sent) == 401
    assert _header(sent, "content-type") == "application/json"
    assert _header(sent, "content-length") == str(len(_body(sent))), "必须写 Content-Length"
    assert inner.calls == [], "未认证请求不得到达内层应用"


def test_wrong_token_and_wrong_cookie_are_rejected() -> None:
    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=TOKEN)

    wrong_query = _run(guard, _scope(query=f"token={OTHER_TOKEN}".encode("latin-1")))
    assert _status(wrong_query) == 401

    wrong_cookie = _run(guard, _scope(headers=[(b"cookie", b"nh_session=deadbeef")]))
    assert _status(wrong_cookie) == 401

    assert inner.calls == []


def test_correct_query_token_reaches_inner_without_setting_cookie() -> None:
    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=TOKEN)

    sent = _run(guard, _scope(path="/api/v1/todos", query=f"token={TOKEN}".encode("latin-1")))
    assert _status(sent) == 200
    assert len(inner.calls) == 1
    assert _start_headers(sent) and not any(k == "set-cookie" for k, _ in _start_headers(sent))


def test_correct_cookie_reaches_inner() -> None:
    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=TOKEN)
    cookie = f"{auth.COOKIE_NAME}={guard.expected_cookie()}".encode("latin-1")

    sent = _run(guard, _scope(headers=[(b"cookie", cookie)]))
    assert _status(sent) == 200
    assert len(inner.calls) == 1


def test_bare_query_token_with_html_accept_returns_303_with_session_cookie() -> None:
    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=TOKEN)

    sent = _run(
        guard,
        _scope(
            path="/todos",
            query=f"token={TOKEN}".encode("latin-1"),
            headers=[(b"accept", b"text/html,application/xhtml+xml")],
        ),
    )

    assert _status(sent) == 303
    assert _header(sent, "location") == "/todos", "去掉令牌后无剩余内容，Location 必须是裸路径"
    value, attrs = _parse_set_cookie(_header(sent, "set-cookie"))
    assert value == guard.expected_cookie()
    assert {"httponly", "samesite"} <= attrs, f"缺少必需属性：{attrs}"
    assert "path=/" in [p.strip().lower() for p in _header(sent, "set-cookie").split(";")], (
        f"必须是 Path=/：{_header(sent, 'set-cookie')!r}"
    )
    assert "secure" not in attrs, "纯 HTTP 部署下不得带 Secure（否则浏览器不回传，功能坏掉）"
    assert inner.calls == [], "303 分支不得调用内层应用"


def test_303_keeps_other_query_params_and_drops_token() -> None:
    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=TOKEN)

    sent = _run(
        guard,
        _scope(
            path="/todos",
            query=f"status=pending&token={TOKEN}".encode("latin-1"),
            headers=[(b"accept", b"text/html")],
        ),
    )

    assert _status(sent) == 303
    location = _header(sent, "location")
    assert location.startswith("/todos?")
    assert "status=pending" in location
    assert "token" not in location, f"Location 不得回显令牌：{location}"
    assert inner.calls == []


def test_html_accept_but_non_get_is_not_redirected() -> None:
    """第 6 步：非 GET 即便已认证且 Accept 含 text/html 也只是透传，不种 Cookie。"""
    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=TOKEN)

    sent = _run(
        guard,
        _scope(
            path="/api/v1/messages",
            method="POST",
            query=f"token={TOKEN}".encode("latin-1"),
            headers=[(b"accept", b"text/html")],
        ),
    )

    assert _status(sent) != 303
    assert _status(sent) == 200
    assert len(inner.calls) == 1, "已认证的非 GET 请求必须透传"


def test_api_path_with_html_accept_is_redirected_too() -> None:
    """R1 裁定：第 5 步**与路径无关**——它判定的是「浏览器导航」而非「页面」。

    ``GET /api/v1/todos?token=T`` 带 ``Accept: text/html`` 同样落入第 5 步：
    303 + 种 Cookie + 不调用内层应用。（早期 §3.4 把这条写成「透传」是架构师写错。）
    """
    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=TOKEN)

    sent = _run(
        guard,
        _scope(
            path="/api/v1/todos",
            query=f"token={TOKEN}".encode("latin-1"),
            headers=[(b"accept", b"text/html")],
        ),
    )

    assert _status(sent) == 303, "第 5 步与路径无关，/api/ 路径同样 303"
    assert _header(sent, "location") == "/api/v1/todos"
    value, attrs = _parse_set_cookie(_header(sent, "set-cookie"))
    assert value == guard.expected_cookie()
    assert "secure" not in attrs
    assert inner.calls == [], "303 分支不得调用内层应用"


def test_api_get_with_token_and_json_accept_is_passed_through_without_cookie() -> None:
    """第 6 步＝第 5 步的**严格补集**：已认证 + 查询令牌 + GET，但 ``Accept`` 不含
    ``text/html`` → 不重定向、不种 Cookie，原样透传给内层应用。"""
    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=TOKEN)

    for accept in (b"application/json", b"*/*"):
        sent = _run(
            guard,
            _scope(
                path="/api/v1/todos",
                query=f"token={TOKEN}".encode("latin-1"),
                headers=[(b"accept", accept)],
            ),
        )
        assert _status(sent) == 200, f"Accept={accept!r} 必须透传，实际 {_status(sent)}"
        assert not any(
            k == "set-cookie" for k, _ in _start_headers(sent)
        ), f"第 6 步不得种 Cookie（Accept={accept!r}）"

    assert len(inner.calls) == 2, "两次请求都必须到达内层应用"


def test_401_typing_covers_wildcard_and_missing_accept() -> None:
    """§3.2 第 7 步的分型精确化：``Accept: */*`` 与**完全没有 Accept 头**都归入 JSON 分型。"""
    import json

    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=TOKEN)

    wildcard = _run(guard, _scope(path="/todos", headers=[(b"accept", b"*/*")]))
    assert _status(wildcard) == 401
    assert _header(wildcard, "content-type") == "application/json"
    assert json.loads(_body(wildcard)) == {"detail": "unauthorized"}
    assert _header(wildcard, "content-length") == str(len(_body(wildcard)))

    no_accept = _run(guard, _scope(path="/todos"))
    assert _status(no_accept) == 401
    assert _header(no_accept, "content-type") == "application/json"
    assert json.loads(_body(no_accept)) == {"detail": "unauthorized"}
    assert _header(no_accept, "content-length") == str(len(_body(no_accept)))

    assert inner.calls == []


def test_401_html_variant_has_html_content_type_and_no_token_echo() -> None:
    auth = _auth()

    guard = auth.AuthGuard(app=_SentinelApp(), token=TOKEN)

    sent = _run(
        guard,
        _scope(
            path="/todos",
            query=f"token={OTHER_TOKEN}".encode("latin-1"),
            headers=[(b"accept", b"text/html")],
        ),
    )

    assert _status(sent) == 401
    assert _header(sent, "content-type").startswith("text/html")
    body = _body(sent)
    assert _header(sent, "content-length") == str(len(body))
    assert OTHER_TOKEN.encode("utf-8") not in body, "响应体不得回显请求携带的任何令牌"


def test_401_json_variant_body_and_no_token_echo() -> None:
    auth = _auth()

    guard = auth.AuthGuard(app=_SentinelApp(), token=TOKEN)

    sent = _run(
        guard,
        _scope(
            path="/api/v1/todos",
            query=f"token={OTHER_TOKEN}".encode("latin-1"),
            headers=[(b"accept", b"application/json")],
        ),
    )

    assert _status(sent) == 401
    assert _header(sent, "content-type") == "application/json"
    import json

    assert json.loads(_body(sent)) == {"detail": "unauthorized"}
    assert _header(sent, "content-length") == str(len(_body(sent)))


def test_malformed_cookie_header_yields_401_and_never_5xx() -> None:
    """§3.2 第 4 步的可观测契约：畸形 Cookie 头最终必须 ``401`` 且**不是 5xx**。

    内部究竟走了 ``SimpleCookie`` 的捕获分支还是被下游拒绝是**不可观测的**，
    本用例不假装能区分内部分支——只断言这两点。
    """
    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=TOKEN)

    for cookie in ('nh_session="', "nh_session=", "=;=;=", "nh_session=a; b", "\x00\x01"):
        sent = _run(guard, _scope(headers=[(b"cookie", cookie.encode("latin-1"))]))
        status = _status(sent)
        assert status == 401, f"畸形 Cookie {cookie!r} 必须得到 401，实际 {status}"
        assert status < 500, f"畸形 Cookie {cookie!r} 不得 5xx，实际 {status}"
    assert inner.calls == []


def test_multiple_cookie_headers_are_joined() -> None:
    """§3.2 第 4 步：``cookie`` 头的**全部**值用 ``"; "`` 连接后再交给 SimpleCookie。"""
    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=TOKEN)
    session = guard.expected_cookie()

    headers = [
        (b"cookie", b"other=1"),
        (b"cookie", f"{auth.COOKIE_NAME}={session}".encode("latin-1")),
    ]
    sent = _run(guard, _scope(headers=headers))
    assert _status(sent) == 200, "合并后的 Cookie 头里应能找到凭据"
    assert len(inner.calls) == 1


def test_first_token_param_wins_and_blank_token_counts_as_absent() -> None:
    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=TOKEN)

    ok = _run(
        guard,
        _scope(query=f"token={TOKEN}&token=garbage".encode("latin-1")),
    )
    assert _status(ok) == 200, "取第一个 token 键的值"

    blank = _run(guard, _scope(query=b"token=", headers=[(b"accept", b"application/json")]))
    assert _status(blank) == 401, "空串令牌视为未提供"

    assert len(inner.calls) == 1


def test_non_ascii_token_in_query_string_is_percent_decoded() -> None:
    """§3.3：查询串先按 latin-1 取字节视图，再由 parse_qsl 按 UTF-8 解百分号转义。"""
    auth = _auth()

    inner = _SentinelApp()
    guard = auth.AuthGuard(app=inner, token=NON_ASCII_TOKEN)
    encoded = "token=" + quote(NON_ASCII_TOKEN, safe="")

    sent = _run(guard, _scope(path="/api/v1/todos", query=encoded.encode("latin-1")))
    assert _status(sent) == 200, "非 ASCII 令牌经百分号编码后必须能与令牌原文相等比较"
    assert len(inner.calls) == 1


def test_guard_without_app_raises_runtime_error_instead_of_silently_passing() -> None:
    """``app is None`` 仅供纯单元测试；一旦需要透传必须抛 RuntimeError，不得静默放行。"""
    auth = _auth()

    guard = auth.AuthGuard(token=TOKEN)

    with pytest.raises(RuntimeError):
        _run(guard, _scope(path=auth.HEALTH_PATH))
    with pytest.raises(RuntimeError):
        _run(guard, _scope(query=f"token={TOKEN}".encode("latin-1")))

    # 纯单元用法不受影响
    assert guard.is_authorized(cookie_value=None, query_token=TOKEN) is True


def test_guard_module_does_not_import_frameworks_or_business_routes() -> None:
    """§3.3：禁止 import fastapi/starlette，也不得导入 notify_hub.web / notify_hub.api；
    不得继承 BaseHTTPMiddleware（它会缓冲响应，干扰后台投递语义）。"""
    import ast
    import importlib
    import sys

    auth = _auth()

    tree = ast.parse(Path(auth.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    forbidden = ("fastapi", "starlette", "notify_hub.web", "notify_hub.api")
    for module in imported:
        roots = [module, *module.split(".")]
        assert not any(f in roots for f in forbidden), (
            f"auth 模块不得导入 {module!r}（§3.3 依赖白名单）"
        )

    base_names = []
    for base in auth.AuthGuard.__mro__:
        base_names.append(getattr(base, "__name__", ""))
        mod = getattr(base, "__module__", None)
        if mod and mod not in sys.modules:
            try:
                importlib.import_module(mod)
            except ImportError:  # pragma: no cover
                continue
    assert "BaseHTTPMiddleware" not in base_names, "不得继承 BaseHTTPMiddleware（§3.2）"


def test_auth_module_source_has_no_logging_via_ast() -> None:
    """§3.1/§3.2 R2（冻结）：「不写日志」是**可静态检查**的约束——凭据模块自身不得成为泄漏源。

    对 ``notify_hub.auth.__file__`` 指向的源码做 **AST** 检查（``ast.parse`` + 遍历
    ``ast.Import`` / ``ast.ImportFrom`` / ``ast.Attribute``，**不用**字符串匹配，否则会被
    注释与字符串字面量骗过去）：既没有 ``import logging``，也没有任何 ``logging.*`` 形式的调用。
    """
    import ast

    auth = _auth()
    source_path = Path(auth.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    log_imports = [m for m in imported if m == "logging" or m.startswith("logging.")]
    assert log_imports == [], f"auth.py 不得 import logging（§3.1）：{log_imports}"

    log_attrs = [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "logging"
    ]
    assert log_attrs == [], f"auth.py 不得出现任何 logging.* 调用：{log_attrs}"


# --------------------------------------------------------------------------- #
# §3.4 单元测试（阶段 0 共享文件组）：config.py / main.py
# 这一组若失败，是架构师实现的问题——不得改期望值去迁就代码。
# --------------------------------------------------------------------------- #
_MINIMAL_CONFIG = """\
server:
  host: 127.0.0.1
  port: 8000
  log_level: INFO
{extra}\
storage:
  db_path: ./data/notify.db
rules:
  path: ./rules.yaml
  poll_interval_seconds: 5
reminders:
  at: "21:00"
  timezone: "Asia/Shanghai"
  scan_interval_seconds: 1
default_channel: null
channels: []
"""


def _load_config(tmp_path: Path, extra_server_lines: str = ""):
    from notify_hub.config import load_settings

    path = tmp_path / "config.yaml"
    path.write_text(
        _MINIMAL_CONFIG.format(
            extra=extra_server_lines and f"  {extra_server_lines}\n"
        ),
        encoding="utf-8",
    )
    return load_settings(path, env={})


def test_auth_token_key_missing_yields_none(tmp_path: Path) -> None:
    settings = _load_config(tmp_path)
    assert settings.auth_token is None


def test_auth_token_explicit_null_yields_none(tmp_path: Path) -> None:
    settings = _load_config(tmp_path, "auth_token: null")
    assert settings.auth_token is None


def test_auth_token_non_string_raises_configuration_error(tmp_path: Path) -> None:
    from notify_hub.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        _load_config(tmp_path, "auth_token: 12345")


@pytest.mark.parametrize("value", ['""', '"   "', '"\\t"'])
def test_auth_token_blank_string_raises_configuration_error(tmp_path: Path, value: str) -> None:
    """空串与纯空白（含仅制表符）都必须抛 ConfigurationError。

    YAML 双引号标量里用 ``"\\t"`` 转义写制表符——直接写字面制表符会被 YAML 解析器
    拒绝（非法字符），那是解析错误而不是我们要断言的配置错误。
    """
    import yaml

    from notify_hub.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        _load_config(tmp_path, f"auth_token: {value}")
    # 确认该 YAML 片段本身可解析且确是「去空白后为空」的字符串：
    # 失败必须来自配置校验，不是 YAML 语法错误。
    parsed = yaml.safe_load(f"auth_token: {value}\n")["auth_token"]
    assert isinstance(parsed, str) and parsed.strip() == ""


def test_auth_token_is_stripped(tmp_path: Path) -> None:
    settings = _load_config(tmp_path, 'auth_token: "  T  "')
    assert settings.auth_token == "T"


def test_credential_values_include_non_empty_auth_token(tmp_path: Path) -> None:
    from notify_hub.config import credential_values

    settings = _load_config(tmp_path, 'auth_token: "  T  "')
    assert "T" in credential_values(settings)


def test_credential_values_omit_absent_auth_token(tmp_path: Path) -> None:
    from notify_hub.config import credential_values

    settings = _load_config(tmp_path)
    assert credential_values(settings) == ()
    assert "None" not in credential_values(settings)


def test_build_uvicorn_kwargs_disables_log_config(tmp_path: Path) -> None:
    from notify_hub.main import build_uvicorn_kwargs

    settings = _load_config(tmp_path)
    kwargs = build_uvicorn_kwargs(settings)

    assert kwargs["log_config"] is None, "D8：必须置空，否则访问行会明文泄漏查询串里的令牌"
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == settings.port
    assert settings.host == "127.0.0.1", "缺省绑定必须是回环地址"


# --------------------------------------------------------------------------- #
# §3.4 模块级测试：把 AuthGuard 装在真实路由前面（装配配方照抄规格，不用 create_app）
# --------------------------------------------------------------------------- #
def guarded_app(ctx, token):
    from fastapi import FastAPI

    from notify_hub.api import create_api_router
    from notify_hub.web import create_web_router

    app = FastAPI()
    app.state.ctx = ctx  # web 路由经 request.app.state.ctx 取上下文
    app.include_router(create_api_router(ctx))
    app.include_router(create_web_router(ctx))
    app.add_middleware(_auth().AuthGuard, token=token)
    return app


def _client(ctx, token=TOKEN):
    from fastapi.testclient import TestClient

    return TestClient(guarded_app(ctx, token))


def test_real_routes_without_credentials_are_401_html_for_browser(ctx) -> None:
    with _client(ctx) as client:
        response = client.get("/todos", headers={"Accept": "text/html"})

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("text/html")


def test_real_api_route_without_credentials_is_401_json(ctx) -> None:
    with _client(ctx) as client:
        response = client.get("/api/v1/todos")

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


def test_unauthenticated_message_ingest_is_blocked(ctx) -> None:
    """本变更的关键安全断言：无鉴权的投递入口必须被挡住。"""
    with _client(ctx) as client:
        response = client.post(
            "/api/v1/messages",
            json={"text": "不应该被受理"},
            headers={"Accept": "application/json"},
        )

    assert response.status_code == 401, "无凭据的投递入口未被挡住——安全回归"
    assert response.json() == {"detail": "unauthorized"}


def test_healthz_is_reachable_without_credentials(ctx) -> None:
    with _client(ctx) as client:
        response = client.get("/healthz", headers={"Accept": "text/html"})

    assert response.status_code == 200
    assert "status" in response.text


def test_browser_first_hit_gets_303_and_session_cookie(ctx) -> None:
    with _client(ctx) as client:
        response = client.get(
            f"/todos?token={TOKEN}",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/todos"
        raw = response.headers["set-cookie"]
        value, attrs = _parse_set_cookie(raw)
        assert value == _auth().session_value(TOKEN)
        assert {"httponly", "path", "samesite"} <= attrs
        assert "secure" not in attrs
        assert TOKEN not in raw


def test_303_preserves_other_query_params(ctx) -> None:
    with _client(ctx) as client:
        response = client.get(
            f"/todos?status=pending&token={TOKEN}",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith("/todos?")
        assert "status=pending" in location
        assert "token" not in location


def test_cookie_only_visit_is_200_and_does_not_set_cookie(ctx) -> None:
    """仅凭 Cookie（查询串里**没有**令牌）再访问：200，且响应不再出现 Set-Cookie。"""
    auth = _auth()
    session = auth.session_value(TOKEN)

    with _client(ctx) as client:
        response = client.get(
            "/todos",
            headers={"Accept": "text/html", "Cookie": f"{auth.COOKIE_NAME}={session}"},
            follow_redirects=False,
        )

    assert response.status_code == 200, "仅凭 Cookie 应通过认证"
    assert "set-cookie" not in {k.lower() for k in response.headers}, "已持凭据的请求不得再种 Cookie"


def test_non_get_with_token_is_not_redirected_through_real_routes(ctx) -> None:
    with _client(ctx) as client:
        response = client.post(
            f"/api/v1/messages?token={TOKEN}",
            json={"text": "认证通过后应被受理"},
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )

    assert response.status_code != 303, "非 GET 不得被重定向（第 6 步透传）"


def test_api_get_with_token_and_json_accept_is_200_json_without_cookie(ctx) -> None:
    """第 6 步（第 5 步的严格补集）：``GET /api/v1/todos?token=T`` 带
    ``Accept: application/json`` / ``*/*`` → 不重定向、透传得到 ``200`` 与真实 JSON，
    且响应**不含** ``Set-Cookie``。"""
    for accept in ("application/json", "*/*"):
        with _client(ctx) as client:
            response = client.get(
                f"/api/v1/todos?token={TOKEN}",
                headers={"Accept": accept},
                follow_redirects=False,
            )

        assert response.status_code == 200, (
            f"Accept={accept!r} 不得被重定向，实际 {response.status_code}"
        )
        assert response.headers["content-type"].startswith("application/json")
        payload = response.json()
        assert set(payload) == {"todos", "total"}, f"应为真实 JSON 响应：{payload!r}"
        assert "set-cookie" not in {k.lower() for k in response.headers}, "第 6 步不得种 Cookie"
        assert TOKEN not in response.text


def test_malformed_cookie_header_through_real_routes_is_not_5xx(ctx) -> None:
    """§3.2 第 4 步可观测契约：畸形 Cookie 头最终 ``401`` 且**不是 5xx**（不区分内部分支）。"""
    with _client(ctx) as client:
        for cookie in ('nh_session="', "nh_session=", "=;=="):
            response = client.get("/todos", headers={"Cookie": cookie})
            status = response.status_code
            assert status == 401, f"畸形 Cookie {cookie!r} 得到 {status}"
            assert status < 500, f"畸形 Cookie {cookie!r} 不得 5xx，实际 {status}"


def test_guard_disabled_in_real_routes_lets_requests_through(ctx) -> None:
    """守卫关闭（``token=None``）＝透传：真实路由无需凭据即可访问。"""
    with _client(ctx, token=None) as client:
        response = client.get("/api/v1/todos")

    assert response.status_code == 200, "未配置令牌时必须透传（D4 契约）"


def test_wrong_token_response_does_not_echo_it_anywhere(ctx) -> None:
    with _client(ctx) as client:
        for path, accept in (("/todos", "text/html"), ("/api/v1/todos", "application/json")):
            response = client.get(
                f"{path}?token={OTHER_TOKEN}",
                headers={"Accept": accept},
                follow_redirects=False,
            )
            assert response.status_code == 401
            assert OTHER_TOKEN not in response.text
            assert all(
                OTHER_TOKEN not in response.headers.get(name, "") for name in _SECRET_HEADERS
            )


# --------------------------------------------------------------------------- #
# §7.1 末尾（P2 #3）：delta 规格「令牌轮换立即生效」
# --------------------------------------------------------------------------- #
def test_token_rotation_invalidates_old_cookie_immediately(ctx) -> None:
    """令牌由 A 改为 B 并**重建应用**后：基于 A 的旧 Cookie 得 ``401``，基于 B 的新 Cookie 得 ``200``。

    对应 ``specs/access-control/spec.md`` 的「令牌轮换立即生效 / 轮换后旧 Cookie 失效」。
    「重启」在模块级测试里的可观测等价物是**用新令牌重新装配一个应用实例**——这正是
    ``create_app`` 在重启时唯一会变的那一步（``add_middleware(AuthGuard, token=...)``）。
    """
    auth = _auth()
    token_a, token_b = TOKEN, OTHER_TOKEN

    old_cookie = {auth.COOKIE_NAME: auth.session_value(token_a)}
    new_cookie = {auth.COOKIE_NAME: auth.session_value(token_b)}
    assert old_cookie[auth.COOKIE_NAME] != new_cookie[auth.COOKIE_NAME]

    # 轮换前：基于 A 的 Cookie 正常通过（正控——否则下面的 401 可能只是 Cookie 写错了）。
    with _client(ctx, token=token_a) as client:
        before = client.get(
            "/todos",
            headers={"Accept": "text/html"},
            cookies=old_cookie,
            follow_redirects=False,
        )
    assert before.status_code == 200, "轮换前基于 A 的 Cookie 必须可用（正控）"

    # 轮换：令牌改为 B，重建应用。旧 Cookie 立即失效，新 Cookie 立即可用。
    with _client(ctx, token=token_b) as client:
        stale = client.get(
            "/todos",
            headers={"Accept": "text/html"},
            cookies=old_cookie,
            follow_redirects=False,
        )
        fresh = client.get(
            "/todos",
            headers={"Accept": "text/html"},
            cookies=new_cookie,
            follow_redirects=False,
        )

    assert stale.status_code == 401, "轮换后基于 A 的旧 Cookie 必须立即失效"
    assert fresh.status_code == 200, "轮换后基于 B 的新 Cookie 必须立即可用"
