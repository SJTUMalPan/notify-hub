"""作用域 2 集成测试：``add-public-access`` 的四组跨模块接缝（architecture.md §4）。

本文件**不在模块内部边界上打桩**：不替换 ``AuthGuard``、不伪造 ``create_app``、不绕开真实路由、
不替换 ``Database`` / 真实 SQLite / 真实 ``uvicorn``。唯一被替身的是**进程外的对端**
（本机 ``http.server`` 桩渠道），以及可控时钟（``ManualClock``，用于消除真实墙钟依赖）。

覆盖的接缝（逐条对应 §4）
-------------------------
1. **A → 真实 uvicorn → 进程日志**（D8 的唯一真凭据）：以 ``python -m notify_hub`` 在真实子进程里
   起真服务（端口 ``0``，从 uvicorn 的启动日志里取回真实端口），带 ``?token=<T>`` 发一次请求，
   在**完整输出（stdout+stderr 合并）**上断言 ``<T>`` 出现 0 次且存在 ``token=***`` 形态。
   同组附一条**负对照**：换成 uvicorn 默认 ``log_config`` 时明文必然出现——证明上一条断言
   真的会失败，而不是空转。
2. **认证 → 真实 DB → 真实路由**：真实 ``create_app``（守卫装载处）+ Cookie 认证下 ``/todos``
   的内容与 ``ctx.todos`` 的真实数据逐条一致；``POST /todos/{id}/done`` 经 Cookie 认证后
   用**新开的数据库连接**复核状态真的变成 ``done``。
3. **凭据 → 投递记录**：带 ``?token=`` 的请求产生真实投递记录与待办事件；桩把 URL 内嵌凭据与
   访问令牌**回显**在错误正文里（飞书式），随后断言这些记录（ORM 视图 + 真实 SQLite 文件字节）
   里都不出现凭据明文。
4. **守卫关闭路径**（D4）：未配置 / 显式 ``null`` 的 ``auth_token`` 在真实 ``create_app`` 下
   仍可无凭据访问，包括无鉴权的投递入口。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlmodel import select

from notify_hub.app import create_app
from notify_hub.auth import COOKIE_NAME
from notify_hub.clock import as_utc
from notify_hub.config import load_settings
from notify_hub.context import build_context
from notify_hub.db import Database
from notify_hub.domain import TodoStatus
from notify_hub.main import build_uvicorn_kwargs
from notify_hub.models import DeliveryRecord, DigestRun, Message, Todo, TodoEvent
from notify_hub.redact import MASK

#: 访问令牌：只含 URL 安全字符，便于同时用于查询串与凭据比对。
TOKEN = "IntSeamAuthToken7f3c9a1b4e6d8f0a2c5b"

#: 渠道 URL 路径里内嵌的凭据（≥16 字符、仅 [A-Za-z0-9_-]，才会被 extract_url_secrets 收为密钥）。
URL_SECRET = "IntSeamHookSecret0123456789abcd"

#: 子进程组的配置：**不声明任何渠道**、``default_channel: null``，扫描周期取上限，
#: 保证后台调度线程不会制造干扰性投递（该组无法注入时钟）。
_REMINDERS = {"at": "21:00", "timezone": "Asia/Shanghai", "scan_interval_seconds": 3600}

_MINIMAL_RULES = {
    "case_sensitive": False,
    "defaults": {
        "category": "uncategorized",
        "labels": [],
        "need_ack": False,
        "channel": None,
    },
    "rules": [],
}

#: ``_write_config`` 的哨兵：表示「配置里根本没有 auth_token 这个键」。
_ABSENT = object()


# --------------------------------------------------------------------------- #
# 配置 / 进程 / 桩 辅助
# --------------------------------------------------------------------------- #
def _write_config(
    tmp_path: Path,
    *,
    auth_token=_ABSENT,
    port: int = 8000,
    channels: list[dict] | None = None,
    default_channel: str | None = None,
    env: dict[str, str] | None = None,
):
    """写一份真实 YAML + 规则文件，用真实 ``load_settings`` 读回 ``Settings``。"""
    (tmp_path / "rules.yaml").write_text(
        yaml.safe_dump(_MINIMAL_RULES, allow_unicode=True), encoding="utf-8"
    )
    server: dict = {"host": "127.0.0.1", "port": port, "log_level": "INFO"}
    if auth_token is not _ABSENT:
        server["auth_token"] = auth_token
    config = {
        "server": server,
        "storage": {"db_path": "./data/notify.db"},
        "rules": {"path": "./rules.yaml", "poll_interval_seconds": 5},
        "reminders": dict(_REMINDERS),
        "default_channel": default_channel,
        "channels": list(channels or []),
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return config_path, load_settings(config_path, env=env or {})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随 3xx：只有这样才能观察到守卫第 5 步的 303。"""

    def redirect_request(self, *args, **kwargs):  # noqa: D102 - urllib API
        return None


