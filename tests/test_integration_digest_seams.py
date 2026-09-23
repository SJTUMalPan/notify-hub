"""作用域 2 补测：``add-daily-digest`` 的**跨模块接缝复核**。

本文件不重复 ``tests/test_digest.py`` / ``test_integration.py`` / ``test_integration_web.py`` /
``test_e2e.py`` 已有的断言，只补它们**没有真正跨过**的接缝：

1. **进程重启 × 同一份 SQLite**：既有重启用例
   （``test_digest.py::test_restart_does_not_resend_on_the_same_day``）手工重建服务对象，
   既不经过 ``create_app`` 的 lifespan，也不经过真实 socket。这里补两种重启：
   ``create_app`` 关停后重建（同日不重复、停机跨过触发时刻后不吞掉当天、次日正常发送），
   以及**真实子进程**边界上的重启（内存里的「今天发过了」必然丢失，只有落库状态能存活）。
2. **本地日期在 UTC 16:00 翻页**（Asia/Shanghai）：把时钟放在 ``15:59Z`` / ``16:01Z`` 两侧，
   验证 ``digest_runs.local_date`` 落在正确的**本地**自然日——用 UTC 日期实现会在第二次返回 0。
3. **汇总正文经真实适配器落到真实 socket 的实际形状**：序号、标题、已超时时长、来源、分类，
   以及 ``msg.title`` 与 ``msg.body`` 的分工（规格 3.3「正文渲染归属」）。
4. **失败重试 × 真实渠道**：真实桩先返 HTTP 500、后返 200 → 当日重试成功；跨天不补发。
5. **``/healthz`` 与 Web 页面在汇总「待发 / 投递失败 / 已投递」各阶段仍可用**。
6. **汇总走 ``default_channel``（真实 socket）**，不按各待办的首选渠道分流。
7. **完成入口 × 汇总**：经真实 Web 表单完成唯一一条待办后，当日与次日都不产生汇总。
8. **生产装配（``create_app(settings)``，真实 ``SystemClock``）**下的完整一轮 + 重启。

硬性约束：真实 ``create_app`` + 真实 lifespan（真实起停后台线程）、真实 SQLite 文件、
真实本机 ``http.server.ThreadingHTTPServer`` 桩（真实 socket）。M1–M8 内部边界**不使用任何替身**，
**不使用** ``httpx.MockTransport``。唯一被替身的是进程外的对端（webhook 接收方）。

打桩边界声明
------------
``DigestStub`` 每次 ``do_POST`` 都记录原始载荷并按**当前**状态码应答，状态可运行时翻转，
用于制造「真实渠道先失败、后成功」；它是进程外对端，不是模块内部边界。
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlmodel import select

from notify_hub.app import create_app
from notify_hub.clock import as_utc
from notify_hub.config import load_settings
from notify_hub.context import build_context
from notify_hub.models import DeliveryRecord, DigestRun, TodoEvent
from notify_hub.services.notifications import format_duration

UTC = timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")
_HUB_THREAD_PREFIX = "notify-hub-"

#: 时钟起点 2024-01-01T00:00:00Z == 本地（Asia/Shanghai）08:00。
_DIGEST_0900 = {"at": "09:00", "timezone": "Asia/Shanghai", "scan_interval_seconds": 3600}
_DIGEST_0000 = {"at": "00:00", "timezone": "Asia/Shanghai", "scan_interval_seconds": 3600}


def _hub_thread_names() -> set[str]:
    return {
        thread.name
        for thread in threading.enumerate()
        if thread.is_alive() and thread.name.startswith(_HUB_THREAD_PREFIX)
    }


# --------------------------------------------------------------------------- #
# 进程外对端：本机 webhook 桩（真实 socket，状态可翻转）
# --------------------------------------------------------------------------- #
class DigestStub:
    """本机 HTTP 桩：真实 socket 上扮演 webhook 接收方，应答状态码可运行时翻转。"""

    def __init__(self, *, status: int = 200, business_code: int = 0) -> None:
        self._lock = threading.Lock()
        self._status = status
        self._business_code = business_code
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
                    status = stub._status
                    body = json.dumps(
                        {"code": stub._business_code, "msg": "ok"}
                    ).encode("utf-8")
                    stub.requests.append(
                        {"path": self.path, "raw": raw, "json": payload}
                    )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # 静音
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="digest-seam-stub", daemon=True
        )
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/notify"

    # ------------------------------------------------------------------ #
    def set_status(self, status: int) -> None:
        with self._lock:
            self._status = status

    def count(self) -> int:
        with self._lock:
            return len(self.requests)

    def payloads(self) -> list[dict]:
        with self._lock:
            return [dict(item["json"]) for item in self.requests if item["json"] is not None]

    def reminder_payloads(self) -> list[dict]:
        return [payload for payload in self.payloads() if payload.get("kind") == "reminder"]

    def wait_for(self, expected: int, timeout: float = 5.0) -> bool:
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

    def __enter__(self) -> "DigestStub":
        return self

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False


@pytest.fixture
def stub():
    with DigestStub() as started:
        yield started


# --------------------------------------------------------------------------- #
# 真实配置装配（走 M1 的 load_settings，凭据经环境变量解析）
# --------------------------------------------------------------------------- #
def _write_rules(path: Path, rules, *, defaults_channel: str | None = None) -> None:
    document = {
        "case_sensitive": False,
        "defaults": {
            "category": "uncategorized",
            "labels": [],
            "need_ack": False,
            "channel": defaults_channel,
        },
        "rules": list(rules),
    }
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _webhook_channel(channel_id: str, env_name: str) -> dict:
    return {
        "id": channel_id,
        "type": "webhook",
        "enabled": True,
        "params": {},
        "credentials": {"url": env_name},
    }


def _make_settings(
    tmp_path: Path,
    *,
    channels,
    default_channel: str,
    reminders,
    env,
    rules=(),
    defaults_channel: str | None = None,
):
    _write_rules(tmp_path / "rules.yaml", rules, defaults_channel=defaults_channel)
    config = {
        "server": {"host": "127.0.0.1", "port": 8000, "log_level": "INFO"},
        "storage": {"db_path": "./data/notify.db"},
        "rules": {"path": "./rules.yaml", "poll_interval_seconds": 5},
        "reminders": dict(reminders),
        "default_channel": default_channel,
        "channels": list(channels),
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return load_settings(config_path, env=dict(env))


# --------------------------------------------------------------------------- #
# 只读地查真实 SQLite
# --------------------------------------------------------------------------- #
def _digest_rows(ctx) -> list[DigestRun]:
    with ctx.db.session() as session:
        return list(
            session.exec(select(DigestRun).order_by(DigestRun.local_date.asc())).all()
        )


def _digest_row(ctx, local_date: date) -> DigestRun | None:
    return next((row for row in _digest_rows(ctx) if row.local_date == local_date), None)


def _delivery_rows(ctx, *, event: str | None = None) -> list[DeliveryRecord]:
    statement = select(DeliveryRecord).order_by(DeliveryRecord.id.asc())
    if event is not None:
        statement = statement.where(DeliveryRecord.event == event)
    with ctx.db.session() as session:
        return list(session.exec(statement).all())


def _event_kinds(ctx, todo_id: int) -> list[str]:
    with ctx.db.session() as session:
        rows = list(
            session.exec(
                select(TodoEvent)
                .where(TodoEvent.todo_id == todo_id)
                .order_by(TodoEvent.occurred_at.asc(), TodoEvent.id.asc())
            ).all()
        )
    return [row.kind for row in rows]


def _complete_form_action(html: str, todo_id: int) -> str:
    pattern = re.compile(
        r'<form[^>]*method="post"[^>]*action="(?P<action>[^"]*)"[^>]*>', re.IGNORECASE
    )
    for match in pattern.finditer(html):
        action = match.group("action")
        if f"/todos/{todo_id}/done" in action:
            return action
    raise AssertionError(f"列表页未找到待办 {todo_id} 的完成表单：{html[:2000]}")


def _timeline(html: str) -> str:
    start = html.index("状态变更时间序列")
    end = html.index("<h2>待办</h2>", start)
    return html[start:end]


def _accept(client: TestClient, *, source: str, title: str, dedup_key: str, **extra):
    payload = {
        "source": source,
        "title": title,
        "level": "error",
        "need_ack": True,
        "dedup_key": dedup_key,
        **extra,
    }
    response = client.post("/api/v1/messages", json=payload)
    assert response.status_code == 202, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 真实**进程**重启用的子进程脚本：每个阶段都是一个独立的 OS 进程，
# 这样「已发送状态」若被放在内存/模块级变量里必然丢失，无法蒙混过关。
# --------------------------------------------------------------------------- #
_CHILD_SCRIPT = '''
import json
import sys
from datetime import datetime

from fastapi.testclient import TestClient

from notify_hub.app import create_app
from notify_hub.clock import ManualClock
from notify_hub.config import load_settings
from notify_hub.context import build_context

config_path, moment, phase, stub_url, dedup_key = sys.argv[1:6]
settings = load_settings(config_path, env={"STUB_URL": stub_url})
clock = ManualClock(datetime.fromisoformat(moment))
ctx = build_context(settings, clock=clock)
out = {"pid": __import__("os").getpid(), "phase": phase}
with TestClient(create_app(ctx=ctx)) as client:
    if phase == "accept":
        response = client.post(
            "/api/v1/messages",
            json={
                "source": "restart-src",
                "title": "进程重启",
                "level": "error",
                "need_ack": True,
                "dedup_key": dedup_key,
            },
        )
        out["accepted"] = response.status_code
        out["todo_id"] = response.json().get("todo_id")
        out["drained"] = ctx.pipeline.drain(timeout=5.0)
    out["run_once"] = ctx.scheduler.run_once()
print(json.dumps(out), flush=True)
'''


def _run_child(script: Path, config_path: Path, moment: str, phase: str, stub_url: str,
               dedup_key: str = "child-1") -> dict:
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, str(script), str(config_path), moment, phase, stub_url, dedup_key],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, f"子进程 {phase} 失败：\n{completed.stdout}\n{completed.stderr}"
    last = [line for line in completed.stdout.strip().splitlines() if line.startswith("{")][-1]
    return json.loads(last)


# --------------------------------------------------------------------------- #
# 接缝 1a：真实 create_app/lifespan 关停后在同一份 SQLite 上重建
#   —— 已发当日不重复；次日正常发送
# --------------------------------------------------------------------------- #
def test_restart_through_real_create_app_does_not_resend_and_sends_next_local_day(
    tmp_path, stub, manual_clock
):
    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel("hook", "STUB_URL")],
        default_channel="hook",
        defaults_channel="hook",
        reminders=_DIGEST_0900,
        env={"STUB_URL": stub.url},
    )

    # ---- 进程 A：受理 → 越过本地 09:00 → 发出一条汇总 ----
    ctx_a = build_context(settings, clock=manual_clock)
    app_a = create_app(ctx=ctx_a)
    with TestClient(app_a) as client:
        accepted = _accept(client, source="db-backup", title="备份失败", dedup_key="r-1")
        todo_id = accepted["todo_id"]
        assert ctx_a.pipeline.drain(timeout=5.0) is True
        assert stub.wait_for(1, timeout=5.0) is True
        assert stub.count() == 1

        manual_clock.advance(3600)  # 01:00Z == 本地 09:00
        assert ctx_a.scheduler.run_once() == 1
        assert stub.wait_for(2, timeout=5.0) is True
        assert stub.count() == 2
        assert stub.reminder_payloads()[-1]["title"] == "[待办汇总] 1 项未完成"
        assert _digest_row(ctx_a, date(2024, 1, 1)).delivered is True

    # lifespan 退出即真实停机：后台线程消失、连接池释放
    assert ctx_a.scheduler.running is False
    assert _hub_thread_names() == set()

    # ---- 进程 B：同一份 SQLite 上重建整个应用（新 Database / 新注册表 / 新调度线程） ----
    ctx_b = build_context(settings, clock=manual_clock)
    assert ctx_b.db is not ctx_a.db
    app_b = create_app(ctx=ctx_b)
    manual_clock.advance(60)  # 本地 09:01，仍在同一自然日
    with TestClient(app_b) as client:
        assert client.get("/healthz").status_code == 200
        assert ctx_b.scheduler.running is True
        assert ctx_b.scheduler.run_once() == 0, "重启后同一自然日不得重复发送"
        assert stub.count() == 2
        row = _digest_row(ctx_b, date(2024, 1, 1))
        assert row is not None
        assert row.delivered is True
        assert row.attempts == 1, "重启不得新增尝试次数（状态只在 digest_runs 里）"
        assert row.todo_count == 1

        # ---- 次日同一时刻：正常发送，且只覆盖仍未完成的那条 ----
        manual_clock.advance(86400)  # 本地 2024-01-02 09:01
        assert ctx_b.scheduler.run_once() == 1
        assert stub.wait_for(3, timeout=5.0) is True
        assert stub.count() == 3
        second = stub.reminder_payloads()[-1]
        assert second["title"] == "[待办汇总] 1 项未完成"
        assert "备份失败" in second["body"]
        assert _digest_row(ctx_b, date(2024, 1, 2)).delivered is True
        assert ctx_b.todos.get(todo_id).reminder_count == 2


# --------------------------------------------------------------------------- #
# 接缝 1a'：真实**操作系统进程**边界上的重启（内存里的「今天发过了」必然丢失）
#   每个阶段各起一个子进程：受理 → 触发日发送 → 同日重启不重复 → 次日正常发送。
# --------------------------------------------------------------------------- #
def test_true_process_restart_keeps_daily_state_in_sqlite(tmp_path, stub):
    from notify_hub.db import Database

    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel("hook", "STUB_URL")],
        default_channel="hook",
        defaults_channel="hook",
        reminders=_DIGEST_0900,
        env={"STUB_URL": stub.url},
    )
    script = tmp_path / "restart_child.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")

    # 进程 1：本地 08:00 受理消息 → 首次通知到达真实 socket，尚未到触发时刻
    first = _run_child(script, tmp_path / "config.yaml",
                       "2024-01-01T00:00:00+00:00", "accept", stub.url)
    assert first["accepted"] == 202
    assert first["drained"] is True
    assert first["run_once"] == 0
    assert stub.wait_for(1, timeout=5.0) is True
    assert stub.count() == 1

    # 进程 2：本地 09:00 → 发出当天唯一一条汇总后退出（进程消失）
    second = _run_child(script, tmp_path / "config.yaml",
                        "2024-01-01T01:00:00+00:00", "fire", stub.url)
    assert second["run_once"] == 1
    assert second["pid"] != first["pid"]
    assert stub.wait_for(2, timeout=5.0) is True
    assert stub.count() == 2

    # 进程 3：同一自然日稍晚重启 → 不得重复发送（内存标记在这里必然丢失）
    third = _run_child(script, tmp_path / "config.yaml",
                       "2024-01-01T01:01:00+00:00", "fire", stub.url)
    assert third["run_once"] == 0, "真实进程重启后同一自然日不得重复发送"
    assert stub.count() == 2

    # 进程 4：次日同一时刻 → 正常发送
    fourth = _run_child(script, tmp_path / "config.yaml",
                        "2024-01-02T01:00:00+00:00", "fire", stub.url)
    assert fourth["run_once"] == 1
    assert stub.wait_for(3, timeout=5.0) is True
    assert stub.count() == 3

    # 落库的每日状态直接对账（父进程另开一个 Database 读同一份文件）
    probe = Database(settings.db_path)
    probe.init_schema()
    try:
        with probe.session() as session:
            rows = list(
                session.exec(select(DigestRun).order_by(DigestRun.local_date.asc())).all()
            )
    finally:
        probe.dispose()
    assert [row.local_date for row in rows] == [date(2024, 1, 1), date(2024, 1, 2)]
    assert [row.delivered for row in rows] == [True, True]
    assert [row.attempts for row in rows] == [1, 1]
    assert [row.todo_count for row in rows] == [1, 1]
    assert [payload["title"] for payload in stub.reminder_payloads()] == [
        "[待办汇总] 1 项未完成",
        "[待办汇总] 1 项未完成",
    ]


# --------------------------------------------------------------------------- #
# 接缝 1b：停机期间跨过触发时刻 → 重启后**不吞掉**当天的汇总
# --------------------------------------------------------------------------- #
def test_restart_after_trigger_passed_without_record_still_sends_today(
    tmp_path, stub, manual_clock
):
    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel("hook", "STUB_URL")],
        default_channel="hook",
        defaults_channel="hook",
        reminders=_DIGEST_0900,
        env={"STUB_URL": stub.url},
    )

    ctx_a = build_context(settings, clock=manual_clock)
    with TestClient(create_app(ctx=ctx_a)) as client:
        _accept(client, source="s", title="停机前后新建", dedup_key="r-2")
        assert ctx_a.pipeline.drain(timeout=5.0) is True
        assert stub.wait_for(1, timeout=5.0) is True
        # 进程 A 停机时尚未到触发时刻，当天没有任何 digest_runs 记录
        assert _digest_row(ctx_a, date(2024, 1, 1)) is None

    # 停机期间时钟越过本地 09:00
    manual_clock.set(datetime(2024, 1, 1, 1, 5, tzinfo=UTC))  # 本地 09:05

    ctx_b = build_context(settings, clock=manual_clock)
    with TestClient(create_app(ctx=ctx_b)) as client:
        assert client.get("/healthz").status_code == 200
        assert ctx_b.scheduler.run_once() == 1, "触发时刻之后重启不得吞掉当天汇总"
        assert stub.wait_for(2, timeout=5.0) is True
        assert stub.count() == 2
        row = _digest_row(ctx_b, date(2024, 1, 1))
        assert row is not None
        assert row.delivered is True
        assert row.todo_count == 1
        assert as_utc(row.fired_at) == manual_clock.now()
        assert ctx_b.scheduler.run_once() == 0
        assert stub.count() == 2


# --------------------------------------------------------------------------- #
# 接缝 2：本地日期在 UTC 16:00 翻页（最容易错的接缝）
#   用 UTC 日期当 local_date 的实现，在 UTC 16:01 那一轮会认为「1 月 1 日已投递」而返回 0。
# --------------------------------------------------------------------------- #
def test_local_date_flips_at_utc_1600_not_at_utc_midnight(
    tmp_path, stub, manual_clock
):
    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel("hook", "STUB_URL")],
        default_channel="hook",
        defaults_channel="hook",
        reminders=_DIGEST_0000,
        env={"STUB_URL": stub.url},
    )
    ctx = build_context(settings, clock=manual_clock)
    with TestClient(create_app(ctx=ctx)) as client:
        accepted = _accept(client, source="edge", title="跨日边界", dedup_key="r-3")
        todo_id = accepted["todo_id"]
        assert ctx.pipeline.drain(timeout=5.0) is True
        assert stub.wait_for(1, timeout=5.0) is True

        # UTC 15:59 → 本地 2024-01-01 23:59（本地日期 = UTC 日期）
        manual_clock.set(datetime(2024, 1, 1, 15, 59, tzinfo=UTC))
        assert ctx.scheduler.run_once() == 1
        assert stub.wait_for(2, timeout=5.0) is True
        first = stub.reminder_payloads()[-1]
        assert first["occurred_at"] == as_utc(manual_clock.now()).isoformat()

        # UTC 16:01 → 本地 2024-01-02 00:01（本地日期已翻页，而 UTC 日期仍是 1 月 1 日）
        manual_clock.set(datetime(2024, 1, 1, 16, 1, tzinfo=UTC))
        assert ctx.scheduler.run_once() == 1, (
            "本地日期已翻到 2024-01-02；按 UTC 日期记录的实现会误判为已投递而返回 0"
        )
        assert stub.wait_for(3, timeout=5.0) is True

        # 本地 1 月 2 日随后定案
        manual_clock.advance(60)
        assert ctx.scheduler.run_once() == 0

        rows = _digest_rows(ctx)
        assert [row.local_date for row in rows] == [date(2024, 1, 1), date(2024, 1, 2)]
        assert all(row.delivered for row in rows)
        assert [row.attempts for row in rows] == [1, 1]
        assert as_utc(rows[0].fired_at) == datetime(2024, 1, 1, 15, 59, tzinfo=UTC)
        assert as_utc(rows[1].fired_at) == datetime(2024, 1, 1, 16, 1, tzinfo=UTC)

        # 一条待办被两个**本地**自然日各覆盖一次
        assert ctx.todos.get(todo_id).reminder_count == 2
        assert len(_delivery_rows(ctx, event="reminder")) == 2
        assert len(stub.reminder_payloads()) == 2


# --------------------------------------------------------------------------- #
# 接缝 3：汇总正文经真实适配器落到真实 socket 的实际形状（规格 3.3）
# --------------------------------------------------------------------------- #
def test_digest_payload_shape_over_real_socket_matches_spec_3_3(
    tmp_path, stub, manual_clock
):
    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel("hook", "STUB_URL")],
        default_channel="hook",
        defaults_channel="hook",
        reminders=_DIGEST_0900,
        env={"STUB_URL": stub.url},
    )
    ctx = build_context(settings, clock=manual_clock)
    with TestClient(create_app(ctx=ctx)) as client:
        first = _accept(
            client, source="db-backup", title="备份失败", dedup_key="shape-a"
        )
        assert ctx.pipeline.drain(timeout=5.0) is True
        assert stub.wait_for(1, timeout=5.0) is True

        manual_clock.advance(1800)  # 本地 08:30
        second = _accept(
            client, source="cert-monitor", title="证书将过期", dedup_key="shape-b"
        )
        assert ctx.pipeline.drain(timeout=5.0) is True
        assert stub.wait_for(2, timeout=5.0) is True

        manual_clock.advance(1800)  # 01:00Z == 本地 09:00
        trigger = manual_clock.now()
        assert ctx.scheduler.run_once() == 1
        assert stub.wait_for(3, timeout=5.0) is True

        assert len(stub.reminder_payloads()) == 1, "两条待办只能合并成一条汇总"
        payload = stub.reminder_payloads()[0]
        assert payload["kind"] == "reminder"
        assert payload["level"] == "warning"
        assert payload["source"] == "notify-hub"
        assert payload["category"] is None
        assert payload["todo_id"] is None
        assert payload["overdue_seconds"] is None
        assert payload["occurred_at"] == as_utc(trigger).isoformat()

        # msg.title 是独立的一段；正文不得重复表头（规格 3.3 正文渲染归属不变量）
        assert payload["title"] == "[待办汇总] 2 项未完成"
        assert "[待办汇总]" not in payload["body"]

        # 逐条明细：序号 + 标题 + 已超时时长 + 来源 + 分类，时长由真实 DB 的
        # first_notified_at 与触发时刻算出（跨 TodoService → notifications → 适配器）
        expected_lines = []
        for body in (first, second):
            todo = ctx.todos.get(body["todo_id"])
            overdue = (as_utc(trigger) - as_utc(todo.first_notified_at)).total_seconds()
            clauses = [
                f"已超时 {format_duration(overdue)}",
                f"来源 {todo.source}",
            ]
            if todo.category:
                clauses.append(f"分类 {todo.category}")
            expected_lines.append(
                (overdue, f"{todo.title}（{'；'.join(clauses)}）")
            )
        expected_lines.sort(key=lambda item: item[0], reverse=True)
        rendered = [f"{index}. {line}" for index, (_, line) in enumerate(expected_lines, 1)]

        lines = payload["body"].split("\n")
        assert lines[:2] == rendered
        assert lines[2:] == ["", "请到待办页面处理。"]

        # 与真实时长对照：3600 秒 → 1 小时；1800 秒 → 30 分钟
        assert "已超时 1 小时" in payload["body"]
        assert "已超时 30 分钟" in payload["body"]
        assert "来源 db-backup" in payload["body"]
        assert "来源 cert-monitor" in payload["body"]


# --------------------------------------------------------------------------- #
# 接缝 4a：真实渠道先失败、后成功 → 当日重试成功
# --------------------------------------------------------------------------- #
def test_real_channel_failure_then_success_retries_within_same_local_day(
    tmp_path, stub, manual_clock
):
    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel("hook", "STUB_URL")],
        default_channel="hook",
        defaults_channel="hook",
        reminders=_DIGEST_0900,
        env={"STUB_URL": stub.url},
    )
    ctx = build_context(settings, clock=manual_clock)
    with TestClient(create_app(ctx=ctx)) as client:
        accepted = _accept(client, source="s", title="渠道先坏后好", dedup_key="r-4")
        todo_id = accepted["todo_id"]
        assert ctx.pipeline.drain(timeout=5.0) is True
        assert stub.wait_for(1, timeout=5.0) is True

        # 渠道此时开始返回真实 HTTP 500（仍在同一个 socket 上）
        stub.set_status(500)
        manual_clock.advance(3600)  # 本地 09:00
        assert ctx.scheduler.run_once() == 0
        assert stub.count() == 2, "失败的那一轮也必须真的打到渠道（不是被内部桩挡掉）"

        failed = _digest_row(ctx, date(2024, 1, 1))
        assert failed is not None
        assert failed.delivered is False
        assert failed.attempts == 1
        assert failed.fired_at is None
        assert failed.todo_count == 1
        assert failed.last_error and "HTTP 500" in failed.last_error

        records = _delivery_rows(ctx, event="reminder")
        assert len(records) == 1
        assert records[0].ok is False
        assert records[0].channel_id == "hook"
        assert records[0].todo_id is None and records[0].message_id is None
        assert ctx.todos.get(todo_id).reminder_count == 0
        assert _event_kinds(ctx, todo_id) == ["created"]

        # 渠道恢复 → 同一自然日内的下一轮重试成功
        stub.set_status(200)
        manual_clock.advance(60)
        assert ctx.scheduler.run_once() == 1
        assert stub.wait_for(3, timeout=5.0) is True
        assert stub.count() == 3

        recovered = _digest_row(ctx, date(2024, 1, 1))
        assert recovered is not None
        assert recovered.delivered is True
        assert recovered.attempts == 2
        assert as_utc(recovered.checked_at) == as_utc(failed.checked_at)
        assert as_utc(recovered.fired_at) == manual_clock.now()

        records = _delivery_rows(ctx, event="reminder")
        assert [(record.channel_id, record.ok) for record in records] == [
            ("hook", False),
            ("hook", True),
        ]
        assert ctx.todos.get(todo_id).reminder_count == 1
        assert _event_kinds(ctx, todo_id) == ["created", "reminder"]

        # 当日已送达，后续轮次不再发送
        manual_clock.advance(3600)
        assert ctx.scheduler.run_once() == 0
        assert stub.count() == 3


# --------------------------------------------------------------------------- #
# 接缝 4b：跨天不补发（含真实重启）
# --------------------------------------------------------------------------- #
def test_failed_day_is_not_retried_after_restart_on_next_local_day(
    tmp_path, stub, manual_clock
):
    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel("hook", "STUB_URL")],
        default_channel="hook",
        defaults_channel="hook",
        reminders=_DIGEST_0900,
        env={"STUB_URL": stub.url},
    )
    ctx_a = build_context(settings, clock=manual_clock)
    with TestClient(create_app(ctx=ctx_a)) as client:
        accepted = _accept(client, source="s", title="昨日失败", dedup_key="r-5")
        todo_id = accepted["todo_id"]
        assert ctx_a.pipeline.drain(timeout=5.0) is True
        assert stub.wait_for(1, timeout=5.0) is True

        stub.set_status(500)
        manual_clock.advance(3600)  # 本地 2024-01-01 09:00
        assert ctx_a.scheduler.run_once() == 0
        assert _digest_row(ctx_a, date(2024, 1, 1)).delivered is False
        assert ctx_a.todos.get(todo_id).reminder_count == 0

    # 停机；次日同一时刻重启，渠道已恢复
    stub.set_status(200)
    manual_clock.set(datetime(2024, 1, 2, 1, 0, tzinfo=UTC))  # 本地 2024-01-02 09:00

    ctx_b = build_context(settings, clock=manual_clock)
    with TestClient(create_app(ctx=ctx_b)) as client:
        assert ctx_b.scheduler.run_once() == 1

        rows = _digest_rows(ctx_b)
        assert [row.local_date for row in rows] == [date(2024, 1, 1), date(2024, 1, 2)]
        yesterday, today = rows
        # 昨日失败不再被重试：attempts 不增长、没有新的投递记录指向它
        assert yesterday.delivered is False
        assert yesterday.attempts == 1
        assert yesterday.last_error and "HTTP 500" in yesterday.last_error
        assert yesterday.fired_at is None
        # 次日是全新的记录
        assert today.delivered is True
        assert today.attempts == 1
        assert as_utc(today.fired_at) == manual_clock.now()

        records = _delivery_rows(ctx_b, event="reminder")
        assert [(record.channel_id, record.ok) for record in records] == [
            ("hook", False),
            ("hook", True),
        ]
        # 该待办只在次日被汇总提醒一次
        assert ctx_b.todos.get(todo_id).reminder_count == 1
        assert _event_kinds(ctx_b, todo_id) == ["created", "reminder"]
        assert len(stub.reminder_payloads()) == 2


# --------------------------------------------------------------------------- #
# 接缝 5：/healthz 与 Web 页面在汇总「待发 / 投递失败 / 已投递」各阶段仍可用
# --------------------------------------------------------------------------- #
def test_health_and_web_pages_remain_usable_across_digest_states(
    tmp_path, stub, manual_clock
):
    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel("hook", "STUB_URL")],
        default_channel="hook",
        defaults_channel="hook",
        reminders=_DIGEST_0900,
        env={"STUB_URL": stub.url},
    )
    ctx = build_context(settings, clock=manual_clock)
    app = create_app(ctx=ctx)
    with TestClient(app, follow_redirects=False) as client:
        accepted = _accept(client, source="web", title="阶段探活", dedup_key="r-6")
        todo_id = accepted["todo_id"]
        assert ctx.pipeline.drain(timeout=5.0) is True
        assert stub.wait_for(1, timeout=5.0) is True

        # ---- 阶段 1：触发时刻之前（待发） ----
        assert client.get("/healthz").json()["status"] == "ok"
        listing = client.get("/todos")
        assert listing.status_code == 200 and "阶段探活" in listing.text
        assert client.get("/messages").status_code == 200
        assert client.get(f"/todos/{todo_id}").status_code == 200

        # ---- 阶段 2：渠道不可用导致汇总投递失败 ----
        stub.set_status(500)
        manual_clock.advance(3600)
        assert ctx.scheduler.run_once() == 0
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/todos").status_code == 200
        assert "阶段探活" in client.get("/todos").text, "投递失败不得影响页面读取"
        detail = client.get(f"/todos/{todo_id}")
        assert detail.status_code == 200
        assert "待完成" in detail.text
        assert "提醒" not in _timeline(detail.text), "失败的那次不得计为一次提醒"
        assert client.get("/api/v1/todos").json()["total"] == 1

        # ---- 阶段 3：渠道恢复、汇总已投递 ----
        stub.set_status(200)
        manual_clock.advance(60)
        assert ctx.scheduler.run_once() == 1
        assert stub.wait_for(3, timeout=5.0) is True
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/todos").status_code == 200
        assert client.get("/messages").status_code == 200
        detail = client.get(f"/todos/{todo_id}")
        assert detail.status_code == 200
        assert "待完成" in detail.text
        assert _timeline(detail.text).count("提醒") == 1
        # 汇总记录不挂在待办详情里（决策 5），但逐条提醒事件可读
        assert ctx.todos.get(todo_id).reminder_count == 1


# --------------------------------------------------------------------------- #
# 接缝 6：汇总走 default_channel（真实 socket），不按各待办的首选渠道分流
# --------------------------------------------------------------------------- #
def test_digest_goes_to_default_channel_socket_not_todo_preferred_channel(
    tmp_path, manual_clock
):
    with DigestStub() as hook_stub, DigestStub() as pager_stub:
        rules = [
            {
                "id": "pager-rule",
                "match": {"source": ["pager-src"]},
                "category": "pager-cat",
                "labels": ["pager"],
                "need_ack": True,
                "channel": "pager",
            }
        ]
        settings = _make_settings(
            tmp_path,
            channels=[
                _webhook_channel("hook", "STUB_URL"),
                _webhook_channel("pager", "PAGER_URL"),
            ],
            default_channel="hook",
            reminders=_DIGEST_0900,
            env={"STUB_URL": hook_stub.url, "PAGER_URL": pager_stub.url},
            rules=rules,
        )
        ctx = build_context(settings, clock=manual_clock)
        assert ctx.registry.available_ids() == ("hook", "pager")
        with TestClient(create_app(ctx=ctx)) as client:
            accepted = client.post(
                "/api/v1/messages",
                json={"source": "pager-src", "title": "支付网关 5xx", "level": "error"},
            )
            assert accepted.status_code == 202
            todo_id = accepted.json()["todo_id"]
            assert ctx.todos.get(todo_id).preferred_channel == "pager"

            assert ctx.pipeline.drain(timeout=5.0) is True
            # 首次通知按待办的首选渠道走
            assert pager_stub.wait_for(1, timeout=5.0) is True
            assert hook_stub.count() == 0

            # 汇总只有一个去向：default_channel
            manual_clock.advance(3600)
            assert ctx.scheduler.run_once() == 1
            assert hook_stub.wait_for(1, timeout=5.0) is True
            assert pager_stub.count() == 1, "汇总不得按待办首选渠道再发一份"

            payload = hook_stub.reminder_payloads()[0]
            assert payload["title"] == "[待办汇总] 1 项未完成"
            assert "支付网关 5xx" in payload["body"]

            records = _delivery_rows(ctx, event="reminder")
            assert len(records) == 1
            assert records[0].channel_id == "hook"
            assert records[0].is_preferred is False
            assert records[0].is_fallback is False
            assert ctx.todos.get(todo_id).reminder_count == 1


# --------------------------------------------------------------------------- #
# 接缝 7：完成入口（真实 Web 表单）× 汇总 —— 当日与次日都不再产生汇总
# --------------------------------------------------------------------------- #
def test_completing_the_only_todo_via_web_form_produces_no_digest_today_or_next_day(
    tmp_path, stub, manual_clock
):
    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel("hook", "STUB_URL")],
        default_channel="hook",
        defaults_channel="hook",
        reminders=_DIGEST_0900,
        env={"STUB_URL": stub.url},
    )
    ctx = build_context(settings, clock=manual_clock)
    with TestClient(create_app(ctx=ctx), follow_redirects=False) as client:
        accepted = _accept(client, source="done-src", title="唯一待办", dedup_key="r-7")
        todo_id = accepted["todo_id"]
        assert ctx.pipeline.drain(timeout=5.0) is True
        assert stub.wait_for(1, timeout=5.0) is True

        # 触发时刻之前，经真实 Web 表单完成
        listing = client.get("/todos")
        action = _complete_form_action(listing.text, todo_id)
        done = client.post(action, data={})
        assert done.status_code == 303
        assert ctx.todos.get(todo_id).status == "done"

        # ---- 当日越过触发时刻：无未完成待办 → 不发消息，但记「已检查」 ----
        manual_clock.advance(3600)  # 本地 09:00
        assert ctx.scheduler.run_once() == 0
        assert stub.count() == 1
        today = _digest_row(ctx, date(2024, 1, 1))
        assert today is not None
        assert today.todo_count == 0
        assert today.delivered is True
        assert today.fired_at is None
        assert _delivery_rows(ctx, event="reminder") == []

        # ---- 次日：同样不产生汇总 ----
        manual_clock.advance(86400)  # 本地 2024-01-02 09:00
        assert ctx.scheduler.run_once() == 0
        assert stub.count() == 1
        tomorrow = _digest_row(ctx, date(2024, 1, 2))
        assert tomorrow is not None
        assert tomorrow.todo_count == 0
        assert _delivery_rows(ctx, event="reminder") == []
        assert stub.reminder_payloads() == []

        assert ctx.todos.get(todo_id).reminder_count == 0
        assert _event_kinds(ctx, todo_id) == ["created", "completed"]


# --------------------------------------------------------------------------- #
# 接缝 8：生产装配（create_app(settings)，真实 SystemClock）跑完整一轮 + 重启
#   既有集成用例全部注入 ManualClock；这里验证「不注入任何时钟」的真实路径
#   （SystemClock → settings.zone → local_date → 真实 socket → SQLite）同样成立。
# --------------------------------------------------------------------------- #
def test_production_wiring_with_real_system_clock_fires_today_and_restart_is_idempotent(
    tmp_path, stub
):
    real_local = datetime.now(SHANGHAI)
    if real_local.hour == 0 and real_local.minute < 2:
        pytest.skip("真实本地时间刚过午夜：跨日窗口内「重启不重发」的断言不稳定")

    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel("hook", "STUB_URL")],
        default_channel="hook",
        defaults_channel="hook",
        # 00:00 意味着「今天已过触发时刻」；scan_interval 取上限让后台线程不抢跑，
        # 由测试用真实 run_once() 精确驱动（生产装配的调度器代码路径不变）。
        reminders=_DIGEST_0000,
        env={"STUB_URL": stub.url},
    )

    # 不注入 clock / registry：完全走 create_app(settings) 的生产装配
    app = create_app(settings)
    ctx = app.state.ctx
    with TestClient(app) as client:
        accepted = _accept(client, source="prod", title="生产装配", dedup_key="r-8")
        todo_id = accepted["todo_id"]
        assert ctx.pipeline.drain(timeout=5.0) is True
        assert stub.wait_for(1, timeout=5.0) is True

        assert ctx.scheduler.run_once() == 1
        assert stub.wait_for(2, timeout=5.0) is True
        payload = stub.reminder_payloads()[-1]
        assert payload["title"] == "[待办汇总] 1 项未完成"
        assert "生产装配" in payload["body"]

        fired_local_date = (
            datetime.fromisoformat(payload["occurred_at"]).astimezone(SHANGHAI).date()
        )
        rows = _digest_rows(ctx)
        assert [row.local_date for row in rows] == [fired_local_date]
        assert rows[0].local_date == datetime.now(SHANGHAI).date()
        assert rows[0].delivered is True
        assert rows[0].todo_count == 1

    # 同一份 SQLite 上重建（新 SystemClock / 新 Database）：当天不重发
    restarted = create_app(settings)
    ctx_b = restarted.state.ctx
    with TestClient(restarted) as client:
        assert client.get("/healthz").status_code == 200
        assert ctx_b.scheduler.run_once() == 0
        assert stub.count() == 2
        rows = _digest_rows(ctx_b)
        assert [row.local_date for row in rows] == [fired_local_date]
        assert rows[0].attempts == 1
        assert ctx_b.todos.get(todo_id).reminder_count == 1
