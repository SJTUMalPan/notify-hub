"""M7 模块测试 —— HTTP 接入层与受理编排（`tests/test_ingest.py`）。

覆盖 ``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 6 节「模块 M7」第 4 段
验证方法的第 1–10、13、14 条（第 11、12 条在 ``tests/test_api_todos.py``）。

阶段 A（``src/notify_hub/api/``、``src/notify_hub/pipeline.py`` 尚不存在）下本文件
**必然是红的**，且红的形态必须是「运行时实现缺失」（fixture 装配时 ModuleNotFoundError），
而不是收集阶段的语法/导入错误。所有对 M1–M7 的导入都在测试函数或 fixture 函数体内惰性完成。

请求/响应字段名是冻结契约（M5 CLI、M8 Web、阶段 D 集成测试依赖），不得自行发明。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import httpx

from notify_hub.domain import AckReason, Level, TodoStatus

#: 与 ``tests/conftest.py`` 的 ``FAKE_TOKEN`` 同值：用于断言日志/响应中不出现它。
FAKE_TOKEN = "SECRETTOKEN"

MSG_URL = "/api/v1/messages"
BATCH_URL = "/api/v1/messages/batch"
HEALTH_URL = "/healthz"


# --------------------------------------------------------------------------- #
# 本地小工具（只依赖标准库/已安装依赖）
# --------------------------------------------------------------------------- #
def _parse_dt(text: str) -> datetime:
    """解析响应里的 ISO 8601 时间（Python 3.10 的 fromisoformat 不认尾部 Z）。"""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _locs(resp: httpx.Response) -> list[str]:
    """展平 FastAPI 默认 422 形状 ``{"detail": [{"loc": [...]}]}`` 中的字段名。"""
    detail = resp.json()["detail"]
    return [str(part) for item in detail for part in item["loc"]]


def _failing_transport() -> httpx.MockTransport:
    """恒返回 HTTP 500 的 webhook 传输层（M4 冻结的测试接缝）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream boom")

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- #
# 第 1–2 条：成功投递 / 进入待办
# --------------------------------------------------------------------------- #
def test_post_message_accepted_and_persisted(api_client, ctx):
    resp = api_client.post(
        MSG_URL,
        json={"source": "s", "title": "t", "level": "error", "need_ack": True},
    )

    assert resp.status_code == 202
    payload = resp.json()
    assert "message_id" in payload
    assert isinstance(payload["message_id"], int)

    stored = ctx.messages.get(payload["message_id"])
    assert stored is not None
    assert stored.source == "s"
    assert stored.title == "t"
    assert stored.level == Level.ERROR


def test_post_message_with_need_ack_creates_pending_todo(api_client, ctx):
    resp = api_client.post(
        MSG_URL,
        json={"source": "s", "title": "t", "level": "error", "need_ack": True},
    )

    assert resp.status_code == 202
    todo_id = resp.json()["todo_id"]
    assert todo_id is not None

    todo = ctx.todos.get(todo_id)
    assert todo is not None
    assert todo.status == TodoStatus.PENDING
    assert todo.ack_reason == AckReason.CALLER_DECLARED


# --------------------------------------------------------------------------- #
# 第 3–5 条：校验失败一律 422 且不写库
# --------------------------------------------------------------------------- #
def test_missing_required_field_rejected_without_persisting(api_client, ctx):
    resp = api_client.post(MSG_URL, json={"title": "t"})

    assert resp.status_code == 422
    assert "source" in _locs(resp)
    assert ctx.messages.count() == 0


def test_invalid_level_rejected_without_persisting(api_client, ctx):
    resp = api_client.post(MSG_URL, json={"source": "s", "title": "t", "level": "critical"})

    assert resp.status_code == 422
    assert "level" in _locs(resp)
    # spec message-ingest：响应需指明 level 的合法取值范围。
    assert "info" in resp.text
    assert "warning" in resp.text
    assert "error" in resp.text
    assert ctx.messages.count() == 0