def _open_no_redirect(url: str, *, accept: str, timeout: float = 15.0):
    """发一次请求并原样返回 ``(status, headers)``，不跟随重定向。"""
    request = urllib.request.Request(url, headers={"Accept": accept})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        response = opener.open(request, timeout=timeout)
        try:
            return response.status, response.headers
        finally:
            response.close()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.headers
        finally:
            exc.close()


def _spawn_hub(tmp_path: Path, config_path: Path, log_path: Path):
    """在真实子进程里用 ``python -m notify_hub`` 起服务，输出（stdout+stderr）合并落盘。"""
    log_file = open(log_path, "wb")
    env = dict(os.environ, NOTIFY_HUB_CONFIG=str(config_path))
    proc = subprocess.Popen(
        [sys.executable, "-m", "notify_hub"],
        cwd=str(tmp_path),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    return proc, log_file


def _terminate(proc: subprocess.Popen, log_file) -> None:
    """无论成败都收干净子进程，不留孤儿。"""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    log_file.close()


def _wait_for_port(proc: subprocess.Popen, log_path: Path, timeout: float = 30.0) -> int:
    """轮询日志，从 uvicorn 的启动行里取回 ``port=0`` 实际绑定的端口。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            match = re.search(
                rb"Uvicorn running on http://127\.0\.0\.1:(\d+)", log_path.read_bytes()
            )
            if match:
                return int(match.group(1))
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    excerpt = log_path.read_bytes().decode("utf-8", "replace")[-2000:] if log_path.exists() else ""
    raise AssertionError(
        f"子进程未在 {timeout}s 内就绪（returncode={proc.poll()}）：\n{excerpt}"
    )


class _EchoingFailureStub:
    """进程外对端：真实 socket 上的 webhook 接收方，按 500 + 回显正文应答。

    正文**故意回显**它收到的 URL 与两个凭据字面量，模拟「平台错误信息回显凭据」
    （设计里已发生的 P1 泄漏形态）。这样「记录里不含明文」的断言就不是空转：
    若脱敏链断掉，凭据会真的落进 ``error_reason``。
    """

    def __init__(self, echo: list[str]) -> None:
        self._lock = threading.Lock()
        self.paths: list[str] = []
        stub = self
        echoed = ", ".join(echo)

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                with stub._lock:
                    stub.paths.append(self.path)
                body = json.dumps(
                    {"code": 1, "msg": f"hook {self.path} rejected; leaked {echoed}"}
                ).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # 静音
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="pubaccess-stub", daemon=True
        )
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/hook/{URL_SECRET}"

    def count(self) -> int:
        with self._lock:
            return len(self.paths)

    def wait_for(self, expected: int, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.count() >= expected:
                return True
            time.sleep(0.02)
        return self.count() >= expected

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)


def _webhook_channel() -> list[dict]:
    return [
        {
            "id": "hook",
            "type": "webhook",
            "enabled": True,
            "params": {},
            "credentials": {"url": "STUB_URL"},
        }
    ]


def _durable_dump(ctx) -> str:
    """把全部持久化实体渲成一段文本（覆盖 ORM/JSON 列，逐条真实读库）。"""
    chunks: list[str] = []
    with ctx.db.session() as session:
        for model in (Message, Todo, DeliveryRecord, TodoEvent, DigestRun):
            rows = list(session.exec(select(model)).all())
            chunks.append(f"{model.__name__}={len(rows)}")
            for row in rows:
                chunks.append(json.dumps(row.model_dump(), default=str, ensure_ascii=False))
    return "\n".join(chunks)


def _db_file_bytes(db_path: Path) -> bytes:
    """真实 SQLite 文件字节（含 WAL/SHM 旁路文件）——比 ORM 视图更强的「记录」取证。"""
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.is_file():
            blob += candidate.read_bytes()
    return blob


def _cookie_from(response) -> str:
    """从真实 303 的 ``Set-Cookie`` 里取出会话 Cookie 值（不经过客户端 cookie jar）。"""
    raw = response.headers.get("set-cookie")
    assert raw, f"响应没有 Set-Cookie：{dict(response.headers)}"
    head = raw.split(";", 1)[0]
    name, _, value = head.partition("=")
    assert name.strip() == COOKIE_NAME, f"Cookie 名不是 {COOKIE_NAME}: {raw}"
    return value


# --------------------------------------------------------------------------- #
# 接缝 1：A → 真实 uvicorn → 进程日志（D8 的唯一真凭据）
# --------------------------------------------------------------------------- #
def test_real_subprocess_access_log_never_contains_token_plaintext(tmp_path):
    config_path, settings = _write_config(tmp_path, auth_token=TOKEN, port=0)

    # 子进程跑的正是冻结的 ``build_uvicorn_kwargs``（main() 的字面路径）。
    kwargs = build_uvicorn_kwargs(settings)
    assert kwargs["log_config"] is None
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 0

    log_path = tmp_path / "hub.log"
    proc, log_file = _spawn_hub(tmp_path, config_path, log_path)
    try:
        port = _wait_for_port(proc, log_path)
        status, headers = _open_no_redirect(
            f"http://127.0.0.1:{port}/todos?token={TOKEN}", accept="text/html"
        )
        assert status == 303, f"带令牌的浏览器形态请求未被守卫接管: {status}"
        assert headers["location"] == "/todos"
        cookie = headers["set-cookie"]
        assert COOKIE_NAME in cookie and "HttpOnly" in cookie
        assert "Path=/" in cookie and "SameSite=Lax" in cookie
        assert "Secure" not in cookie
        # 等访问日志行落盘（StreamHandler 逐条 flush）。
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if b"GET /todos?token=" in log_path.read_bytes():
                break
            time.sleep(0.05)
        # 同一进程上的无凭据请求（真实 socket 上的守卫 401）——证明这几行日志来自真装配。
        assert _open_no_redirect(f"http://127.0.0.1:{port}/todos", accept="text/html")[0] == 401
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if b'GET /todos HTTP/1.1" 401' in log_path.read_bytes():
                break
            time.sleep(0.05)
    finally:
        _terminate(proc, log_file)

    raw = log_path.read_bytes()  # 完整输出：stdout 与 stderr 已合并到同一 fd

    # 非空转前提：这一次请求的访问日志行确实被采集到了，且它写的就是带令牌的 URL。
    assert b'GET /todos?token=*** HTTP/1.1" 303' in raw, raw.decode("utf-8", "replace")[-3000:]
    # 同进程的第二次请求（无凭据）留下 401 的访问行——日志确实来自真装配的真 socket。
    assert b'GET /todos HTTP/1.1" 401' in raw, raw.decode("utf-8", "replace")[-3000:]
    # D8 的真凭据：令牌明文出现 0 次，且留下脱敏痕迹。
    assert raw.count(TOKEN.encode()) == 0, raw.decode("utf-8", "replace")[-3000:]
    assert b"token=***" in raw


def test_uvicorn_default_log_config_would_leak_token_negative_control(tmp_path):
    """负对照：默认 ``log_config`` 下同一请求的令牌**必然**明文落日志。

    这不是生产入口，也**不得**被「修复」——它的唯一作用是证明上面那条断言真的会失败
    （否则「不含明文」只是一条不会红的空断言）。
    """
    config_path, _settings = _write_config(tmp_path, auth_token=TOKEN, port=0)
    launcher = tmp_path / "launcher_default_logging.py"
    launcher.write_text(
        "import sys, uvicorn\n"
        "from notify_hub.app import create_app\n"
        "from notify_hub.config import load_settings\n"
        "settings = load_settings(sys.argv[1])\n"
        "uvicorn.run(\n"
        "    create_app(settings),\n"
        "    host=settings.host,\n"
        "    port=settings.port,\n"
        "    log_config=uvicorn.config.LOGGING_CONFIG,\n"
        ")\n",
        encoding="utf-8",
    )

    log_path = tmp_path / "hub_default_logging.log"
    log_file = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, str(launcher), str(config_path)],
        cwd=str(tmp_path),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        port = _wait_for_port(proc, log_path)
        status, _headers = _open_no_redirect(
            f"http://127.0.0.1:{port}/todos?token={TOKEN}", accept="text/html"
        )
        assert status == 303
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if TOKEN.encode() in log_path.read_bytes():
                break
            time.sleep(0.05)
    finally:
        _terminate(proc, log_file)

    raw = log_path.read_bytes()
    assert raw.count(TOKEN.encode()) >= 1, raw.decode("utf-8", "replace")[-3000:]
    assert b"token=***" not in raw


# --------------------------------------------------------------------------- #
# 接缝 2：认证 → 真实 DB → 真实路由
# --------------------------------------------------------------------------- #
def test_cookie_auth_reaches_real_routes_and_really_writes_the_db(tmp_path, manual_clock):
    _config_path, settings = _write_config(tmp_path, auth_token=TOKEN)
    ctx = build_context(settings, clock=manual_clock)
    app = create_app(ctx=ctx)  # 生产装配：守卫就是在这里装上的

    with TestClient(app, follow_redirects=False) as client:
        # 守卫确实挂在整棵路由树上：web、api、以及**无鉴权的投递入口**都被挡住。
        assert client.get("/todos").status_code == 401
        html_401 = client.get("/todos", headers={"Accept": "text/html"})
        assert html_401.status_code == 401
        assert html_401.headers["content-type"].startswith("text/html")
        blocked = client.post(
            "/api/v1/messages", json={"source": "x", "title": "无凭据投递"}
        )
        assert blocked.status_code == 401
        assert blocked.json() == {"detail": "unauthorized"}
        # /healthz 是唯一例外，且它连到真实路由。
        health = client.get("/healthz")
        assert health.status_code == 200 and health.json()["status"] == "ok"

        # 浏览器形态首次请求：303 + 会话 Cookie（不跟随重定向才能观察到）。
        handshake = client.get(f"/todos?token={TOKEN}", headers={"Accept": "text/html"})
        assert handshake.status_code == 303
        assert handshake.headers["location"] == "/todos"
        # 首次请求必须种下会话 Cookie，且属性按冻结契约来。
        assert "HttpOnly" in handshake.headers["set-cookie"]
        assert "Secure" not in handshake.headers["set-cookie"]
        cookie = _cookie_from(handshake)
        auth = {"cookies": {COOKIE_NAME: cookie}}

        # 仅凭 Cookie：真实 web 路由可用，且不再重复种 Cookie。
        page = client.get("/todos", **auth)
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "set-cookie" not in page.headers

        # 仅凭 Cookie 走真实 API 投递入口 → 真实管道 → 真实 SQLite。
        created: list[int] = []
        for index, title in enumerate(("Cookie 认证待办甲", "Cookie 认证待办乙")):
            accepted = client.post(
                "/api/v1/messages",
                json={
                    "source": "seam2",
                    "title": title,
                    "level": "error",
                    "need_ack": True,
                    "dedup_key": f"seam2-{index}",
                },
                **auth,
            )
            assert accepted.status_code == 202, accepted.text
            created.append(accepted.json()["todo_id"])
            assert accepted.json()["todo_id"] is not None

        # 列表页内容与 ctx.todos 的真实数据逐条一致（不是「包含某个字符串」）。
        views = ctx.todos.list(status=TodoStatus.PENDING)
        assert sorted(view.id for view in views) == sorted(created)
        rendered = client.get("/todos", **auth)
        assert rendered.status_code == 200
        rendered_ids = [int(m) for m in re.findall(r'href="/todos/(\d+)"', rendered.text)]
        assert sorted(rendered_ids) == sorted(created)
        for view in views:
            assert view.title in rendered.text
        assert len(re.findall(r'action="/todos/\d+/done"', rendered.text)) == len(created)

        # 经 Cookie 认证提交真实「完成」表单 → 服务 → 真实 DB。
        target = created[0]
        done = client.post(f"/todos/{target}/done", data={}, **auth)
        assert done.status_code == 303, done.text
        assert done.headers["location"].endswith("/todos")

        # 用**新开的连接**复核落库结果（不是读服务的内存状态）。
        check = Database(settings.db_path)
        check.init_schema()
        try:
            with check.session() as session:
                row = session.get(Todo, target)
                assert row is not None
                assert row.status == TodoStatus.DONE.value
                assert row.completed_at is not None
                assert as_utc(row.completed_at) == manual_clock.now()
                events = list(
                    session.exec(select(TodoEvent).where(TodoEvent.todo_id == target)).all()
                )
                assert any(event.kind == "completed" for event in events)
        finally:
            check.dispose()

        assert ctx.todos.get(target).status == TodoStatus.DONE.value
        after = client.get("/todos", **auth)
        assert after.status_code == 200
        main_region = after.text.split("最近完成")[0]
        assert "Cookie 认证待办甲" not in main_region
        assert f"/todos/{target}" not in main_region
        api_done = client.get("/api/v1/todos?status=done", **auth)
        assert api_done.status_code == 200
        assert [item["id"] for item in api_done.json()["todos"]] == [target]
        assert api_done.json()["todos"][0]["status"] == "done"


# --------------------------------------------------------------------------- #
# 接缝 3：凭据 → 投递记录 / 待办事件
# --------------------------------------------------------------------------- #
def test_token_bearing_request_leaves_no_credential_in_records(tmp_path, manual_clock):
    stub = _EchoingFailureStub([URL_SECRET, TOKEN])
    try:
        config_path, settings = _write_config(
            tmp_path,
            auth_token=TOKEN,
            channels=_webhook_channel(),
            default_channel="hook",
            env={"STUB_URL": stub.url},
        )
        assert URL_SECRET in str(settings.channels[0].credentials["url"])
        ctx = build_context(settings, clock=manual_clock)
        app = create_app(ctx=ctx)

        with TestClient(app, follow_redirects=False) as client:
            accepted = client.post(
                f"/api/v1/messages?token={TOKEN}",
                json={
                    "source": "seam3",
                    "title": "带令牌的投递入口请求",
                    "level": "error",
                    "need_ack": True,
                    "dedup_key": "seam3-1",
                },
            )
            assert accepted.status_code == 202, accepted.text
            # POST + 查询令牌：第 6 步透传，不得被重定向、不得种 Cookie。
            assert "set-cookie" not in accepted.headers
            assert TOKEN not in accepted.text

            message_id = accepted.json()["message_id"]
            todo_id = accepted.json()["todo_id"]
            assert ctx.pipeline.drain(timeout=10.0) is True
            assert stub.wait_for(1, timeout=10.0) is True
            # 桩确实收到了「带 URL 内嵌凭据」的渠道请求（回显来源真实）。
            assert any(URL_SECRET in path for path in stub.paths)

            deliveries = ctx.messages.deliveries(message_id)
            assert len(deliveries) >= 1
            assert all(record.ok is False for record in deliveries)
            reasons = [record.error_reason or "" for record in deliveries]
            assert all(reason for reason in reasons)
            for reason in reasons:
                # 记录里确实装着桩回显的正文（脱敏后的），否则下面的断言会空转。
                assert "HTTP 500" in reason and "hook" in reason, reason
                assert TOKEN not in reason, reason
                assert URL_SECRET not in reason, reason
                # 脱敏真的发生了，而不是靠 200 字符截断把凭据挤出去。
                assert MASK in reason, reason

            # 该请求真的产生了待办事件（凭据不会因此进入 todo_events.detail）。
            with ctx.db.session() as session:
                event_rows = list(
                    session.exec(select(TodoEvent).where(TodoEvent.todo_id == todo_id)).all()
                )
            assert event_rows, "带令牌的请求没有产生任何待办事件，接缝 3 会退化为空转"

            # 全部持久化记录（ORM 视图）里都不出现凭据明文。
            dump = _durable_dump(ctx)
            assert "DeliveryRecord=0" not in dump
            assert TOKEN not in dump
            assert URL_SECRET not in dump
            # 真实 SQLite 文件字节级取证（含 JSON 列/WAL）。
            raw_db = _db_file_bytes(settings.db_path)
            assert raw_db
            assert TOKEN.encode() not in raw_db
            assert URL_SECRET.encode() not in raw_db

            detail = client.get(f"/api/v1/messages/{message_id}?token={TOKEN}")
            assert detail.status_code == 200
            assert TOKEN not in detail.text
            assert URL_SECRET not in detail.text
            assert ctx.todos.detail(todo_id) is not None
            events = client.get(f"/api/v1/todos?status=all&token={TOKEN}")
            assert events.status_code == 200 and TOKEN not in events.text
    finally:
        stub.close()


# --------------------------------------------------------------------------- #
# 接缝 4：守卫关闭路径（D4 fail-open 默认）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("auth_token", "label"),
    [(_ABSENT, "键缺失"), (None, "显式 null")],
)
def test_real_app_without_auth_token_is_reachable_without_credentials(
    tmp_path, auth_token, label
):
    _config_path, settings = _write_config(tmp_path, auth_token=auth_token)
    assert settings.auth_token is None, label
    app = create_app(settings)  # 完整生产装配，与令牌配置无关

    with TestClient(app, follow_redirects=False) as client:
        page = client.get("/todos", headers={"Accept": "text/html"})
        assert page.status_code == 200, f"[{label}] {page.status_code}"
        assert not page.headers.get("set-cookie")
        api = client.get("/api/v1/todos")
        assert api.status_code == 200
        assert api.json() == {"todos": [], "total": 0}
        accepted = client.post(
            "/api/v1/messages",
            json={"source": "seam4", "title": f"无凭据投递-{label}", "need_ack": True,
                  "dedup_key": f"seam4-{label}"},
        )
        assert accepted.status_code == 202, accepted.text
        health = client.get("/healthz")
        assert health.status_code == 200
        assert client.get("/").status_code == 303  # 真实 web 路由在场
