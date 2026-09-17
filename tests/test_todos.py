"""M6 模块测试之二：``services/todos.py`` 与 ``services/scheduler.py``。

覆盖 ``architecture.md`` 第 6 节「模块 M6 · 4. 验证方法」中的第 2–13、16、17 条。

**惰性导入是硬要求**（阶段 A：``notify_hub.services``、``notify_hub.db``、``notify_hub.notifiers``
尚不存在）。本文件顶层只导入标准库、pytest 与阶段 0 的共享模块，保证 pytest 能成功
**收集**，失败发生在运行时（``ModuleNotFoundError`` = 实现缺失）。

时间一律由 ``conftest.py`` 的 ``manual_clock`` 驱动，**禁止任何真实等待**（架构硬要求：
这两个测试文件中不得出现真实的休眠调用）。
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from notify_hub.clock import as_utc
from notify_hub.domain import (
    AckReason,
    ClassificationVerdict,
    Level,
    TodoEventKind,
    TodoStatus,
)

LOG = logging.getLogger("notify_hub.tests.m6.todos")


# --------------------------------------------------------------------------- #
# 惰性构造工具（M1/M2/M4 在调用时才导入）
# --------------------------------------------------------------------------- #
def _db(tmp_path):
    from notify_hub.db import Database

    database = Database(tmp_path / "m6-todos.sqlite3")
    database.init_schema()
    return database


def _services(db, clock):
    from notify_hub.services.messages import MessageService
    from notify_hub.services.todos import TodoService

    return MessageService(db, clock), TodoService(db, clock, logger=LOG)


def _models():
    from notify_hub.models import DeliveryRecord, Todo, TodoEvent

    return Todo, TodoEvent, DeliveryRecord


def _rows(db, model):
    """只读地取出某张表的全部行。

    用独立的只读 ``Session``（不 commit），这样返回的行在 session 关闭后仍可安全读取属性；
    若复用 ``Database.session()``，正常退出时的 commit 会把属性标记为过期，离开 session 后
    访问会抛 ``DetachedInstanceError``。
    """
    from sqlmodel import Session, select

    with Session(db.engine) as session:
        return list(session.exec(select(model)).all())


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
        "dedup_key": None,
        "meta": {"host": "db-01"},
    }
    payload.update(overrides)
    return MessageDraft(**payload)


def _ack_message(
    messages,
    *,
    source: str = "db-backup",
    dedup_key: str | None = None,
    title: str = "数据库备份失败",
    need_ack: bool = True,
    preferred_channel: str | None = None,
):
    """写入一条消息并返回持久化后的行。"""
    created = messages.create(
        _draft(source=source, dedup_key=dedup_key, title=title, need_ack=need_ack),
        _verdict(need_ack=need_ack, preferred_channel=preferred_channel),
    )
    row = messages.get(created.id)
    assert row is not None
    return row


def _ensure_todo_at(messages, todos, clock, moment, *, source: str, **kwargs):
    """把时钟设到 ``moment``，写入消息并生成待办——用于构造不同的 first_notified_at。"""
    clock.set(moment)
    return todos.ensure_for_message(_ack_message(messages, source=source, **kwargs))


def _reminder_settings(*, scan: float = 1.0, first: float = 60.0, interval: float = 120.0):
    from notify_hub.config import ReminderSettings

    return ReminderSettings(
        scan_interval_seconds=scan,
        first_reminder_after_seconds=first,
        reminder_interval_seconds=interval,
    )


class _RecordingNotifier:
    """M6 测试的局部投递替身：记录每次 ``send()`` 的入参，返回可配置结果。"""

    def __init__(
        self,
        channel_id: str = "recording",
        *,
        ok: bool = True,
        receipt: str | None = None,
        error_reason: str = "渠道不可用",
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


class _ExplodingDelivery:
    """包装真实 ``DeliveryService``：第一次 ``deliver()`` 抛异常，之后委派。

    规格第 4 段第 17 条要求「单条待办异常不得中断整轮」。M4 的 ``deliver()`` 按契约不抛异常，
    所以这里用投递替身在**边界上**注入一次真实异常，验证 scheduler 的隔离逻辑。
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    def deliver(self, msg, *, preferred_channel=None, message_id=None, todo_id=None):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("模拟投递期间的意外异常")
        return self._inner.deliver(
            msg,
            preferred_channel=preferred_channel,
            message_id=message_id,
            todo_id=todo_id,
        )


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


def _scheduler(todos, delivery, clock, settings):
    from notify_hub.services.scheduler import ReminderScheduler

    return ReminderScheduler(
        todos=todos, delivery=delivery, clock=clock, settings=settings, logger=LOG
    )


