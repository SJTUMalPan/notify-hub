"""M6 模块测试之二：``services/todos.py`` **保留下来**的接口。

``add-daily-digest`` 把「单项超时间隔提醒」整体替换为「每日汇总」，因此本文件只保留
仍然成立的 M6 行为：待办生成/去重、完成幂等、列表排序、详情时间序列、逐条记账。
被移除的单项提醒用例集中在文件末尾的「已移除用例」注释里（逐条写明它测的是哪个已移除行为，
便于评审者区分「移除特性」与「为过测试而删测试」）。

每日汇总的调度语义（``ReminderScheduler.run_once`` / ``DigestService``）在
``tests/test_digest.py``；本文件不再构造 ``ReminderScheduler``。

**惰性导入是硬要求**（阶段 A：``notify_hub.services``、``notify_hub.db``、``notify_hub.notifiers``
尚不存在）。本文件顶层只导入标准库与阶段 0 的共享模块，保证 pytest 能成功**收集**，
失败发生在运行时（``ModuleNotFoundError`` = 实现缺失）。

时间一律由 ``conftest.py`` 的 ``manual_clock`` 驱动，**禁止任何真实等待**。
"""

from __future__ import annotations

import logging
from datetime import timedelta

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
# 规格第 4 段第 9 条：详情（事件时间序列；汇总投递不归属于单条待办）
# --------------------------------------------------------------------------- #
def test_detail_contains_event_timeline_and_unattributed_digest_delivery(
    tmp_path, manual_clock
):
    """``TodoService.detail`` 仍然成立的部分。

    改写说明：原用例用「单项提醒调度」制造 2 条挂在待办上的投递记录。``add-daily-digest``
    之后不再有「按待办投递」，汇总的投递记录 ``message_id=None`` / ``todo_id=None``
    （架构 3.3 决策 5），因此断言改为「时间序列仍完整」+「汇总记录不出现在待办详情里」，
    而不是删掉这条覆盖。
    """
    from notify_hub.services.notifications import notification_for_digest

    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)
    _, _, DeliveryRecord = _models()
    delivery = _delivery(db, manual_clock, _RecordingNotifier(), default_channel="recording")

    todo = todos.ensure_for_message(_ack_message(messages))
    assert todo is not None

    manual_clock.advance(3600)
    todos.record_reminder(todo.id, delivered=True, channel_id="recording")
    manual_clock.advance(120)
    todos.record_reminder(todo.id, delivered=True, channel_id="recording")

    # 一次真实的汇总投递：该记录不归属任何单条待办
    digest_message = notification_for_digest(todos.list(), now=manual_clock.now())
    outcome = delivery.deliver(
        digest_message, preferred_channel=None, message_id=None, todo_id=None
    )
    assert outcome.ok is True

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
        TodoEventKind.COMPLETED.value,
    ]
    event_times = [as_utc(e.occurred_at) for e in detail.events]
    assert event_times == sorted(event_times)

    # 汇总那条记录确实存在，但不按 todo_id 归属，故不出现在待办详情里
    assert detail.deliveries == ()
    all_records = _rows(db, DeliveryRecord)
    assert len(all_records) == 1
    assert all_records[0].todo_id is None
    assert all_records[0].message_id is None

    assert todos.detail(99999) is None


# --------------------------------------------------------------------------- #
# 逐条记账（保留接口 ``record_reminder``；原先只被单项提醒用例间接覆盖）
# --------------------------------------------------------------------------- #
def test_record_reminder_updates_time_count_and_writes_event(tmp_path, manual_clock):
    db = _db(tmp_path)
    messages, todos = _services(db, manual_clock)

    todo = todos.ensure_for_message(_ack_message(messages))
    assert todo is not None

    manual_clock.advance(90)
    moment = manual_clock.now()
    todos.record_reminder(todo.id, delivered=True, channel_id="recording")

    current = todos.get(todo.id)
    assert current.reminder_count == 1
    assert as_utc(current.last_notified_at) == moment
    assert current.status == TodoStatus.PENDING

    events = _reminder_events(db, todo.id)
    assert len(events) == 1
    assert events[0].channel_id == "recording"
    assert events[0].delivery_ok is True
    assert as_utc(events[0].occurred_at) == moment

    # 再次记账是累加（不再有「距上次通知 ≥ 间隔」的门槛判定）
    manual_clock.advance(10)
    later = manual_clock.now()
    todos.record_reminder(todo.id, delivered=False, channel_id="recording")
    assert todos.get(todo.id).reminder_count == 2
    assert as_utc(todos.get(todo.id).last_notified_at) == later
    assert len(_reminder_events(db, todo.id)) == 2


# --------------------------------------------------------------------------- #
# 已移除用例（add-daily-digest；规格 REMOVED「超时判定与重复提醒」）
#
# 下列用例随「单项超时间隔提醒」特性一并移除。逐条登记它测的是哪个被移除的行为，
# 便于评审者区分「移除特性」与「为让新代码通过而删测试」：
#
# - test_due_for_reminder_respects_first_threshold
#     测 ``TodoService.due_for_reminder`` 的「首次提醒门槛」
#     （``reminders.first_reminder_after_seconds``）；该方法与配置键已被架构 3.2 / 3.4
#     第 26 条明确删除。
# - test_run_once_reminds_then_waits_for_the_interval
#     测单项提醒的「距上次通知 ≥ ``reminder_interval_seconds``」节奏；规格 REMOVED 明确
#     「不再有『距上次通知 ≥ 间隔』的判定」。
# - test_completed_todo_is_never_reminded
#     以 ``due_for_reminder`` 为断言入口测「完成后不再提醒」；该性质在新模型下由
#     ``tests/test_digest.py`` 第 21 条（次日汇总只覆盖仍未完成者）覆盖。
# - test_failed_reminder_retries_on_next_cycle
#     测单项提醒失败后按间隔重试；新语义是「同一自然日内重试、跨天不补发」，
#     由 ``tests/test_digest.py`` 第 22 / 23 条覆盖。
# - test_run_once_reminds_at_most_once_per_round
#     测单项提醒「一轮至多一次」；新模型每天只发一条汇总，
#     由 ``tests/test_digest.py`` 第 18 条覆盖。
# - test_single_todo_failure_does_not_abort_the_round
#     测单项投递异常不中断整轮；新模型的隔离点是「单条待办记账失败」，
#     由 ``tests/test_digest.py`` 第 25 条覆盖。
#
# 同时删除的死代码：``_reminder_settings``（旧 ReminderSettings 三键）、``_scheduler``
# （旧 ReminderScheduler 签名）、``_ExplodingDelivery``（只服务于上面最后一条用例）。
# --------------------------------------------------------------------------- #



