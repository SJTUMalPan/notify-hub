"""M11 模块测试：每日汇总状态服务（``services/digest.py``）与调度语义（``ReminderScheduler.run_once``）。

覆盖 ``openspec/changes/add-daily-digest/architecture.md`` 第 3.4 节第 10–27 条。

**惰性导入是硬要求**（阶段 A：``notify_hub.services.digest`` 尚不存在）。本文件顶层只导入
标准库、pytest 与**已存在**的共享模块（``notify_hub.clock`` / ``notify_hub.domain``），
保证 pytest 能成功**收集**；失败一律发生在运行时（``ModuleNotFoundError`` = 实现缺失）。

**禁止真实等待**：时间一律由 ``conftest.py`` 的 ``manual_clock``（``ManualClock``）驱动，
本文件不含任何休眠调用。

**关于时区**：本环境没有 IANA tz 数据库（``zoneinfo.ZoneInfo('Asia/Shanghai')`` 抛
``ZoneInfoNotFoundError``，``/usr/share/zoneinfo`` 不存在）。为了让「红」只表示实现缺失、
而不混入测试脚手架的失败，本文件**自己**用固定 ``+08:00`` 构造「Asia/Shanghai 的墙上时刻」
（该时区无夏令时，等价）。被测实现仍须按冻结规格用 ``settings.zone`` 做换算。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from notify_hub.clock import as_utc
from notify_hub.domain import (
    AckReason,
    ClassificationVerdict,
    DeliveryEvent,
    Level,
    TodoEventKind,
)

LOG = logging.getLogger("notify_hub.tests.m11.digest")

#: Asia/Shanghai 无夏令时，测试用固定偏移构造本地时刻（见模块 docstring）。
CST = timezone(timedelta(hours=8))
DAY1 = date(2024, 1, 1)
DAY2 = date(2024, 1, 2)


def _local(day: int, hour: int, minute: int) -> datetime:
    """2024-01-<day> <hour>:<minute>（Asia/Shanghai 墙上时间），返回 tz-aware 时刻。"""
    return datetime(2024, 1, day, hour, minute, tzinfo=CST)


# --------------------------------------------------------------------------- #
# 惰性构造工具
# --------------------------------------------------------------------------- #
def _rows(db, model):
    """只读地取出某张表的全部行（不 commit，避免 ``DetachedInstanceError``）。"""
    from sqlmodel import Session, select

    with Session(db.engine) as session:
        return list(session.exec(select(model)).all())


def _digest_rows(db):
    from notify_hub.models import DigestRun

    return _rows(db, DigestRun)


def _delivery_rows(db):
    from notify_hub.models import DeliveryRecord

    return _rows(db, DeliveryRecord)


def _reminder_events(db, todo_id: int):
    from notify_hub.models import TodoEvent

    return [
        event
        for event in _rows(db, TodoEvent)
        if event.todo_id == todo_id and event.kind == TodoEventKind.REMINDER.value
    ]


def _settings():
    from notify_hub.config import ReminderSettings

    return ReminderSettings(
        at="21:00", timezone="Asia/Shanghai", scan_interval_seconds=60.0
    )


def _scheduler(*, todos, delivery, digest, clock, settings):
    from notify_hub.services.scheduler import ReminderScheduler

    return ReminderScheduler(
        todos=todos,
        delivery=delivery,
        digest=digest,
        clock=clock,
        settings=settings,
        logger=LOG,
    )


class _RecordingNotifier:
    """局部投递替身：记录 ``send()`` 入参，``ok`` 可运行时翻转（用于失败后重试）。"""

    def __init__(
        self, channel_id: str, *, ok: bool = True, error_reason: str = "模拟渠道不可用"
    ) -> None:
        self.channel_id = channel_id
        self.ok = ok
        self.error_reason = error_reason
        self.sent = []

    def capabilities(self):
        from notify_hub.notifiers.base import ChannelCapabilities

        return ChannelCapabilities()

    def send(self, msg):
        from notify_hub.notifiers.base import DeliveryResult

        self.sent.append(msg)
        if self.ok:
            return DeliveryResult.success(receipt=f"{self.channel_id}-1")
        return DeliveryResult.failure(self.error_reason)


class _ExplodingAccounting:
    """包装 ``TodoService``：对指定待办的 ``record_reminder`` 抛异常，其余调用原样委派。

    规格第 25 条要求「单条待办记账失败不得中断整轮」。
    """

    def __init__(self, inner, *, explode_id: int) -> None:
        self._inner = inner
        self._explode_id = explode_id
        self.calls: list[int] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def record_reminder(self, todo_id, *, delivered, channel_id):
        self.calls.append(todo_id)
        if todo_id == self._explode_id:
            raise RuntimeError("模拟记账失败")
        return self._inner.record_reminder(
            todo_id, delivered=delivered, channel_id=channel_id
        )


def _rig(
    tmp_path,
    clock,
    *,
    default_ok: bool = True,
    fallback_channel: bool = True,
    fallback_ok: bool = True,
):
    """自带 Settings / Database / 服务 / 调度器的一套环境（不使用 ``tmp_settings`` 等 fixture）。

    库文件名固定，因此对同一 ``tmp_path`` 再调一次 = 模拟进程重启（复用同一库文件）。

    ``fallback_channel=False`` 时只注册默认渠道——用于构造「**所有**候选渠道都失败」的
    投递失败前提：只要还有任何一个可用渠道成功，``DeliveryService`` 的降级链会把本轮算作
    已送达（``outcome.ok`` 为真、不重试）。
    """
    from notify_hub.db import Database
    from notify_hub.delivery import DeliveryService
    from notify_hub.notifiers import NotifierRegistry
    from notify_hub.services.digest import DigestService
    from notify_hub.services.messages import MessageService
    from notify_hub.services.todos import TodoService

    db = Database(tmp_path / "digest.sqlite3")
    db.init_schema()

    # 两个渠道：默认渠道 + 一条「各待办的首选渠道」。决策 4 要求汇总走默认渠道。
    default = _RecordingNotifier("default-rec", ok=default_ok)
    registry = NotifierRegistry()
    registry.register(default)
    preferred = None
    if fallback_channel:
        preferred = _RecordingNotifier("preferred-other", ok=fallback_ok)
        registry.register(preferred)

    messages = MessageService(db, clock)
    todos = TodoService(db, clock, logger=LOG)
    digest = DigestService(db, clock)
    delivery = DeliveryService(
        db,
        registry,
        default_channel="default-rec",
        channel_order=registry.ids(),
        clock=clock,
        logger=LOG,
    )
    settings = _settings()
    return SimpleNamespace(
        db=db,
        messages=messages,
        todos=todos,
        digest=digest,
        delivery=delivery,
        settings=settings,
        default=default,
        preferred=preferred,
        scheduler=_scheduler(
            todos=todos,
            delivery=delivery,
            digest=digest,
            clock=clock,
            settings=settings,
        ),
    )


def _verdict(*, need_ack: bool = True, preferred_channel: str | None = None):
    return ClassificationVerdict(
        rule_id="backup-failure",
        category="backup-failure",
        labels=("infra", "backup"),
        need_ack=need_ack,
        ack_reason=AckReason.RULE if need_ack else AckReason.NONE,
        preferred_channel=preferred_channel,
    )


def _draft(*, source: str, title: str, dedup_key: str | None = None):
    from notify_hub.services.messages import MessageDraft

    return MessageDraft(
        source=source,
        title=title,
        body="exit code 1",
        level=Level.ERROR,
        need_ack=True,
        dedup_key=dedup_key,
        meta={},
    )


def _ack_message(
    messages, *, source: str, title: str, dedup_key: str | None = None,
    preferred_channel: str | None = None,
):
    created = messages.create(
        _draft(source=source, title=title, dedup_key=dedup_key),
        _verdict(preferred_channel=preferred_channel),
    )
    row = messages.get(created.id)
    assert row is not None
    return row


def _todo_at(env, clock, moment, *, source: str, title: str, preferred_channel=None):
    """把时钟设到 ``moment``，写入消息并生成待办（用于构造不同的 first_notified_at）。"""
    clock.set(moment)
    todo = env.todos.ensure_for_message(
        _ack_message(
            env.messages,
            source=source,
            title=title,
            preferred_channel=preferred_channel,
        )
    )
    assert todo is not None
    return todo


# --------------------------------------------------------------------------- #
# 第 10–12 条：DigestService 的状态语义
# --------------------------------------------------------------------------- #
def test_state_for_missing_and_future_date_returns_none(tmp_path, manual_clock):
    """第 10 条：未记录的日期（含未来日期）→ ``None``。"""
    env = _rig(tmp_path, manual_clock)

    assert env.digest.state_for(DAY1) is None
    assert env.digest.state_for(date(2030, 1, 1)) is None
    assert _digest_rows(env.db) == []


def test_record_first_and_repeat_calls(tmp_path, manual_clock):
    """第 11 条：首次 ``attempts == 1`` 且 ``checked_at == clock.now()``；
    重复调用 ``attempts += 1``、``checked_at`` 保持不变、其余字段以本次为准。
    """
    env = _rig(tmp_path, manual_clock)
    first_checked_at = manual_clock.now()

    first = env.digest.record(DAY1, todo_count=0, delivered=True)
    assert first.local_date == DAY1
    assert first.attempts == 1
    assert as_utc(first.checked_at) == first_checked_at
    assert first.fired_at is None
    assert first.todo_count == 0
    assert first.delivered is True
    assert first.last_error is None

    manual_clock.advance(120)
    second = env.digest.record(DAY1, todo_count=2, delivered=False, error="渠道不可用")
    assert second.attempts == 2
    assert as_utc(second.checked_at) == first_checked_at  # 重复调用不得改写定案时刻
    assert second.local_date == DAY1
    assert second.todo_count == 2
    assert second.delivered is False
    assert second.last_error == "渠道不可用"

    manual_clock.advance(60)
    fired_moment = manual_clock.now()
    third = env.digest.record(DAY1, todo_count=2, delivered=True, fired_at=fired_moment)
    assert third.attempts == 3
    assert as_utc(third.checked_at) == first_checked_at
    assert as_utc(third.fired_at) == fired_moment
    assert third.delivered is True

    # 按 local_date 幂等：始终只有一行，且回读与返回值一致
    assert len(_digest_rows(env.db)) == 1
    stored = env.digest.state_for(DAY1)
    assert stored is not None
    assert stored.attempts == 3
    assert stored.todo_count == 2
    assert as_utc(stored.checked_at) == first_checked_at


def test_record_with_none_error_does_not_raise(tmp_path, manual_clock):
    """第 12 条：``error=None`` 不得抛异常；``last_error`` 保持原值或为 None 均可。"""
    env = _rig(tmp_path, manual_clock)

    fresh = env.digest.record(DAY1, todo_count=1, delivered=False)
    assert fresh.last_error is None
    assert fresh.attempts == 1

    with_error = env.digest.record(DAY1, todo_count=1, delivered=False, error="boom")
    assert with_error.last_error == "boom"

    cleared = env.digest.record(
        DAY1, todo_count=1, delivered=True, fired_at=manual_clock.now()
    )
    assert cleared.last_error in (None, "boom")  # 两种读法都接受，但不得抛异常
    assert cleared.attempts == 3


# --------------------------------------------------------------------------- #
# 第 13–25 条：ReminderScheduler.run_once 的调度语义（全部用 ManualClock）
# --------------------------------------------------------------------------- #
def test_before_trigger_time_does_not_fire(tmp_path, manual_clock):
    """第 13 条：20:59 不发、无记录、无投递。"""
    env = _rig(tmp_path, manual_clock)
    todo = _todo_at(env, manual_clock, _local(1, 9, 0), source="s-13", title="早间失败")

    manual_clock.set(_local(1, 20, 59))
    assert env.scheduler.run_once() == 0

    assert _digest_rows(env.db) == []
    assert _delivery_rows(env.db) == []
    assert env.default.sent == []
    assert env.todos.get(todo.id).reminder_count == 0


def test_at_trigger_time_sends_one_digest_via_default_channel(tmp_path, manual_clock):
    """第 14 条：21:00 发一条；投递记录恰好 1 条且 ``message_id`` / ``todo_id`` 为 None、
    渠道为 ``default_channel``（决策 4：即使待办声明了别的首选渠道）。
    """
    env = _rig(tmp_path, manual_clock)
    _todo_at(
        env, manual_clock, _local(1, 9, 0), source="s-14a", title="待办A",
        preferred_channel="preferred-other",
    )
    _todo_at(
        env, manual_clock, _local(1, 9, 5), source="s-14b", title="待办B",
        preferred_channel="preferred-other",
    )

    manual_clock.set(_local(1, 21, 0))
    now = manual_clock.now()
    assert env.scheduler.run_once() == 1

    assert len(env.default.sent) == 1
    assert env.preferred.sent == []  # 汇总不按各待办的首选渠道分流
    msg = env.default.sent[0]
    assert msg.title == "[待办汇总] 2 项未完成"
    assert msg.kind is DeliveryEvent.REMINDER
    assert msg.level is Level.WARNING
    assert msg.source == "notify-hub"
    assert msg.todo_id is None
    assert msg.overdue_seconds is None
    assert as_utc(msg.occurred_at) == now

    records = _delivery_rows(env.db)
    assert len(records) == 1
    assert records[0].message_id is None
    assert records[0].todo_id is None
    assert records[0].channel_id == "default-rec"
    assert records[0].ok is True
    assert records[0].event == DeliveryEvent.REMINDER.value


def test_digest_details_are_ordered_by_overdue_desc(tmp_path, manual_clock):
    """第 15 条：明细按超时时长降序（20h → 5h → 1h）。"""
    env = _rig(tmp_path, manual_clock)
    trigger = _local(1, 21, 0)
    for hours in (1, 20, 5):  # 故意乱序构造
        _todo_at(
            env,
            manual_clock,
            trigger - timedelta(hours=hours),
            source=f"s-15-{hours}h",
            title=f"待办-{hours}h",
        )
    manual_clock.set(trigger)

    assert env.scheduler.run_once() == 1
    body = env.default.sent[0].body
    assert body.index("待办-20h") < body.index("待办-5h") < body.index("待办-1h")


def test_digest_body_carries_titles_and_overdue_durations(tmp_path, manual_clock):
    """第 16 条：正文含每条待办标题与「已超时 <format_duration>」片段。"""
    from notify_hub.services.notifications import format_duration

    env = _rig(tmp_path, manual_clock)
    trigger = _local(1, 21, 0)
    _todo_at(
        env, manual_clock, trigger - timedelta(hours=20), source="s-16a", title="备份失败"
    )
    _todo_at(
        env, manual_clock, trigger - timedelta(hours=1), source="s-16b", title="磁盘告警"
    )
    manual_clock.set(trigger)

    assert env.scheduler.run_once() == 1
    body = env.default.sent[0].body
    assert "备份失败" in body
    assert "磁盘告警" in body
    assert "已超时" in body
    # 时长复用 notifications.format_duration，不另造一份实现
    assert format_duration(72000) in body  # 20 小时
    assert format_duration(3600) in body  # 1 小时
    assert "请到待办页面处理。" in body


def test_each_covered_todo_is_accounted_individually(tmp_path, manual_clock):
    """第 17 条：逐条记账（reminder_count +1、last_notified_at、各一条 REMINDER 事件），
    汇总只留一条投递记录。
    """
    env = _rig(tmp_path, manual_clock)
    trigger = _local(1, 21, 0)
    covered = [
        _todo_at(
            env,
            manual_clock,
            trigger - timedelta(hours=hours),
            source=f"s-17-{hours}",
            title=f"待办{hours}",
        )
        for hours in (3, 2, 1)
    ]
    manual_clock.set(trigger)
    now = manual_clock.now()

    assert env.scheduler.run_once() == 1

    for todo in covered:
        current = env.todos.get(todo.id)
        assert current is not None
        assert current.reminder_count == 1
        assert as_utc(current.last_notified_at) == now

        events = _reminder_events(env.db, todo.id)
        assert len(events) == 1
        assert events[0].channel_id == "default-rec"
        assert events[0].delivery_ok is True

    assert len(_delivery_rows(env.db)) == 1  # 汇总本身只有一条投递记录


def test_same_local_day_never_sends_twice(tmp_path, manual_clock):
    """第 18 条：同一自然日不重复发送。"""
    env = _rig(tmp_path, manual_clock)
    trigger = _local(1, 21, 0)
    todo = _todo_at(
        env, manual_clock, trigger - timedelta(hours=2), source="s-18", title="待办"
    )
    manual_clock.set(trigger)
    assert env.scheduler.run_once() == 1

    manual_clock.advance(60)
    assert env.scheduler.run_once() == 0
    manual_clock.advance(3600)
    assert env.scheduler.run_once() == 0

    assert len(_delivery_rows(env.db)) == 1
    assert len(env.default.sent) == 1
    assert env.todos.get(todo.id).reminder_count == 1
    state = env.digest.state_for(DAY1)
    assert state is not None
    assert state.attempts == 1


def test_empty_pending_settles_the_day(tmp_path, manual_clock):
    """第 19 条：空待办不发消息，但当天定案（``todo_count == 0``）。

    ``delivered=True`` / ``fired_at is None`` 来自 architecture.md 3.3 的冻结伪码：
    ``digest.record(today, todo_count=0, delivered=True)``。
    """
    env = _rig(tmp_path, manual_clock)
    manual_clock.set(_local(1, 21, 0))

    assert env.scheduler.run_once() == 0
    assert env.default.sent == []
    assert _delivery_rows(env.db) == []

    state = env.digest.state_for(DAY1)
    assert state is not None
    assert state.todo_count == 0
    assert state.delivered is True
    assert state.fired_at is None
    assert state.attempts == 1


def test_todo_created_after_settlement_does_not_fire_today(tmp_path, manual_clock):
    """第 20 条：当天定案之后新建的待办当天不再触发（可预期性取舍）。"""
    env = _rig(tmp_path, manual_clock)
    manual_clock.set(_local(1, 21, 0))
    assert env.scheduler.run_once() == 0

    todo = _todo_at(env, manual_clock, _local(1, 22, 30), source="s-20", title="深夜新建")
    manual_clock.set(_local(1, 23, 0))
    assert env.scheduler.run_once() == 0

    assert _delivery_rows(env.db) == []
    assert env.default.sent == []
    assert env.todos.get(todo.id).reminder_count == 0
    assert _reminder_events(env.db, todo.id) == []
    settled = env.digest.state_for(DAY1)
    assert settled is not None
    assert settled.attempts == 1  # 定案后不再改写记录
    assert settled.todo_count == 0


def test_next_day_digest_covers_only_still_pending(tmp_path, manual_clock):
    """第 21 条：次日正常触发，且只覆盖仍未完成的待办。"""
    env = _rig(tmp_path, manual_clock)
    trigger1 = _local(1, 21, 0)
    keep = _todo_at(
        env, manual_clock, trigger1 - timedelta(hours=3), source="s-21a", title="保留待办"
    )
    done = _todo_at(
        env, manual_clock, trigger1 - timedelta(hours=2), source="s-21b", title="已完成待办"
    )
    manual_clock.set(trigger1)
    assert env.scheduler.run_once() == 1
    assert env.todos.complete(done.id).status == "completed"

    manual_clock.set(_local(2, 21, 0))
    assert env.scheduler.run_once() == 1

    assert len(env.default.sent) == 2
    second = env.default.sent[1]
    assert second.title == "[待办汇总] 1 项未完成"
    assert "保留待办" in second.body
    assert "已完成待办" not in second.body

    state = env.digest.state_for(DAY2)
    assert state is not None
    assert state.todo_count == 1
    assert state.delivered is True
    assert len(_delivery_rows(env.db)) == 2
    # 跨两个自然日各汇总一次：该待办被覆盖 2 次 → 计数与事件数都应为 2（本次是加强）
    assert env.todos.get(keep.id).reminder_count == 2
    assert len(_reminder_events(env.db, keep.id)) == 2
    # 第 1 天覆盖后即完成的那条只被覆盖 1 次，第 2 天不再被覆盖
    assert env.todos.get(done.id).reminder_count == 1
    assert len(_reminder_events(env.db, done.id)) == 1


def test_failed_digest_retries_within_same_day(tmp_path, manual_clock):
    """第 22 条（异常场景 a）：投递失败当日重试，成功后 ``delivered=True`` / ``attempts == 2``。

    失败前提必须让**所有候选渠道都失败**（``fallback_channel=False`` 只注册一个失败渠道）：
    只要有任一渠道成功，降级链就会把本轮算作送达，不会进入重试路径。
    """
    env = _rig(tmp_path, manual_clock, default_ok=False, fallback_channel=False)
    trigger = _local(1, 21, 0)
    todo = _todo_at(
        env, manual_clock, trigger - timedelta(hours=2), source="s-22", title="渠道故障"
    )
    manual_clock.set(trigger)

    assert env.scheduler.run_once() == 0
    failed = env.digest.state_for(DAY1)
    assert failed is not None
    assert failed.delivered is False
    assert failed.attempts == 1
    assert failed.last_error
    assert "模拟渠道不可用" in failed.last_error  # 失败原因已落到状态里
    assert failed.fired_at is None
    assert failed.todo_count == 1
    # 未送达不记账
    assert env.todos.get(todo.id).reminder_count == 0
    assert _reminder_events(env.db, todo.id) == []
    assert len(_delivery_rows(env.db)) == 1
    assert _delivery_rows(env.db)[0].ok is False
    assert _delivery_rows(env.db)[0].channel_id == "default-rec"

    manual_clock.advance(60)  # 同一自然日内的下一个检查周期
    env.default.ok = True
    assert env.scheduler.run_once() == 1

    recovered = env.digest.state_for(DAY1)
    assert recovered is not None
    assert recovered.delivered is True
    assert recovered.attempts == 2
    assert as_utc(recovered.checked_at) == as_utc(failed.checked_at)
    assert as_utc(recovered.fired_at) == manual_clock.now()
    assert env.todos.get(todo.id).reminder_count == 1
    assert len(_reminder_events(env.db, todo.id)) == 1
    assert len(_delivery_rows(env.db)) == 2


def test_degraded_delivery_counts_as_success_without_retry(tmp_path, manual_clock):
    """第 22 条的对照面：默认渠道失败但降级到其它可用渠道成功 → ``outcome.ok`` 为真，
    本轮即算送达，**不重试**、当日不再发第二条汇总（降级链是既有冻结特性）。
    """
    env = _rig(tmp_path, manual_clock, default_ok=False, fallback_ok=True)
    trigger = _local(1, 21, 0)
    todo = _todo_at(
        env, manual_clock, trigger - timedelta(hours=2), source="s-22b", title="降级送达"
    )
    manual_clock.set(trigger)

    assert env.scheduler.run_once() == 1

    state = env.digest.state_for(DAY1)
    assert state is not None
    assert state.delivered is True
    assert state.attempts == 1
    assert state.todo_count == 1
    assert state.fired_at is not None
    assert state.last_error is None

    assert len(env.default.sent) == 1  # 尝试过但失败（``sent`` 记录的是尝试，含失败）
    assert len(env.preferred.sent) == 1  # 降级后真正送达的那一次
    assert env.preferred.sent[0].title == "[待办汇总] 1 项未完成"

    records = _delivery_rows(env.db)
    assert len(records) == 2  # 默认渠道失败一条 + 降级成功一条
    assert {(r.channel_id, r.ok) for r in records} == {
        ("default-rec", False),
        ("preferred-other", True),
    }
    assert env.todos.get(todo.id).reminder_count == 1

    manual_clock.advance(60)
    assert env.scheduler.run_once() == 0  # 已送达，当日不再重试
    assert len(_delivery_rows(env.db)) == 2
    assert env.digest.state_for(DAY1).attempts == 1


def test_failure_is_not_retried_on_a_later_day(tmp_path, manual_clock):
    """第 23 条：跨天不补发——昨天的失败记录不再被重试。"""
    env = _rig(tmp_path, manual_clock, default_ok=False, fallback_channel=False)
    trigger1 = _local(1, 21, 0)
    _todo_at(
        env, manual_clock, trigger1 - timedelta(hours=2), source="s-23", title="昨日失败"
    )
    manual_clock.set(trigger1)
    assert env.scheduler.run_once() == 0
    assert env.digest.state_for(DAY1).attempts == 1

    # 次日触发时刻之前
    manual_clock.set(_local(2, 20, 59))
    assert env.scheduler.run_once() == 0
    assert env.digest.state_for(DAY2) is None
    yesterday = env.digest.state_for(DAY1)
    assert yesterday is not None
    assert yesterday.delivered is False
    assert yesterday.attempts == 1
    assert len(_delivery_rows(env.db)) == 1

    # 次日触发时刻之后：只产生当日的新记录，昨日记录仍不被补发
    manual_clock.set(_local(2, 21, 0))
    assert env.scheduler.run_once() == 0  # 渠道仍不可用，故当日也发不出去
    assert env.digest.state_for(DAY1).attempts == 1
    today = env.digest.state_for(DAY2)
    assert today is not None
    assert today.attempts == 1
    assert today.delivered is False
    assert len(_delivery_rows(env.db)) == 2


def test_restart_does_not_resend_on_the_same_day(tmp_path, manual_clock):
    """第 24 条：重启安全——新调度器实例（新建 Database/服务）在同一自然日不再发送。

    状态只在 ``digest_runs`` 表里，不依赖内存标记。
    """
    env = _rig(tmp_path, manual_clock)
    trigger = _local(1, 21, 0)
    _todo_at(env, manual_clock, trigger - timedelta(hours=2), source="s-24a", title="待办A")
    _todo_at(env, manual_clock, trigger - timedelta(hours=1), source="s-24b", title="待办B")
    manual_clock.set(trigger)
    assert env.scheduler.run_once() == 1
    env.db.dispose()

    # 模拟进程重启：同一库文件、全新的 Database / 服务 / 调度器 / 适配器
    restarted = _rig(tmp_path, manual_clock)
    assert restarted.digest.state_for(DAY1) is not None

    manual_clock.advance(30)
    assert restarted.scheduler.run_once() == 0
    assert restarted.default.sent == []
    assert len(_delivery_rows(restarted.db)) == 1
    state = restarted.digest.state_for(DAY1)
    assert state is not None
    assert state.attempts == 1
    assert state.delivered is True


def test_accounting_exception_for_one_todo_does_not_abort_the_round(
    tmp_path, manual_clock
):
    """第 25 条（异常场景 b）：某条待办记账抛异常时，其余待办仍记账、汇总仍算成功。"""
    env = _rig(tmp_path, manual_clock)
    trigger = _local(1, 21, 0)
    for hours in (3, 2, 1):
        _todo_at(
            env,
            manual_clock,
            trigger - timedelta(hours=hours),
            source=f"s-25-{hours}",
            title=f"待办{hours}",
        )
    manual_clock.set(trigger)

    ordered = env.todos.list()
    assert len(ordered) == 3
    exploding_id = ordered[0].id  # 超时最长的待办 = 汇总里的第一条
    proxy = _ExplodingAccounting(env.todos, explode_id=exploding_id)
    scheduler = _scheduler(
        todos=proxy,
        delivery=env.delivery,
        digest=env.digest,
        clock=manual_clock,
        settings=env.settings,
    )

    assert scheduler.run_once() == 1  # 不抛异常，汇总仍算成功

    assert proxy.calls == [todo.id for todo in ordered]  # 整轮未被中断
    for todo in ordered:
        current = env.todos.get(todo.id)
        if todo.id == exploding_id:
            assert current.reminder_count == 0
            assert _reminder_events(env.db, todo.id) == []
        else:
            assert current.reminder_count == 1
            assert len(_reminder_events(env.db, todo.id)) == 1

    state = env.digest.state_for(DAY1)
    assert state is not None
    assert state.delivered is True
    assert state.attempts == 1
    assert len(_delivery_rows(env.db)) == 1


# --------------------------------------------------------------------------- #
# 第 26–27 条：移除项的显式验证
# --------------------------------------------------------------------------- #
def test_due_for_reminder_is_removed_from_todo_service():
    """第 26 条：``TodoService`` 上不得再存在 ``due_for_reminder``（不得留兼容垫片）。

    对应规格 REMOVED「超时判定与重复提醒」。
    """
    from notify_hub.services.todos import TodoService

    assert not hasattr(TodoService, "due_for_reminder")


def test_reminder_settings_fields_are_exactly_the_new_trio():
    """第 27 条：``ReminderSettings`` 的字段集合恰好是 ``{at, timezone, scan_interval_seconds}``。

    旧键 ``first_reminder_after_seconds`` / ``reminder_interval_seconds`` 必须已删除。
    """
    from notify_hub.config import ReminderSettings

    assert set(ReminderSettings.__dataclass_fields__) == {
        "at",
        "timezone",
        "scan_interval_seconds",
    }
    defaults = ReminderSettings()
    assert defaults.at == "21:00"
    assert defaults.timezone == "Asia/Shanghai"
    assert defaults.scan_interval_seconds == 60.0