def _reminder_events(db, todo_id: int):
    _, TodoEvent, _ = _models()
    return [
        event
        for event in _rows(db, TodoEvent)
        if event.todo_id == todo_id and event.kind == TodoEventKind.REMINDER.value
    ]


# --------------------------------------------------------------------------- #
# 规格第 4 段第 2 条：待办生成
# --------------------------------------------------------------------------- #
def test_ensure_for_message_creates_pending_todo(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)
    Todo, TodoEvent, _ = _models()

    message = _ack_message(messages)
    created_at = manual_clock.now()
    todo = todos.ensure_for_message(message)

    assert todo is not None
    assert todo.message_id == message.id
    assert todo.status == TodoStatus.PENDING
    assert todo.source == "db-backup"
    assert todo.title == "数据库备份失败"
    assert todo.category == "backup-failure"
    assert todo.ack_reason == AckReason.RULE.value
    assert as_utc(todo.first_notified_at) == created_at
    assert as_utc(todo.last_notified_at) == created_at
    assert todo.reminder_count == 0
    assert todo.completed_at is None

    # 该消息只有 1 条待办，且只写了 1 条 CREATED 事件
    todo_rows = _rows(db, Todo)
    assert len(todo_rows) == 1
    assert todo_rows[0].message_id == message.id
    events = _rows(db, TodoEvent)
    assert len(events) == 1
    assert events[0].kind == TodoEventKind.CREATED.value
    assert events[0].todo_id == todo.id


# --------------------------------------------------------------------------- #
# 规格第 4 段第 3 条：不需要待办
# --------------------------------------------------------------------------- #
def test_ensure_for_message_returns_none_when_not_needed(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)
    Todo, _, _ = _models()

    message = _ack_message(messages, need_ack=False)

    assert todos.ensure_for_message(message) is None
    assert _rows(db, Todo) == []


# --------------------------------------------------------------------------- #
# 规格第 4 段第 4 条：去重
# --------------------------------------------------------------------------- #
def test_ensure_for_message_dedups_same_source_and_key(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)
    Todo, TodoEvent, _ = _models()

    first_message = _ack_message(messages, dedup_key="bk-1")
    first = todos.ensure_for_message(first_message)
    assert first is not None

    manual_clock.advance(60)
    second_message = _ack_message(messages, dedup_key="bk-1", title="重复投递")
    second = todos.ensure_for_message(second_message)

    assert second is not None
    assert second.id == first.id
    assert len(_rows(db, Todo)) == 1
    created = [e for e in _rows(db, TodoEvent) if e.kind == TodoEventKind.CREATED.value]
    assert len(created) == 1


# --------------------------------------------------------------------------- #
# 规格第 4 段第 5 条：dedup_key 为空不去重
# --------------------------------------------------------------------------- #
def test_no_dedup_when_key_is_empty(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)
    Todo, _, _ = _models()

    first = todos.ensure_for_message(_ack_message(messages, dedup_key=None))
    second = todos.ensure_for_message(_ack_message(messages, dedup_key=None))

    assert first is not None and second is not None
    assert first.id != second.id
    assert len(_rows(db, Todo)) == 2


# --------------------------------------------------------------------------- #
# 规格第 4 段第 6 条：完成后可再建
# --------------------------------------------------------------------------- #
def test_new_todo_allowed_after_completion(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)
    Todo, _, _ = _models()

    first = todos.ensure_for_message(_ack_message(messages, dedup_key="bk-1"))
    assert first is not None
    assert todos.complete(first.id).status == "completed"

    manual_clock.advance(10)
    second = todos.ensure_for_message(
        _ack_message(messages, dedup_key="bk-1", title="第二次失败")
    )

    assert second is not None
    assert second.id != first.id
    assert second.status == TodoStatus.PENDING
    assert len(_rows(db, Todo)) == 2


# --------------------------------------------------------------------------- #
# 规格第 4 段第 7 条：完成幂等 + 不存在的 id（异常场景 a）
# --------------------------------------------------------------------------- #
def test_complete_is_idempotent_and_reports_not_found(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)

    todo = todos.ensure_for_message(_ack_message(messages))
    assert todo is not None

    first = todos.complete(todo.id)
    assert first.status == "completed"
    assert first.todo_id == todo.id
    assert first.completed_at is not None
    assert as_utc(first.completed_at) == manual_clock.now()
    assert todos.get(todo.id).status == TodoStatus.DONE
    first_completed_at = as_utc(first.completed_at)

    manual_clock.advance(600)
    again = todos.complete(todo.id)
    assert again.status == "already_completed"
    assert again.completed_at is not None
    assert as_utc(again.completed_at) == first_completed_at
    # 已完成不得回到待完成
    assert todos.get(todo.id).status == TodoStatus.DONE

    missing = todos.complete(99999)
    assert missing.status == "not_found"
    assert missing.todo_id == 99999
    assert missing.completed_at is None


