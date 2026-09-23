"""M6 模块测试之一：``services/messages.py`` 与 ``services/notifications.py`` 的保留部分。

覆盖 ``architecture.md`` 第 6 节「模块 M6 · 4. 验证方法」中的第 1、15 条，外加
``MessageService`` 其余冻结接口（``get`` / ``list`` / ``count`` / ``deliveries`` / ``todo_for``）。

``add-daily-digest`` 移除了第 14 条（单项提醒文案 = ``notification_for_message`` 的
``kind=REMINDER`` 分支），该用例随之删除，见文件末尾「已移除用例」说明；
汇总文案改由 ``notification_for_digest`` 负责，断言在 ``tests/test_digest.py``。

**惰性导入是硬要求**（阶段 A：``notify_hub.services``、``notify_hub.db``、``notify_hub.notifiers``
尚不存在）。本文件顶层只导入标准库与阶段 0 的共享模块，保证 pytest 能成功
**收集**，失败发生在运行时（``ModuleNotFoundError`` = 实现缺失）。

时间一律由 ``conftest.py`` 的 ``manual_clock`` 驱动，**不得真实等待**。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from notify_hub.clock import as_utc
from notify_hub.domain import (
    AckReason,
    ClassificationVerdict,
    DeliveryEvent,
    Level,
)

LOG = logging.getLogger("notify_hub.tests.m6.services")


# --------------------------------------------------------------------------- #
# 惰性构造工具（依赖 M1/M2/M4 的模块在调用时才导入）
# --------------------------------------------------------------------------- #
def _db(tmp_path):
    from notify_hub.db import Database

    database = Database(tmp_path / "m6-services.sqlite3")
    database.init_schema()
    return database


def _messages(db, clock):
    from notify_hub.services.messages import MessageService

    return MessageService(db, clock)


def _todos(db, clock):
    from notify_hub.services.todos import TodoService

    return TodoService(db, clock, logger=LOG)


def _verdict(
    *,
    need_ack: bool = True,
    rule_id: str | None = "backup-failure",
    category: str = "backup-failure",
    labels: tuple[str, ...] = ("infra", "backup"),
    preferred_channel: str | None = None,
) -> ClassificationVerdict:
    return ClassificationVerdict(
        rule_id=rule_id,
        category=category,
        labels=tuple(labels),
        need_ack=need_ack,
        ack_reason=AckReason.RULE if need_ack else AckReason.NONE,
        preferred_channel=preferred_channel,
    )


def _draft(**overrides):
    from notify_hub.services.messages import MessageDraft

    payload = {
        "source": "db-backup",
        "title": "数据库备份失败",
        "body": "exit code 1",
        "level": Level.ERROR,
        "need_ack": True,
        "dedup_key": "bk-2024-05-01",
        "meta": {"host": "db-01", "tags": ["infra"]},
    }
    payload.update(overrides)
    return MessageDraft(**payload)


def _notification(message, *, kind, now, todo=None):
    from notify_hub.services.notifications import notification_for_message

    return notification_for_message(message, kind=kind, now=now, todo=todo)


class _RecordingNotifier:
    """M6 测试的局部投递替身：记录每次 ``send()`` 的入参，返回可配置结果。"""

    def __init__(
        self,
        channel_id: str = "recording",
        *,
        ok: bool = True,
        receipt: str | None = None,
        error_reason: str = "模拟投递失败",
    ) -> None:
        self.channel_id = channel_id
        self.ok = ok
        self.receipt = receipt
        self.error_reason = error_reason
        self.sent = []

    def capabilities(self):
        from notify_hub.notifiers.base import ChannelCapabilities

        return ChannelCapabilities()

    def send(self, msg):
        from notify_hub.notifiers.base import DeliveryResult

        self.sent.append(msg)
        if self.ok:
            return DeliveryResult.success(receipt=self.receipt or f"{self.channel_id}-1")
        return DeliveryResult.failure(self.error_reason)


def _delivery(db, clock, notifier, *, default_channel: str | None = None):
    from notify_hub.delivery import DeliveryService
    from notify_hub.notifiers import NotifierRegistry

    registry = NotifierRegistry()
    registry.register(notifier)
    return DeliveryService(
        db,
        registry,
        default_channel=default_channel,
        channel_order=registry.ids(),
        clock=clock,
        logger=LOG,
    )


def _persisted(messages, **overrides):
    """写入一条消息并返回**重新读回**的行（模拟跨请求读取，时间列因此是 naive 的）。"""
    created = messages.create(_draft(**overrides), _verdict())
    row = messages.get(created.id)
    assert row is not None
    return row


# --------------------------------------------------------------------------- #
# 规格第 4 段第 1 条：消息落库
# --------------------------------------------------------------------------- #
def test_create_persists_draft_and_verdict_fields(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages = _messages(db, manual_clock)
    created = messages.create(_draft(), _verdict())

    assert created.id is not None
    row = messages.get(created.id)
    assert row is not None

    # 原始草稿字段
    assert row.source == "db-backup"
    assert row.title == "数据库备份失败"
    assert row.body == "exit code 1"
    assert row.level == Level.ERROR.value
    assert row.need_ack_declared is True
    assert row.dedup_key == "bk-2024-05-01"
    assert row.meta == {"host": "db-01", "tags": ["infra"]}

    # 分类结论逐字段来自 verdict
    assert row.rule_id == "backup-failure"
    assert row.category == "backup-failure"
    assert list(row.labels) == ["infra", "backup"]
    assert row.needs_ack is True
    assert row.ack_reason == AckReason.RULE.value
    assert row.preferred_channel is None


def test_received_at_follows_clock_and_occurred_at_defaults_to_it(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages = _messages(db, manual_clock)

    manual_clock.advance(120)
    received = manual_clock.now()
    row = _persisted(messages, occurred_at=None)

    assert as_utc(row.received_at) == received
    assert as_utc(row.occurred_at) == as_utc(row.received_at)


def test_explicit_occurred_at_is_preserved(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages = _messages(db, manual_clock)

    occurred = manual_clock.now() - timedelta(hours=2)
    row = _persisted(messages, occurred_at=occurred)

    assert as_utc(row.occurred_at) == occurred
    assert as_utc(row.received_at) == manual_clock.now()


def test_get_unknown_message_returns_none(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages = _messages(db, manual_clock)

    assert messages.get(99999) is None


# --------------------------------------------------------------------------- #
# MessageService.list / count（按 received_at 降序）
# --------------------------------------------------------------------------- #
def test_list_orders_by_received_at_desc_and_filters_by_source(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages = _messages(db, manual_clock)

    first = messages.create(_draft(source="s1", title="a", dedup_key=None), _verdict())
    manual_clock.advance(10)
    second = messages.create(_draft(source="s2", title="b", dedup_key=None), _verdict())
    manual_clock.advance(10)
    third = messages.create(_draft(source="s1", title="c", dedup_key=None), _verdict())

    assert [m.id for m in messages.list()] == [third.id, second.id, first.id]
    assert [m.id for m in messages.list(source="s1")] == [third.id, first.id]
    assert [m.id for m in messages.list(limit=2)] == [third.id, second.id]
    assert [m.id for m in messages.list(limit=2, offset=1)] == [second.id, first.id]

    assert messages.count() == 3
    assert messages.count(source="s1") == 2
    assert messages.count(source="nope") == 0


# --------------------------------------------------------------------------- #
# MessageService.deliveries / todo_for
# --------------------------------------------------------------------------- #
def test_deliveries_are_ordered_by_attempted_at(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages = _messages(db, manual_clock)
    row = _persisted(messages)
    delivery = _delivery(db, manual_clock, _RecordingNotifier())

    assert messages.deliveries(row.id) == []

    first = _notification(row, kind=DeliveryEvent.FIRST_NOTICE, now=manual_clock.now())
    assert delivery.deliver(first, preferred_channel=None, message_id=row.id).ok is True
    manual_clock.advance(30)
    second = _notification(row, kind=DeliveryEvent.FIRST_NOTICE, now=manual_clock.now())
    assert delivery.deliver(second, preferred_channel=None, message_id=row.id).ok is True

    records = messages.deliveries(row.id)
    assert len(records) == 2
    times = [as_utc(r.attempted_at) for r in records]
    assert times == sorted(times)
    assert times[0] < times[1]


def test_todo_for_returns_the_linked_todo(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages = _messages(db, manual_clock)
    todos = _todos(db, manual_clock)

    row = _persisted(messages)
    assert messages.todo_for(row.id) is None

    todo = todos.ensure_for_message(row)
    assert todo is not None
    linked = messages.todo_for(row.id)
    assert linked is not None
    assert linked.id == todo.id


# --------------------------------------------------------------------------- #
# 规格第 4 段第 15 条：format_duration 边界
# --------------------------------------------------------------------------- #
def test_format_duration_boundaries():
    from notify_hub.services.notifications import format_duration

    assert "0 秒" in format_duration(0)
    assert format_duration(59) == "59 秒"
    assert format_duration(60) == "1 分钟"
    assert "1 小时" in format_duration(3661)
    assert "1 分钟" in format_duration(3661)
    assert "1 天" in format_duration(90000)


# --------------------------------------------------------------------------- #
# 已移除用例（add-daily-digest；移除清单含 notification_for_message 的 kind=REMINDER 分支）
#
# - test_reminder_notification_carries_title_overdue_and_body
#     测的是**单项提醒文案**这一已被移除的行为：标题前缀 `[待办超时 <时长>]`、正文里的
#     `已超时:` / `待办 id:` 行、`meta` 里的 todo_id / message_id / reminder_count。
#     汇总改由 notification_for_digest 负责（断言在 tests/test_digest.py 第 14–17 条），
#     旧模型下已无任何调用方以 kind=REMINDER 调用 notification_for_message。
#     这是「移除特性」，不是为了让新代码通过而删测试。
#
# 本次保留（未被移除）：
# - format_duration 边界用例（第 15 条）：汇总文案仍复用它。
# - notification_for_message(kind=FIRST_NOTICE) 用例：首次通知文案不变。
# --------------------------------------------------------------------------- #


def test_first_notice_notification_basics(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages = _messages(db, manual_clock)

    row = _persisted(messages)
    first = _notification(row, kind=DeliveryEvent.FIRST_NOTICE, now=manual_clock.now())

    assert first.kind is DeliveryEvent.FIRST_NOTICE
    assert first.overdue_seconds is None
    assert first.level == Level.ERROR
    assert first.source == row.source
    assert row.title in first.title
