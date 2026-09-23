"""阶段 D 集成测试：``CLI → 真实 uvicorn → app``（architecture.md 第 7 节接缝表）。

作用域 2。本文件补上一轮明确披露的缺口：当时 M5 尚未实现，CLI 接缝未覆盖。

真实链路上的**每一跳都是真货**，没有任何模块内部替身：

    subprocess ``.venv/bin/python -m notify_hub.cli``（真实进程 + 真实 socket）
      → uvicorn.Server（临时端口，后台线程，真实 ASGI 服务器）
      → create_app 的真实 lifespan（classifier/pipeline/scheduler 三个后台线程）
      → 分类器 → SQLite → 待办 → IngestPipeline 的真实工作线程
      → 真实 WebhookNotifier（真实 socket）→ 本机 http.server 桩渠道

唯一允许的进程外替身是本机 ``http.server.ThreadingHTTPServer``（桩渠道）。

退出码断言直接对齐冻结表：0 成功 / 1 服务端拒绝 / 3 服务不可达。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml

from notify_hub.app import create_app
from notify_hub.clock import as_utc
from notify_hub.config import load_settings
from notify_hub.context import build_context

REPO_ROOT = Path(__file__).resolve().parents[1]

_SLOW_REMINDERS = {
    "scan_interval_seconds": 3600,
    "at": "21:00",
    "timezone": "Asia/Shanghai",
}


def _from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- #
# 被测 CLI 的真实进程入口
# --------------------------------------------------------------------------- #
def _cli_python() -> str:
    """优先用仓库 ``.venv`` 的解释器（规格命令即 ``.venv/bin/python -m notify_hub.cli``）。"""
    candidate = REPO_ROOT / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


def _run_cli(*args: str, env_extra: dict[str, str] | None = None):
    """以**子进程**方式跑真实 CLI；返回值带 exit_code/stdout/stderr。"""
    env = dict(os.environ)
    env.pop("NOTIFY_HUB_ENDPOINT", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [_cli_python(), "-m", "notify_hub.cli", *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
        env=env,
    )


def _free_port() -> int:
    """先取一个空闲端口并立刻释放（仅用于「保证无人监听」的不可达场景）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --------------------------------------------------------------------------- #