# --------------------------------------------------------------------------- #
# 规格第 4 段第 8 条：列表排序与过滤
# --------------------------------------------------------------------------- #
def test_list_orders_by_overdue_desc_and_filters_by_status(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)

    base = manual_clock.now()
    oldest = _ensure_todo_at(
        messages, todos, manual_clock, base - timedelta(hours=5), source="s-5h"
    )
    middle = _ensure_todo_at(
        messages, todos, manual_clock, base - timedelta(hours=1), source="s-1h"
    )
    newest = _ensure_todo_at(
        messages, todos, manual_clock, base - timedelta(minutes=20), source="s-20m"
    )
    manual_clock.set(base)

    views = todos.list()
    assert [v.id for v in views] == [oldest.id, middle.id, newest.id]
    assert all(v.status == TodoStatus.PENDING for v in views)
    assert abs(views[0].overdue_seconds - 18000) < 1
    assert abs(views[1].overdue_seconds - 3600) < 1
    assert abs(views[2].overdue_seconds - 1200) < 1

    # 默认（pending）列表不含已完成；done 列表只含已完成
    assert todos.complete(middle.id).status == "completed"
    assert [v.id for v in todos.list()] == [oldest.id, newest.id]
    done = todos.list(status=TodoStatus.DONE)
    assert [v.id for v in done] == [middle.id]
    assert all(v.status == TodoStatus.DONE for v in done)


# --------------------------------------------------------------------------- #
# 规格第 4 段第 9 条：详情（事件时间序列 + 投递记录）
# --------------------------------------------------------------------------- #
def test_detail_contains_event_timeline_and_deliveries(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)
    settings = _reminder_settings(first=60, interval=120, scan=1)
    delivery = _delivery(db, manual_clock, _RecordingNotifier())
    scheduler = _scheduler(todos, delivery, manual_clock, settings)

    todo = todos.ensure_for_message(_ack_message(messages))
    assert todo is not None

    manual_clock.advance(61)
    assert scheduler.run_once() == 1
    manual_clock.advance(120)
    assert scheduler.run_once() == 1
    manual_clock.advance(5)
    assert todos.complete(todo.id).status == "completed"

    detail = todos.detail(todo.id)
    assert detail is not None
    assert detail.todo.id == todo.id
    assert detail.message.id == todo.message_id
    assert [e.kind for e in detail.events] == [
        TodoEventKind.CREATED.value,
        TodoEventKind.REMINDER.value,
        TodoEventKind.REMINDER.value,
        "completed",
    ]
    event_times = [as_utc(e.occurred_at) for e in detail.events]
    assert event_times == sorted(event_times)

    assert len(detail.deliveries) == 2
    assert all(record.todo_id == todo.id for record in detail.deliveries)
    delivery_times = [as_utc(record.attempted_at) for record in detail.deliveries]
    assert delivery_times == sorted(delivery_times)

    assert todos.detail(99999) is None


# --------------------------------------------------------------------------- #
# 规格第 4 段第 10 条：超时判定门槛
# --------------------------------------------------------------------------- #
def test_due_for_reminder_respects_first_threshold(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)
    settings = _reminder_settings(first=60, interval=120, scan=1)

    todo = todos.ensure_for_message(_ack_message(messages))
    assert todo is not None

    manual_clock.advance(59)
    assert todos.due_for_reminder(settings=settings) == []

    manual_clock.advance(2)  # 累计 61 秒 >= 60
    due = todos.due_for_reminder(settings=settings)
    assert [t.id for t in due] == [todo.id]


# --------------------------------------------------------------------------- #
# 规格第 4 段第 11 条：提醒与节奏
# --------------------------------------------------------------------------- #
def test_run_once_reminds_then_waits_for_the_interval(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)
    _, TodoEvent, _ = _models()
    settings = _reminder_settings(first=60, interval=120, scan=1)
    notifier = _RecordingNotifier()
    delivery = _delivery(db, manual_clock, notifier)
    scheduler = _scheduler(todos, delivery, manual_clock, settings)

    todo = todos.ensure_for_message(_ack_message(messages))
    assert todo is not None

    manual_clock.advance(61)
    reminded_at = manual_clock.now()
    assert scheduler.run_once() == 1

    assert len(notifier.sent) == 1
    assert notifier.sent[0].overdue_seconds == pytest.approx(61.0)
    current = todos.get(todo.id)
    assert as_utc(current.last_notified_at) == reminded_at
    assert current.reminder_count == 1
    assert len(_reminder_events(db, todo.id)) == 1

    # 未到间隔不打扰
    manual_clock.advance(119)
    assert scheduler.run_once() == 0
    assert todos.get(todo.id).reminder_count == 1
    assert len(_reminder_events(db, todo.id)) == 1

    manual_clock.advance(2)  # 累计距上次通知 121 秒 >= 120
    assert scheduler.run_once() == 1
    assert todos.get(todo.id).reminder_count == 2
    assert len(_reminder_events(db, todo.id)) == 2