def test_wrong_field_types_rejected_without_persisting(api_client, ctx):
    resp = api_client.post(MSG_URL, json={"source": 1, "title": []})

    assert resp.status_code == 422
    assert ctx.messages.count() == 0


# --------------------------------------------------------------------------- #
# 第 6 条：可选字段缺省语义
# --------------------------------------------------------------------------- #
def test_optional_field_defaults(api_client, ctx):
    resp = api_client.post(MSG_URL, json={"source": "s", "title": "t"})

    assert resp.status_code == 202
    payload = resp.json()
    assert payload["todo_id"] is None

    stored = ctx.messages.get(payload["message_id"])
    assert stored is not None
    assert stored.level == Level.INFO
    assert stored.need_ack_declared is False
    assert stored.occurred_at == stored.received_at
    assert ctx.todos.list() == []


# --------------------------------------------------------------------------- #
# 第 7–8 条：批量投递
# --------------------------------------------------------------------------- #
def test_batch_partial_failure_keeps_valid_items(api_client, ctx):
    resp = api_client.post(
        BATCH_URL,
        json=[
            {"source": "s1", "title": "t1"},
            {"title": "t2"},
            {"source": "s3", "title": "t3"},
        ],
    )

    assert resp.status_code == 207
    payload = resp.json()
    assert payload["accepted_count"] == 2
    assert payload["rejected_count"] == 1

    results = payload["results"]
    assert [item["index"] for item in results] == [0, 1, 2]
    assert [item["accepted"] for item in results] == [True, False, True]

    assert isinstance(results[0]["message_id"], int)
    assert results[1]["index"] == 1
    assert results[1]["message_id"] is None
    assert "source" in results[1]["error_fields"]
    assert results[1]["error"]
    assert isinstance(results[2]["message_id"], int)
    assert results[2]["message_id"] != results[0]["message_id"]

    assert ctx.messages.count() == 2


def test_batch_body_must_be_json_array(api_client, ctx):
    resp = api_client.post(BATCH_URL, json={"messages": []})

    assert resp.status_code == 422
    assert ctx.messages.count() == 0


# --------------------------------------------------------------------------- #
# 第 9 条：慢渠道下受理不阻塞响应（唯一需要 run_inline=False 的用例）
# --------------------------------------------------------------------------- #
def test_accept_does_not_block_on_slow_channel(
    tmp_settings, manual_clock, make_recording_notifier
):
    """`create_api_app` 不启动后台线程，因此本用例自行 start/stop 工作线程。"""
    from fastapi.testclient import TestClient

    from notify_hub.api import create_api_app
    from notify_hub.context import build_context
    from notify_hub.notifiers import NotifierRegistry

    slow = make_recording_notifier("slow", delay=2.0)
    registry = NotifierRegistry()
    registry.register(slow)
    ctx = build_context(tmp_settings, clock=manual_clock, registry=registry)

    ctx.pipeline.start()
    try:
        with TestClient(create_api_app(ctx)) as client:
            started = time.monotonic()
            resp = client.post(MSG_URL, json={"source": "s", "title": "t"})
            elapsed = time.monotonic() - started

        assert resp.status_code == 202
        assert elapsed < 0.5, f"受理被慢渠道阻塞了 {elapsed:.3f}s"

        assert ctx.pipeline.drain(timeout=5.0) is True
    finally:
        ctx.pipeline.stop()

    assert len(slow.sent) == 1
    records = ctx.messages.deliveries(resp.json()["message_id"])
    assert len(records) == 1
    assert records[0].ok is True