# 进程外对端：本机 webhook 桩（真实 socket）
# --------------------------------------------------------------------------- #
class WebhookStub:
    """本机 HTTP 桩：真实 socket 上扮演 webhook 接收方，记录 JSON 载荷。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: list[dict] = []
        stub = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    payload = None
                with stub._lock:
                    stub.requests.append(
                        {
                            "path": self.path,
                            "headers": {
                                key.lower(): value for key, value in self.headers.items()
                            },
                            "json": payload,
                        }
                    )
                body = b'{"code": 0, "msg": "ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # 静音
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="webhook-stub-cli", daemon=True
        )
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/notify"

    def count(self) -> int:
        with self._lock:
            return len(self.requests)

    def payloads(self) -> list[dict]:
        with self._lock:
            return [dict(item["json"]) for item in self.requests if item["json"] is not None]

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


@pytest.fixture
def webhook_stub():
    stub = WebhookStub()
    try:
        yield stub
    finally:
        stub.close()


# --------------------------------------------------------------------------- #
# 真实 ASGI 服务器：uvicorn 于临时端口 + 后台线程，收尾放在 __exit__/finally
# --------------------------------------------------------------------------- #
def _serve_in_thread(server: uvicorn.Server) -> None:
    """``Server.serve()`` 是协程：在**本线程自己的**事件循环里跑它。"""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(server.serve())
    finally:
        loop.close()


class RealServer:
    """``uvicorn.Server`` 跑在后台线程；``port`` 为内核实际分配的端口（port=0）。"""

    def __init__(self, app) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        )
        self._thread: threading.Thread | None = None
        self.port: int = 0
        self.endpoint: str = ""

    def __enter__(self) -> "RealServer":
        self._thread = threading.Thread(
            target=_serve_in_thread, args=(self._server,), name="uvicorn-integration", daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if self._server.started and self._server.servers:
                sockets = self._server.servers[0].sockets
                if sockets:
                    self.port = sockets[0].getsockname()[1]
                    break
            if not self._thread.is_alive():
                raise RuntimeError("uvicorn 启动失败（线程已退出）")
            time.sleep(0.02)
        if not self.port:
            self.__exit__(None, None, None)
            raise RuntimeError("uvicorn 未在 15 秒内报告监听端口")
        self.endpoint = f"http://127.0.0.1:{self.port}"
        self._wait_until_healthy()
        return self

    def _wait_until_healthy(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        last: object = None
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{self.endpoint}/healthz", timeout=1.0)
                if response.status_code == 200:
                    return
                last = f"/healthz 返回 {response.status_code}"
            except Exception as exc:  # noqa: BLE001 - 启动期连接失败属预期
                last = exc
            time.sleep(0.05)
        raise RuntimeError(f"真实 uvicorn 未就绪: {last}")

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.endpoint, timeout=10.0)

    def __exit__(self, *exc_info) -> bool:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=20.0)
            assert not self._thread.is_alive(), "uvicorn 线程未在 20 秒内收尾"
        return False


# --------------------------------------------------------------------------- #
# 真实配置装配（M1 load_settings，凭据经环境变量解析）
# --------------------------------------------------------------------------- #
def _write_rules(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "case_sensitive": False,
                "defaults": {
                    "category": "uncategorized",
                    "labels": [],
                    "need_ack": False,
                    "channel": "hook",
                },
                "rules": [],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _make_settings(tmp_path: Path, webhook_url: str):
    _write_rules(tmp_path / "rules.yaml")
    config = {
        "server": {"host": "127.0.0.1", "port": 8000, "log_level": "INFO"},
        "storage": {"db_path": "./data/notify.db"},
        "rules": {"path": "./rules.yaml", "poll_interval_seconds": 5},
        "reminders": dict(_SLOW_REMINDERS),
        "default_channel": "hook",
        "channels": [
            {
                "id": "hook",
                "type": "webhook",
                "enabled": True,
                "params": {},
                "credentials": {"url": "STUB_WEBHOOK_URL"},
            }
        ],
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return load_settings(config_path, env={"STUB_WEBHOOK_URL": webhook_url})


@pytest.fixture
def served(tmp_path, webhook_stub, manual_clock):
    """真实 uvicorn + 真实 ``create_app`` 装配的完整上下文（含三个后台线程）。"""
    settings = _make_settings(tmp_path, webhook_stub.url)
    ctx = build_context(settings, clock=manual_clock)
    with RealServer(create_app(ctx=ctx)) as server:
        yield server, ctx


# --------------------------------------------------------------------------- #
# 1. CLI → 真实 uvicorn → app → 分类 → 落库 → 待办 → 真实 webhook 桩
# --------------------------------------------------------------------------- #
def test_cli_send_reaches_stub_channel_through_real_uvicorn(served, webhook_stub):
    server, ctx = served
    result = _run_cli(
        "--endpoint",
        server.endpoint,
        "--source",
        "t",
        "--title",
        "hi",
        "--level",
        "warning",
        "--need-ack",
    )

    assert result.returncode == 0, result.stderr
    assert "message_id" in result.stdout
    message_id = int(result.stdout.strip().split("message_id=")[1].split()[0])

    # 真实消费链路：uvicorn 的 lifespan 起了受理工作线程，投递真正走到桩渠道。
    assert webhook_stub.wait_for(1, timeout=10.0) is True, "桩渠道未收到 CLI 投递的通知"
    assert webhook_stub.count() == 1

    item = webhook_stub.requests[0]
    assert item["path"] == "/notify"
    payload = item["json"]
    assert payload["title"] == "hi"
    assert payload["source"] == "t"
    assert payload["level"] == "warning"
    assert payload["kind"] == "first_notice"
    assert payload["todo_id"] is None  # 首次通知不带待办 id（冻结文案）

    message = ctx.messages.get(message_id)
    assert message is not None
    assert message.source == "t"
    assert message.title == "hi"
    assert message.level == "warning"
    assert message.need_ack_declared is True
    assert as_utc(message.received_at) == as_utc(_from_iso(payload["occurred_at"]))

    with server.client() as client:
        todos = client.get("/api/v1/todos").json()["todos"]
    assert len(todos) == 1
    todo_id = todos[0]["id"]
    assert todos[0]["title"] == "hi"
    todo = ctx.todos.get(todo_id)
    assert todo is not None and todo.status == "pending" and todo.message_id == message_id

    records = ctx.messages.deliveries(message_id)
    assert len(records) == 1
    assert records[0].ok is True and records[0].channel_id == "hook"


# --------------------------------------------------------------------------- #
# 2. 服务端拒绝（真实校验失败）→ 退出码 1
# --------------------------------------------------------------------------- #
def test_cli_send_rejected_by_real_server_exits_1(served):
    server, ctx = served
    result = _run_cli("--endpoint", server.endpoint, "--source", "", "--title", "空来源")

    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert "错误" in result.stderr
    assert "source" in result.stderr, result.stderr
    assert "Traceback" not in (result.stdout + result.stderr)
    assert "message_id" not in result.stdout
    # 被拒绝的请求不得落库
    assert ctx.messages.count() == 0


# --------------------------------------------------------------------------- #
# 3. 服务不可达 → 退出码 3
# --------------------------------------------------------------------------- #
def test_cli_send_unreachable_server_exits_3():
    endpoint = f"http://127.0.0.1:{_free_port()}"
    result = _run_cli("--endpoint", endpoint, "--source", "t", "--title", "unreachable")

    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
    assert "无法连接" in result.stderr, result.stderr
    assert "message_id" not in result.stdout


# --------------------------------------------------------------------------- #
# 4. CLI ``todo list`` → 真实服务端排序（CLI 不得重排）
# --------------------------------------------------------------------------- #
def test_cli_todo_list_keeps_real_server_ordering(served, manual_clock):
    server, ctx = served
    with server.client() as client:
        for index, title in enumerate(("最久", "中等", "最新"), start=1):
            response = client.post(
                "/api/v1/messages",
                json={"source": f"src-{index}", "title": title, "need_ack": True},
            )
            assert response.status_code == 202
            manual_clock.advance(60)

    # 每条待办比前一条晚 1 分钟创建（此后不再推进时钟），三者超时各不相同。
    manual_clock.advance(60)

    result = _run_cli("todo", "list", "--endpoint", server.endpoint)
    assert result.returncode == 0, result.stderr
    rows = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(rows) == 3, result.stdout
    columns = [row.split("\t") for row in rows]
    for row in columns:
        assert len(row) == 4, row

    def total_minutes(text: str) -> int:
        match = re.fullmatch(r"(\d+)d (\d+)h (\d+)m", text)
        assert match is not None, text
        days, hours, minutes = (int(part) for part in match.groups())
        return days * 1440 + hours * 60 + minutes

    overdue = [total_minutes(row[1]) for row in columns]
    assert overdue == sorted(overdue, reverse=True), result.stdout
    assert len(set(overdue)) == 3, f"三条待办超时必须有区分度：{overdue}"
    # 服务端按 overdue_seconds 降序；CLI 必须原样保持该顺序（不得重排、不得改格式）。
    expected = [
        int((manual_clock.now() - as_utc(todo.first_notified_at)).total_seconds() // 60)
        for todo in sorted(
            ctx.todos.list(status=None),
            key=lambda view: view.first_notified_at,
        )
    ]
    assert overdue == expected, (overdue, expected, result.stdout)
    assert [row[3] for row in columns] == ["最久", "中等", "最新"]
    assert [row[2] for row in columns] == ["src-1", "src-2", "src-3"]
    assert [row[0] for row in columns] == ["1", "2", "3"]


# --------------------------------------------------------------------------- #
# 5. CLI ``todo done`` → 真实服务端状态迁移；``--all`` 状态列
# --------------------------------------------------------------------------- #
def test_cli_todo_done_through_real_server(served):
    server, ctx = served
    with server.client() as client:
        accepted = client.post(
            "/api/v1/messages",
            json={"source": "s", "title": "待完成", "need_ack": True},
        )
        assert accepted.status_code == 202
        todo_id = accepted.json()["todo_id"]

    done = _run_cli("todo", "done", str(todo_id), "--endpoint", server.endpoint)
    assert done.returncode == 0, done.stderr
    assert f"done {todo_id}" in done.stdout
    assert ctx.todos.get(todo_id).status == "done"

    listing = _run_cli("todo", "list", "--all", "--endpoint", server.endpoint)
    assert listing.returncode == 0, listing.stderr
    rows = [line for line in listing.stdout.splitlines() if line.strip()]
    assert len(rows) == 1, listing.stdout
    # ``--all`` 时行首是状态列（服务端原始状态值）
    assert rows[0].startswith("done\t"), rows[0]
    assert "\t待完成" in rows[0], rows[0]

    # 不存在的待办：真实 404 → 退出码 1
    missing = _run_cli("todo", "done", "999999", "--endpoint", server.endpoint)
    assert missing.returncode == 1, (missing.returncode, missing.stderr)
    assert "404" in missing.stderr, missing.stderr


# --------------------------------------------------------------------------- #
# 6. endpoint 解析：``--endpoint`` 覆盖环境变量（环境变量指向不可达端口）
# --------------------------------------------------------------------------- #
def test_cli_endpoint_flag_overrides_env_var(served):
    server, _ = served
    result = _run_cli(
        "--source",
        "t",
        "--title",
        "env-override",
        "--endpoint",
        server.endpoint,
        env_extra={"NOTIFY_HUB_ENDPOINT": f"http://127.0.0.1:{_free_port()}"},
    )
    assert result.returncode == 0, result.stderr
    assert "message_id" in result.stdout