# --------------------------------------------------------------------------- #
# 规格第 4 段第 12 条：完成后不再提醒
# --------------------------------------------------------------------------- #
def test_completed_todo_is_never_reminded(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)
    _, TodoEvent, _ = _models()
    settings = _reminder_settings(first=60, interval=120, scan=1)
    notifier = _RecordingNotifier()
    delivery = _delivery(db, manual_clock, notifier)
    scheduler = _scheduler(todos, delivery, manual_clock, settings)

    todo = todos.ensure_for_message(_ack_message(messages))
    assert todo is not None

    manual_clock.advance(30)  # 门槛（60）之前完成
    assert todos.complete(todo.id).status == "completed"

    manual_clock.advance(10000)
    assert scheduler.run_once() == 0
    assert todos.due_for_reminder(settings=settings) == []
    kinds = [e.kind for e in _rows(db, TodoEvent) if e.todo_id == todo.id]
    assert TodoEventKind.REMINDER.value not in kinds
    assert notifier.sent == []


# --------------------------------------------------------------------------- #
# 规格第 4 段第 13 条：提醒失败仍按间隔重试
# --------------------------------------------------------------------------- #
def test_failed_reminder_retries_on_next_cycle(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)
    _, _, DeliveryRecord = _models()
    settings = _reminder_settings(first=60, interval=120, scan=1)
    notifier = _RecordingNotifier(ok=False, error_reason="渠道不可用")
    delivery = _delivery(db, manual_clock, notifier)
    scheduler = _scheduler(todos, delivery, manual_clock, settings)

    todo = todos.ensure_for_message(_ack_message(messages))
    assert todo is not None

    manual_clock.advance(61)
    failed_at = manual_clock.now()
    assert scheduler.run_once() == 1

    current = todos.get(todo.id)
    assert current.status == TodoStatus.PENDING
    assert as_utc(current.last_notified_at) == failed_at  # 与投递成败无关
    assert current.reminder_count == 1

    records = [r for r in _rows(db, DeliveryRecord) if r.todo_id == todo.id]
    assert len(records) == 1
    assert records[0].ok is False
    assert records[0].error_reason

    manual_clock.advance(120)
    assert scheduler.run_once() == 1
    assert todos.get(todo.id).reminder_count == 2
    assert len(_reminder_events(db, todo.id)) == 2
    assert todos.get(todo.id).status == TodoStatus.PENDING


# --------------------------------------------------------------------------- #
# 规格第 4 段第 16 条：同一轮内至多提醒一次
# --------------------------------------------------------------------------- #
def test_run_once_reminds_at_most_once_per_round(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)
    settings = _reminder_settings(first=60, interval=120, scan=1)
    delivery = _delivery(db, manual_clock, _RecordingNotifier())
    scheduler = _scheduler(todos, delivery, manual_clock, settings)

    todo = todos.ensure_for_message(_ack_message(messages))
    assert todo is not None

    manual_clock.advance(3600)  # 远远超过多个间隔，不得成批补发
    assert scheduler.run_once() == 1
    assert todos.get(todo.id).reminder_count == 1
    assert len(_reminder_events(db, todo.id)) == 1


# --------------------------------------------------------------------------- #
# 规格第 4 段第 17 条：单条异常不中断整轮（异常场景 b）
# --------------------------------------------------------------------------- #
def test_single_todo_failure_does_not_abort_the_round(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)
    _, TodoEvent, _ = _models()
    settings = _reminder_settings(first=60, interval=120, scan=1)
    inner = _delivery(db, manual_clock, _RecordingNotifier())
    exploding = _ExplodingDelivery(inner)
    scheduler = _scheduler(todos, exploding, manual_clock, settings)

    first = todos.ensure_for_message(_ack_message(messages, source="s-first", title="第一条"))
    second = todos.ensure_for_message(_ack_message(messages, source="s-second", title="第二条"))
    assert first is not None and second is not None

    manual_clock.advance(61)
    assert scheduler.run_once() == 1  # 不抛异常，且另一条仍被提醒

    reminders = [e for e in _rows(db, TodoEvent) if e.kind == TodoEventKind.REMINDER.value]
    assert len(reminders) == 1
    counts = sorted(todos.get(t.id).reminder_count for t in (first, second))
    assert counts == [0, 1]
    for todo in (first, second):
        assert todos.get(todo.id).status == TodoStatus.PENDING