# --------------------------------------------------------------------------- #
# 第 10 条：消息详情
# --------------------------------------------------------------------------- #
def test_message_detail_returns_meta_category_and_deliveries(api_client):
    meta = {"a": 1, "b": [1, 2]}
    resp = api_client.post(
        MSG_URL, json={"source": "s", "title": "t", "body": "hello", "meta": meta}
    )
    assert resp.status_code == 202
    message_id = resp.json()["message_id"]

    detail = api_client.get(f"{MSG_URL}/{message_id}")
    assert detail.status_code == 200
    payload = detail.json()

    assert payload["id"] == message_id
    assert payload["source"] == "s"
    assert payload["title"] == "t"
    assert payload["body"] == "hello"
    assert payload["level"] == "info"
    assert payload["meta"] == meta
    assert payload["occurred_at"] is not None
    _parse_dt(payload["received_at"])

    # conftest 的最小规则集没有任何规则，因此 rule_id 为 None、category 取 defaults。
    assert payload["rule_id"] is None
    assert payload["category"] == "uncategorized"

    deliveries = payload["deliveries"]
    assert isinstance(deliveries, list)
    assert len(deliveries) >= 1
    first = deliveries[0]
    assert first["channel_id"] == "recording"
    assert first["ok"] is True
    assert isinstance(first["is_preferred"], bool)
    assert first["event"] == "first_notice"


def test_message_detail_not_found(api_client):
    resp = api_client.get(f"{MSG_URL}/999999")

    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# 第 13 条：健康检查不依赖渠道可用性
# --------------------------------------------------------------------------- #
def test_healthz_reports_ok(api_client):
    resp = api_client.get(HEALTH_URL)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert isinstance(payload["version"], str)
    assert payload["version"]
    # 全局时间约定（architecture.md 1.2）：时间点必须是 tz-aware 的 UTC。
    assert _parse_dt(payload["time"]).utcoffset() == timedelta(0)


def test_healthz_stays_ok_without_any_available_channel(
    make_context, tmp_settings, manual_clock, monkeypatch
):
    from fastapi.testclient import TestClient

    from notify_hub.api import create_api_app
    from notify_hub.delivery import DeliveryService

    ctx = make_context(tmp_settings, clock=manual_clock, notifiers=[])
    assert ctx.registry.available_ids() == ()

    def _boom(*args, **kwargs):
        raise AssertionError("/healthz 不得探测渠道或调用投递层")

    monkeypatch.setattr(DeliveryService, "deliver", _boom)

    with TestClient(create_api_app(ctx)) as client:
        resp = client.get(HEALTH_URL)

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# --------------------------------------------------------------------------- #
# 第 14 条：凭据隔离（响应体、错误信息、日志）
# --------------------------------------------------------------------------- #
def test_channel_credentials_never_leak(
    make_context, tmp_settings, manual_clock, caplog
):
    from fastapi.testclient import TestClient

    from notify_hub.api import create_api_app
    from notify_hub.notifiers.webhook import WebhookNotifier

    caplog.set_level(logging.DEBUG, logger="notify_hub")

    notifier = WebhookNotifier(
        "hook",
        f"https://example.invalid/hook?access_token={FAKE_TOKEN}",
        transport=_failing_transport(),
        secrets=[FAKE_TOKEN],
    )
    ctx = make_context(tmp_settings, clock=manual_clock, notifiers=[notifier])

    with TestClient(create_api_app(ctx)) as client:
        rejected = client.post(MSG_URL, json={"title": "缺 source"})
        assert rejected.status_code == 422

        accepted = client.post(MSG_URL, json={"source": "s", "title": "t"})
        assert accepted.status_code == 202
        detail = client.get(f"{MSG_URL}/{accepted.json()['message_id']}")
        assert detail.status_code == 200

    assert FAKE_TOKEN not in rejected.text
    assert FAKE_TOKEN not in detail.text
    assert FAKE_TOKEN not in caplog.text

    # 该渠道恒失败（HTTP 500），投递记录被如实反映，且未回显 URL 中的 token。
    record = detail.json()["deliveries"][0]
    assert record["ok"] is False
    assert "500" in (record["error_reason"] or "")
