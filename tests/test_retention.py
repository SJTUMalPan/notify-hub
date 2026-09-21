"""M-A 模块测试：数据保留与自动回收（``services/retention.py`` 与调度器挂钩）。

覆盖 ``openspec/changes/add-retention-and-web-ui/architecture.md`` 第 4.5 节的验收表
（单元 / 阶段 0 共享文件组 / 行为 / 调度器集成）。

**只依据冻结规格书写**：本文件写于 ``notify_hub.services.retention`` 尚不存在之时。
``RetentionService`` 按 §4.2 的签名**直接构造**，不经 ``context.py`` 装配
（该装配由架构师在 M-A 实现落地后补上）。

**惰性导入是硬要求**：顶层只导入标准库、pytest 与**已存在**的模块，保证 pytest 能成功
**收集**本文件；失败一律发生在运行时（``ModuleNotFoundError`` ＝ 实现缺失）。

**禁止真实等待**：时间一律由 ``conftest.py`` 的 ``manual_clock``（``ManualClock``）驱动。

**只读夹具**：本文件不修改 ``tests/conftest.py``，也不得修改 ``src/`` 下任何文件。
"""

from __future__ import annotations

import contextlib
import logging
from datetime import date, datetime, timedelta, timezone

import pytest
import yaml

from notify_hub.clock import ManualClock, as_utc
from notify_hub.domain import DeliveryEvent, TodoEventKind, TodoStatus

#: ``ManualClock`` 起点（``conftest.START``）＝北京时间 08:00，早于触发时刻 21:00。
T0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
#: 北京时间（Asia/Shanghai 无夏令时，固定 +08:00）的上午十点，测试内的「今天」。
LOCAL_NOW = datetime(2024, 4, 10, 10, 0, 0, tzinfo=timezone(timedelta(hours=8)))
#: 北京时间 21:00 ＝ 触发时刻（``reminders.at = "21:00"``）。
LOCAL_TRIGGER = datetime(2024, 4, 10, 21, 0, 0, tzinfo=timezone(timedelta(hours=8)))
#: ``LOCAL_NOW`` 的本地自然日。
TODAY = date(2024, 4, 10)
#: 默认保留期。
DAYS = 30

LOG = logging.getLogger("notify_hub.tests.retention")


def _local_date(moment: datetime, zone) -> date:
    """把时刻换算到给定时区取日期（规格 §3 的「时间基准」）。"""
    return as_utc(moment).astimezone(zone).date()


# --------------------------------------------------------------------------- #
# 惰性构造工具
# --------------------------------------------------------------------------- #
def _service(db, clock, *, days=DAYS, zone, logger=None):
    """按 §4.2 的签名直接构造被测服务（不依赖 ``context.py`` 装配）。"""
    from notify_hub.services.retention import RetentionService

    return RetentionService(db, clock, days=days, zone=zone, logger=logger)


def _report_cls():
    from notify_hub.services.retention import PurgeReport

    return PurgeReport


def _rows(db, model):
    """只读地取出某张表的全部行（不 commit，避免 ``DetachedInstanceError``）。"""
    from sqlmodel import Session, select

    with Session(db.engine) as session:
        return list(session.exec(select(model)).all())


def _count(db, model) -> int:
    return len(_rows(db, model))


def _make_message(
    db,
    *,
    received_at: datetime,
    source: str = "test",
    title: str = "消息",
    dedup_key: str | None = None,
):
    """插入一条消息，返回其主键（调用方随后用 id 造待办/投递）。"""
    from notify_hub.models import Message

    with db.session() as session:
        row = Message(
            source=source,
            title=title,
            body="正文",
            occurred_at=received_at,
            received_at=received_at,
            dedup_key=dedup_key,
        )
        session.add(row)
        session.flush()
        return row.id


def _make_todo(
    db,
    message_id: int,
    *,
    status: str = TodoStatus.PENDING.value,
    completed_at: datetime | None = None,
    source: str = "test",
    dedup_key: str | None = None,
):
    """插入一条待办，返回其主键。默认造一条 pending 待办。"""
    from notify_hub.models import Todo

    with db.session() as session:
        row = Todo(
            message_id=message_id,
            source=source,
            dedup_key=dedup_key,
            title="待办",
            status=status,
            ack_reason="rule",
            created_at=T0,
            first_notified_at=T0,
            last_notified_at=T0,
            reminder_count=0,
            completed_at=completed_at,
        )
        session.add(row)
        session.flush()
        return row.id


def _make_todo_event(db, todo_id: int, *, occurred_at: datetime, kind: str):
    """给待办插一条事件，返回其主键。"""
    from notify_hub.models import TodoEvent

    with db.session() as session:
        row = TodoEvent(todo_id=todo_id, kind=kind, occurred_at=occurred_at)
        session.add(row)
        session.flush()
        return row.id


def _make_delivery(
    db,
    *,
    attempted_at: datetime,
    message_id: int | None = None,
    todo_id: int | None = None,
    ok: bool = True,
):
    """插一条投递记录，返回其主键。"""
    from notify_hub.models import DeliveryRecord

    with db.session() as session:
        row = DeliveryRecord(
            message_id=message_id,
            todo_id=todo_id,
            channel_id="recording",
            attempted_at=attempted_at,
            ok=ok,
            receipt="r-1",
            event=DeliveryEvent.FIRST_NOTICE.value,
        )
        session.add(row)
        session.flush()
        return row.id


def _make_digest_run(db, *, local_date: date, checked_at: datetime):
    """插一条 ``digest_runs``，返回其主键。"""
    from notify_hub.models import DigestRun

    with db.session() as session:
        row = DigestRun(
            local_date=local_date,
            checked_at=checked_at,
            todo_count=1,
            delivered=True,
            attempts=1,
        )
        session.add(row)
        session.flush()
        return row.id


def _todo_ids(db) -> set[int]:
    from notify_hub.models import Todo

    return {row.id for row in _rows(db, Todo)}


def _message_ids(db) -> set[int]:
    from notify_hub.models import Message

    return {row.id for row in _rows(db, Message)}


def _delivery_by_id(db, delivery_id: int):
    from notify_hub.models import DeliveryRecord

    for row in _rows(db, DeliveryRecord):
        if row.id == delivery_id:
            return row
    return None


def _todo_event_ids(db) -> set[int]:
    from notify_hub.models import TodoEvent

    return {row.id for row in _rows(db, TodoEvent)}


def _todo_events_for(db, todo_id: int):
    from notify_hub.models import TodoEvent

    return [row for row in _rows(db, TodoEvent) if row.todo_id == todo_id]


def _digest_dates(db) -> set[date]:
    from notify_hub.models import DigestRun

    return {row.local_date for row in _rows(db, DigestRun)}


def _dangling_todo_refs(db) -> list[tuple[str, int]]:
    """返回指向不存在的待办的外键引用 ``(表名, 引用值)``；规格要求恢复后为空。"""
    from notify_hub.models import DeliveryRecord, TodoEvent

    known = _todo_ids(db)
    bad: list[tuple[str, int]] = []
    for row in _rows(db, DeliveryRecord):
        if row.todo_id is not None and row.todo_id not in known:
            bad.append(("deliveries.todo_id", row.todo_id))
    for row in _rows(db, TodoEvent):
        if row.todo_id not in known:
            bad.append(("todo_events.todo_id", row.todo_id))
    return bad


@contextlib.contextmanager
def _capture(logger):
    """捕获 ``logger`` 自身的记录（不依赖传播到根 logger）。

    实现可能把日志打到子 logger（例如 ``...retention``）上；这里同时捕获该 logger
    的**后代**，避免因 logger 名不同而把「打了日志」误判成「没打日志」。
    """
    captured: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Handler(level=logging.DEBUG)
    loggers = [logger] + [
        logging.getLogger(name)
        for name in logging.root.manager.loggerDict
        if name == logger.name or name.startswith(logger.name + ".")
    ]
    previous_levels = [(item, item.level) for item in loggers]
    previous_propagate = logger.propagate
    try:
        for item in loggers:
            item.setLevel(logging.DEBUG)
            item.addHandler(handler)
        logger.propagate = False
        yield captured
    finally:
        logger.propagate = previous_propagate
        for item, level in previous_levels:
            item.removeHandler(handler)
            item.setLevel(level)


def _retention_logger():
    return logging.getLogger("notify_hub.services.retention")


# --------------------------------------------------------------------------- #
# 调度器构造（按 ``context.py`` 第 12 步的完整方式，补上 ``retention=``）
# --------------------------------------------------------------------------- #
class _FakeRetention:
    """记录调用次数、可切换为抛异常的假 retention（异常隔离用例用）。"""

    def __init__(self, *, raise_exc: BaseException | None = None) -> None:
        self.calls = 0
        self.raise_exc = raise_exc

    def purge_expired(self, now=None):
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return _report_cls()


def _make_scheduler(settings, *, clock, retention, notifiers=(), db):
    from notify_hub.delivery import DeliveryService
    from notify_hub.notifiers import NotifierRegistry
    from notify_hub.services.digest import DigestService
    from notify_hub.services.scheduler import ReminderScheduler
    from notify_hub.services.todos import TodoService

    registry = NotifierRegistry()
    for notifier in notifiers:
        registry.register(notifier)
    delivery = DeliveryService(
        db,
        registry,
        default_channel=settings.default_channel,
        channel_order=registry.ids(),
        clock=clock,
        logger=LOG,
    )
    return ReminderScheduler(
        todos=TodoService(db, clock, logger=LOG),
        delivery=delivery,
        digest=DigestService(db, clock),
        clock=clock,
        settings=settings.reminders,
        logger=LOG,
        retention=retention,
    )


# --------------------------------------------------------------------------- #
# 单元测试：enabled / cutoff_date / PurgeReport（§4.5 前两张表）
# --------------------------------------------------------------------------- #
def test_enabled_false_when_days_zero(db, manual_clock, tmp_settings):
    service = _service(db, manual_clock, days=0, zone=tmp_settings.reminders.zone)
    assert service.enabled is False


def test_enabled_true_when_days_thirty(db, manual_clock, tmp_settings):
    service = _service(db, manual_clock, days=30, zone=tmp_settings.reminders.zone)
    assert service.enabled is True


def test_cutoff_date_is_today_minus_days(db, manual_clock, tmp_settings):
    manual_clock.set(LOCAL_NOW)
    service = _service(db, manual_clock, days=30, zone=tmp_settings.reminders.zone)
    assert service.cutoff_date() == TODAY - timedelta(days=30)


def test_cutoff_date_days_zero_is_today(db, manual_clock, tmp_settings):
    manual_clock.set(LOCAL_NOW)
    service = _service(db, manual_clock, days=0, zone=tmp_settings.reminders.zone)
    assert service.cutoff_date() == TODAY


def test_cutoff_date_accepts_naive_now_and_normalizes_as_utc(
    db, manual_clock, tmp_settings
):
    """naive ``now`` 不得抛错，且按 UTC（而非本地）归一化：与显式 aware UTC 等价。"""
    service = _service(db, manual_clock, days=30, zone=tmp_settings.reminders.zone)
    naive = datetime(2024, 4, 10, 10, 0, 0)
    aware = datetime(2024, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
    assert service.cutoff_date(naive) == service.cutoff_date(aware)


def test_cutoff_date_naive_uses_utc_date_not_local_wall_date(
    db, manual_clock, tmp_settings
):
    """naive 输入按 **UTC** 读（SQLite 读出的时间是裸的），不是本地墙钟时间。

    naive ``2024-04-10 07:00`` ＝ UTC ``2024-04-10 07:00`` ＝ 北京时间 15:00，
    本地自然日仍是 04-10，故 cutoff 为 ``04-10 - 30 天``。
    若误按北京时间墙钟读（UTC 04-09 23:00，本地日 04-09）才会得到 04-09——那是错的。
    """
    manual_clock.set(LOCAL_NOW)
    service = _service(db, manual_clock, days=30, zone=tmp_settings.reminders.zone)
    naive_utc = datetime(2024, 4, 10, 7, 0, 0)
    assert service.cutoff_date(naive_utc) == date(2024, 4, 10) - timedelta(days=30)


def test_cutoff_date_huge_days_does_not_raise(db, manual_clock, tmp_settings):
    manual_clock.set(LOCAL_NOW)
    service = _service(db, manual_clock, days=10**9, zone=tmp_settings.reminders.zone)
    assert service.cutoff_date() == date.min


def test_huge_days_purge_expired_does_not_raise_and_deletes_nothing(
    db, manual_clock, tmp_settings
):
    """§4.2「``days`` 极大时不得抛异常＝不回收任何东西」的**端到端**形态。

    上一条只验到 ``cutoff_date()`` 返回 ``date.min``；再往下走一步才是真正的契约：
    ``purge_expired()`` 必须同样不抛异常、返回空报告、且一条记录都不删。
    「不回收任何东西」是语义，不是「结算时抛异常再打一条 WARNING」——后者会让
    每一次结算都产生排查噪音。
    """
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    old = T0 - timedelta(days=100)
    message_id = _make_message(db, received_at=old)
    todo_id = _make_todo(db, message_id, status=TodoStatus.DONE.value, completed_at=old)
    _make_todo_event(db, todo_id, occurred_at=old, kind=TodoEventKind.COMPLETED.value)
    _make_delivery(db, attempted_at=old, message_id=message_id, todo_id=todo_id)
    _make_digest_run(db, local_date=_local_date(old, zone), checked_at=old)
    from notify_hub.models import DeliveryRecord, DigestRun, Message, Todo, TodoEvent

    tables = {
        "todos": Todo,
        "todo_events": TodoEvent,
        "deliveries": DeliveryRecord,
        "messages": Message,
        "digest_runs": DigestRun,
    }
    before = {name: _count(db, model) for name, model in tables.items()}
    assert all(count > 0 for count in before.values()), f"夹具没造出数据：{before}"

    service = _service(db, clock, days=10**9, zone=zone)
    assert service.cutoff_date() == date.min, "前置：极大 days 下 cutoff 应为 date.min"

    report = service.purge_expired()

    assert report.is_empty is True, (
        f"极大 days 语义上等价于「没有任何记录早于 cutoff」，报告必须为空，实际 {report!r}"
    )
    assert report.deleted_total == 0
    assert {name: _count(db, model) for name, model in tables.items()} == before, (
        f"极大 days 下一条记录都不该被删，回收前 {before}，"
        f"回收后 {({name: _count(db, model) for name, model in tables.items()})}"
    )


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({}, 0),
        ({"todos": 3, "todo_events": 4, "deliveries": 6, "messages": 7, "digest_runs": 8}, 28),
        ({"deliveries_unlinked": 5}, 0),
        (
            {
                "todos": 1,
                "todo_events": 2,
                "deliveries_unlinked": 9,
                "deliveries": 3,
                "messages": 4,
                "digest_runs": 5,
            },
            15,
        ),
    ],
)
def test_deleted_total_sums_all_fields_except_unlinked(fields, expected):
    report = _report_cls()(**fields)
    assert report.deleted_total == expected


def test_is_empty_true_when_all_zero():
    assert _report_cls()().is_empty is True


def test_is_empty_false_when_something_deleted():
    assert _report_cls()(messages=1).is_empty is False


def test_is_empty_false_when_only_unlinked():
    """解绑也算「发生了事」：行没少，但归属被改写了。"""
    assert _report_cls()(deliveries_unlinked=1).is_empty is False


# --------------------------------------------------------------------------- #
# 单元测试：阶段 0 共享文件组（``config.py`` 的 ``retention`` 段，语义见 §2）
# --------------------------------------------------------------------------- #
def _settings_for(tmp_path, retention):
    """写一份最小合法配置（可选带 ``retention`` 段）并加载。"""
    from notify_hub.config import load_settings

    (tmp_path / "rules.yaml").write_text(
        yaml.safe_dump(
            {
                "case_sensitive": False,
                "defaults": {"category": "uncategorized", "labels": [], "need_ack": False},
                "rules": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    config = {
        "server": {"host": "127.0.0.1", "port": 8000, "log_level": "INFO"},
        "storage": {"db_path": "./data/notify.db"},
        "rules": {"path": "./rules.yaml", "poll_interval_seconds": 5},
        "reminders": {"at": "21:00", "timezone": "Asia/Shanghai", "scan_interval_seconds": 1},
        "default_channel": None,
        "channels": [],
    }
    if retention is not None:
        config["retention"] = retention
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return load_settings(config_path)


def test_retention_section_missing_defaults_to_30(tmp_path):
    assert _settings_for(tmp_path, None).retention.days == 30


def test_retention_section_empty_mapping_defaults_to_30(tmp_path):
    assert _settings_for(tmp_path, {}).retention.days == 30


def test_retention_days_zero_disables(tmp_path):
    settings = _settings_for(tmp_path, {"days": 0})
    assert settings.retention.days == 0
    assert settings.retention.enabled is False


def test_retention_days_ninety_enabled(tmp_path):
    settings = _settings_for(tmp_path, {"days": 90})
    assert settings.retention.days == 90
    assert settings.retention.enabled is True


def test_retention_days_negative_raises(tmp_path):
    from notify_hub.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        _settings_for(tmp_path, {"days": -1})


def test_retention_days_float_raises_not_truncated(tmp_path):
    """``30.7`` 不得静默截断成 30——那等于悄悄改短与删数据有关的参数。"""
    from notify_hub.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        _settings_for(tmp_path, {"days": 30.7})


def test_retention_days_string_raises(tmp_path):
    from notify_hub.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        _settings_for(tmp_path, {"days": "30"})


def test_retention_days_bool_raises(tmp_path):
    """``bool`` 是 ``int`` 的子类，但不得被当成整数接受。"""
    from notify_hub.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        _settings_for(tmp_path, {"days": True})


def test_retention_settings_defaults():
    from notify_hub.config import RetentionSettings

    settings = RetentionSettings()
    assert settings.days == 30
    assert settings.enabled is True


# --------------------------------------------------------------------------- #
# 行为测试：先造数据，再断言删没删（§4.5 第三张表）
# --------------------------------------------------------------------------- #
def test_pending_todo_never_deleted_and_message_pinned(db, manual_clock, tmp_settings):
    """硬约束 D2：pending 待办永不删，它引用的消息也不得删。"""
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    old = T0 - timedelta(days=100)
    message_id = _make_message(db, received_at=old)
    todo_id = _make_todo(db, message_id)
    _make_todo_event(db, todo_id, occurred_at=old, kind=TodoEventKind.CREATED.value)
    service = _service(db, clock, days=DAYS, zone=zone)

    service.purge_expired()

    assert todo_id in _todo_ids(db), "pending 待办被删了（违反硬约束）"
    assert message_id in _message_ids(db), "被待办引用的消息被删了（违反硬约束）"


def test_message_referenced_by_todo_keeps_all_deliveries(db, manual_clock, tmp_settings):
    """被待办引用的消息与它的全部投递记录一行不少。"""
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    old = T0 - timedelta(days=100)
    message_id = _make_message(db, received_at=old)
    todo_id = _make_todo(db, message_id)
    delivery_id = _make_delivery(db, attempted_at=old, message_id=message_id, todo_id=todo_id)
    from notify_hub.models import DeliveryRecord

    before = _count(db, DeliveryRecord)
    service = _service(db, clock, days=DAYS, zone=zone)

    service.purge_expired()

    assert _count(db, DeliveryRecord) == before
    row = _delivery_by_id(db, delivery_id)
    assert row is not None
    assert row.todo_id == todo_id, "被保留的待办，其投递记录的归属不该被改写"


def test_expired_completed_todo_and_its_events_deleted(db, manual_clock, tmp_settings):
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    old = T0 - timedelta(days=100)
    message_id = _make_message(db, received_at=old)
    todo_id = _make_todo(db, message_id, status=TodoStatus.DONE.value, completed_at=old)
    event_id = _make_todo_event(db, todo_id, occurred_at=old, kind=TodoEventKind.COMPLETED.value)
    service = _service(db, clock, days=DAYS, zone=zone)

    report = service.purge_expired()

    assert todo_id not in _todo_ids(db)
    assert event_id not in _todo_event_ids(db)
    assert report.todos == 1
    assert report.todo_events == 1


def test_delivery_of_deleted_todo_is_unlinked_not_deleted(db, manual_clock, tmp_settings):
    """D4 的核心承诺：解绑（真的写 NULL）而不是删行，``message_id`` 不变。

    该承诺的耐久形态只能在 ``purge_completed_older_than`` 上观察（§4.2）：
    ``purge_expired`` 第 1 步解绑后，第 2 步必然把那条旧消息连同它的投递记录一起删掉。
    这里待办完成于昨天、消息收于两天前，二者都在保留期内，故本方法不动 messages。
    """
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    completed = T0 - timedelta(days=1)
    received = T0 - timedelta(days=2)
    message_id = _make_message(db, received_at=received)
    todo_id = _make_todo(db, message_id, status=TodoStatus.DONE.value, completed_at=completed)
    event_id = _make_todo_event(
        db, todo_id, occurred_at=completed, kind=TodoEventKind.COMPLETED.value
    )
    delivery_id = _make_delivery(
        db, attempted_at=received, message_id=message_id, todo_id=todo_id
    )
    from notify_hub.models import DeliveryRecord

    before = _count(db, DeliveryRecord)
    service = _service(db, clock, days=DAYS, zone=zone)

    report = service.purge_completed_older_than(_local_date(T0, zone))

    assert todo_id not in _todo_ids(db), "完成于昨天的待办（早于 cutoff）应被删除"
    assert event_id not in _todo_event_ids(db), "被删待办的 todo_events 应一并删除"
    assert message_id in _message_ids(db), "该方法明文不动 messages，消息必须存活"
    assert _count(db, DeliveryRecord) == before, "投递记录被删了，应当只解绑"
    row = _delivery_by_id(db, delivery_id)
    assert row is not None
    assert row.todo_id is None
    assert row.message_id == message_id
    assert report.deliveries_unlinked == 1
    assert report.deliveries == 0


def test_record_completed_within_retention_is_untouched(db, manual_clock, tmp_settings):
    """完成于 1 天前（``days=30``）→ 待办与消息都还在。"""
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    recent = T0 - timedelta(days=1)
    message_id = _make_message(db, received_at=recent)
    todo_id = _make_todo(db, message_id, status=TodoStatus.DONE.value, completed_at=recent)
    service = _service(db, clock, days=DAYS, zone=zone)

    report = service.purge_expired()

    assert todo_id in _todo_ids(db)
    assert message_id in _message_ids(db)
    assert report.is_empty


def test_old_message_without_any_todo_is_deleted_with_its_deliveries(
    db, manual_clock, tmp_settings
):
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    old = T0 - timedelta(days=100)
    message_id = _make_message(db, received_at=old)
    delivery_id = _make_delivery(db, attempted_at=old, message_id=message_id)
    from notify_hub.models import DeliveryRecord

    before = _count(db, DeliveryRecord)
    service = _service(db, clock, days=DAYS, zone=zone)

    report = service.purge_expired()

    assert message_id not in _message_ids(db)
    assert _count(db, DeliveryRecord) == before - 1
    assert _delivery_by_id(db, delivery_id) is None
    assert report.messages == 1
    assert report.deliveries == 1


def test_recent_message_without_todo_is_untouched(db, manual_clock, tmp_settings):
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    recent = T0 - timedelta(days=1)
    message_id = _make_message(db, received_at=recent)
    service = _service(db, clock, days=DAYS, zone=zone)

    report = service.purge_expired()

    assert message_id in _message_ids(db)
    assert report.is_empty


def test_digest_runs_purged_by_local_date(db, manual_clock, tmp_settings):
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    old = T0 - timedelta(days=100)
    _make_digest_run(db, local_date=_local_date(old, zone), checked_at=old)
    _make_digest_run(db, local_date=_local_date(T0, zone), checked_at=T0)
    service = _service(db, clock, days=DAYS, zone=zone)

    report = service.purge_expired()

    assert _digest_dates(db) == {_local_date(T0, zone)}
    assert report.digest_runs == 1


def test_days_zero_disables_purge_entirely(db, manual_clock, tmp_settings):
    """``days=0``：什么都不删，返回全 0。"""
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    old = T0 - timedelta(days=100)
    message_id = _make_message(db, received_at=old)
    todo_id = _make_todo(db, message_id, status=TodoStatus.DONE.value, completed_at=old)
    delivery_id = _make_delivery(db, attempted_at=old, message_id=message_id, todo_id=todo_id)
    _make_digest_run(db, local_date=_local_date(old, zone), checked_at=old)
    from notify_hub.models import DeliveryRecord, DigestRun, Message, Todo

    before = (
        _count(db, Todo),
        _count(db, Message),
        _count(db, DeliveryRecord),
        _count(db, DigestRun),
    )
    service = _service(db, clock, days=0, zone=zone)

    report = service.purge_expired()

    after = (
        _count(db, Todo),
        _count(db, Message),
        _count(db, DeliveryRecord),
        _count(db, DigestRun),
    )
    assert after == before
    assert report.is_empty
    assert report.deleted_total == 0
    assert report.deliveries_unlinked == 0


def test_purge_is_idempotent(db, manual_clock, tmp_settings):
    """紧接着再跑一次：``deleted_total == 0`` 且 ``deliveries_unlinked == 0``。"""
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    old = T0 - timedelta(days=100)
    old_message = _make_message(db, received_at=old)
    old_todo = _make_todo(db, old_message, status=TodoStatus.DONE.value, completed_at=old)
    _make_todo_event(db, old_todo, occurred_at=old, kind=TodoEventKind.COMPLETED.value)
    _make_delivery(db, attempted_at=old, message_id=old_message, todo_id=old_todo)
    orphan_message = _make_message(db, received_at=old, dedup_key="orphan")
    _make_delivery(db, attempted_at=old, message_id=orphan_message)
    _make_digest_run(db, local_date=_local_date(old, zone), checked_at=old)
    service = _service(db, clock, days=DAYS, zone=zone)

    first = service.purge_expired()
    assert first.deleted_total > 0, "前置条件：第一次应当确实删掉了东西"

    second = service.purge_expired()

    assert second.deleted_total == 0
    assert second.deliveries_unlinked == 0


def test_no_dangling_references_after_purge(db, manual_clock, tmp_settings):
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    old = T0 - timedelta(days=100)
    for index in range(2):
        message_id = _make_message(db, received_at=old, dedup_key=f"d-{index}")
        todo_id = _make_todo(
            db, message_id, status=TodoStatus.DONE.value, completed_at=old, dedup_key=f"d-{index}"
        )
        _make_todo_event(db, todo_id, occurred_at=old, kind=TodoEventKind.COMPLETED.value)
        _make_delivery(db, attempted_at=old, message_id=message_id, todo_id=todo_id)
    pending_message = _make_message(db, received_at=old, dedup_key="pending-1")
    _make_todo(db, pending_message, dedup_key="pending-1")
    service = _service(db, clock, days=DAYS, zone=zone)

    service.purge_expired()

    assert _dangling_todo_refs(db) == []


def test_done_todo_with_null_completed_at_is_kept(db, manual_clock, tmp_settings):
    """``completed_at`` 为 NULL 的 done 待办＝数据异常，保守不删。"""
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    old = T0 - timedelta(days=100)
    message_id = _make_message(db, received_at=old)
    todo_id = _make_todo(db, message_id, status=TodoStatus.DONE.value, completed_at=None)
    service = _service(db, clock, days=DAYS, zone=zone)

    report = service.purge_expired()

    assert todo_id in _todo_ids(db)
    assert report.todos == 0


def test_purge_completed_older_than_leaves_messages_and_digests_alone(
    db, manual_clock, tmp_settings
):
    """§4.2：``purge_completed_older_than`` 只动待办、待办事件与投递归属。"""
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    old = T0 - timedelta(days=100)
    message_id = _make_message(db, received_at=old)
    todo_id = _make_todo(db, message_id, status=TodoStatus.DONE.value, completed_at=old)
    _make_todo_event(db, todo_id, occurred_at=old, kind=TodoEventKind.COMPLETED.value)
    delivery_id = _make_delivery(db, attempted_at=old, message_id=message_id, todo_id=todo_id)
    _make_digest_run(db, local_date=_local_date(old, zone), checked_at=old)
    from notify_hub.models import DigestRun

    digests_before = _count(db, DigestRun)
    service = _service(db, clock, days=DAYS, zone=zone)

    report = service.purge_completed_older_than(_local_date(T0, zone))

    assert todo_id not in _todo_ids(db)
    assert message_id in _message_ids(db), "该方法的规格明确不动 messages"
    assert _count(db, DigestRun) == digests_before, "该方法的规格明确不动 digest_runs"
    row = _delivery_by_id(db, delivery_id)
    assert row is not None and row.todo_id is None
    assert report.deliveries_unlinked == 1
    assert report.messages == 0
    assert report.digest_runs == 0


def test_purge_completed_older_than_keeps_today(db, manual_clock, tmp_settings):
    """网页「清空已完成」的语义：cutoff ＝ 今天 → 今天完成的不删。"""
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    old = T0 - timedelta(days=100)
    old_message = _make_message(db, received_at=old, dedup_key="old")
    old_todo = _make_todo(
        db, old_message, status=TodoStatus.DONE.value, completed_at=old, dedup_key="old"
    )
    today_message = _make_message(db, received_at=T0, dedup_key="today")
    today_todo = _make_todo(
        db, today_message, status=TodoStatus.DONE.value, completed_at=T0, dedup_key="today"
    )
    service = _service(db, clock, days=DAYS, zone=zone)

    service.purge_completed_older_than(_local_date(T0, zone))

    ids = _todo_ids(db)
    assert today_todo in ids
    assert old_todo not in ids


def test_purge_completed_older_than_keeps_pending(db, manual_clock, tmp_settings):
    """硬约束同样适用于按 cutoff 直呼的路径。"""
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    old = T0 - timedelta(days=100)
    message_id = _make_message(db, received_at=old)
    todo_id = _make_todo(db, message_id)
    service = _service(db, clock, days=DAYS, zone=zone)

    service.purge_completed_older_than(_local_date(T0, zone))

    assert todo_id in _todo_ids(db)


# --------------------------------------------------------------------------- #
# 日志契约（§4.2 最后一条）
# --------------------------------------------------------------------------- #
def test_logs_one_info_line_when_something_happened(db, manual_clock, tmp_settings):
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    old = T0 - timedelta(days=100)
    message_id = _make_message(db, received_at=old)
    todo_id = _make_todo(db, message_id, status=TodoStatus.DONE.value, completed_at=old)
    _make_delivery(db, attempted_at=old, message_id=message_id, todo_id=todo_id)
    service = _service(db, clock, days=DAYS, zone=zone)

    with _capture(_retention_logger()) as records:
        report = service.purge_expired()

    infos = [record for record in records if record.levelno == logging.INFO]
    assert report.deleted_total > 0 or report.deliveries_unlinked > 0
    assert len(infos) == 1, "发生回收时必须恰好打一条 INFO"


def test_logs_nothing_when_nothing_happened(db, manual_clock, tmp_settings):
    zone = tmp_settings.reminders.zone
    clock = ManualClock(T0)
    service = _service(db, clock, days=DAYS, zone=zone)

    with _capture(_retention_logger()) as records:
        report = service.purge_expired()

    assert report.is_empty
    assert records == [], "什么都没发生时不得打日志"


# --------------------------------------------------------------------------- #
# 调度器集成测试（ManualClock 推进，禁止真实等待）
# --------------------------------------------------------------------------- #
def _scheduler_fixture(
    db, manual_clock, tmp_settings, *, days=DAYS, retention=None, notifiers=()
):
    """造一条 100 天前的已完成待办 + 一条 pending 待办，返回 (scheduler, retention, ids)。

    ``notifiers`` 转交 ``_make_scheduler``：不接渠道时注册表为空，汇总投递必然失败
    （``run_once`` 返回 0），所以需要「投递成功」语义的用例必须传入记录型渠道。
    """
    zone = tmp_settings.reminders.zone
    clock = manual_clock
    clock.set(T0)
    old = T0 - timedelta(days=100)
    old_message = _make_message(db, received_at=old, dedup_key="old-1")
    old_todo = _make_todo(
        db, old_message, status=TodoStatus.DONE.value, completed_at=old, dedup_key="old-1"
    )
    _make_todo_event(db, old_todo, occurred_at=old, kind=TodoEventKind.COMPLETED.value)
    _make_delivery(db, attempted_at=old, message_id=old_message, todo_id=old_todo)
    pending_message = _make_message(db, received_at=old, dedup_key="pending-1")
    pending_todo = _make_todo(db, pending_message, dedup_key="pending-1")
    if retention is None:
        retention = _service(db, clock, days=days, zone=zone)
    scheduler = _make_scheduler(
        tmp_settings, clock=clock, retention=retention, notifiers=notifiers, db=db
    )
    return scheduler, retention, {
        "old_todo": old_todo,
        "old_message": old_message,
        "pending_todo": pending_todo,
        "pending_message": pending_message,
    }


def test_scheduler_purges_after_settlement(
    db, manual_clock, tmp_settings, make_recording_notifier
):
    """推进时钟跨过触发时刻 → 汇总结算 → 过期数据被删。"""
    notifier = make_recording_notifier("recording")
    scheduler, _, ids = _scheduler_fixture(
        db, manual_clock, tmp_settings, notifiers=(notifier,)
    )
    manual_clock.set(LOCAL_TRIGGER)

    with _capture(_retention_logger()) as records:
        result = scheduler.run_once()

    assert result == 1
    from notify_hub.services.digest import DigestService

    assert DigestService(db, manual_clock).state_for(TODAY).delivered is True
    assert ids["old_todo"] not in _todo_ids(db)
    assert ids["pending_todo"] in _todo_ids(db), "pending 待办不得被回收"
    assert ids["pending_message"] in _message_ids(db)
    assert ids["old_message"] not in _message_ids(db), (
        "old_message 的唯一引用者已被清掉且自身超期 → 按 §3 第 2 步必须被删；"
        "pending 那条消息的存活由上一行断言负责"
    )
    infos = [r for r in records if r.levelno == logging.INFO]
    assert len(infos) >= 1, "回收确实发生了，应当有一条 INFO 汇总日志"


def test_scheduler_does_not_purge_before_trigger_time(db, manual_clock, tmp_settings):
    """时钟早于触发时刻 → 数据一条不少（也不结算）。"""
    scheduler, _, ids = _scheduler_fixture(db, manual_clock, tmp_settings)
    manual_clock.set(LOCAL_NOW)  # 北京时间 10:00，早于 21:00

    with _capture(_retention_logger()) as records:
        result = scheduler.run_once()

    assert result == 0
    assert ids["old_todo"] in _todo_ids(db)
    assert records == [], "未结算就不得触发回收"


def test_scheduler_does_not_purge_again_when_settled(
    db, manual_clock, tmp_settings, make_recording_notifier
):
    """当天已定案 → 第二轮不再回收，也不产生第二条回收日志。"""
    notifier = make_recording_notifier("recording")
    scheduler, _, ids = _scheduler_fixture(
        db, manual_clock, tmp_settings, notifiers=(notifier,)
    )
    manual_clock.set(LOCAL_TRIGGER)

    with _capture(_retention_logger()) as first_records:
        first = scheduler.run_once()
    with _capture(_retention_logger()) as second_records:
        second = scheduler.run_once()

    assert first == 1
    assert second == 0, "当天已定案必须早退"
    assert ids["old_todo"] not in _todo_ids(db)
    assert len([r for r in first_records if r.levelno == logging.INFO]) >= 1
    assert [r for r in second_records if r.levelno == logging.INFO] == []


def test_scheduler_does_not_purge_when_digest_fails(
    db, manual_clock, tmp_settings, make_recording_notifier
):
    """投递失败（未定案）→ 不回收。"""
    failing = make_recording_notifier("recording", ok=False)
    scheduler, retention, ids = _scheduler_fixture(db, manual_clock, tmp_settings)
    # 换成会失败的渠道重建调度器（同一批数据）。
    scheduler = _make_scheduler(
        tmp_settings, clock=manual_clock, retention=retention, notifiers=[failing], db=db
    )
    manual_clock.set(LOCAL_TRIGGER)

    with _capture(_retention_logger()) as records:
        result = scheduler.run_once()

    assert result == 0
    assert ids["old_todo"] in _todo_ids(db), "汇总未定案时不得回收"
    assert records == []


def test_retention_exception_does_not_break_digest_or_return_value(
    db, manual_clock, tmp_settings, make_recording_notifier
):
    """异常场景①：回收抛异常 → ``run_once`` 不抛出、返回值不变、调度器不崩。"""
    zone = tmp_settings.reminders.zone
    manual_clock.set(T0)
    old = T0 - timedelta(days=100)
    message_id = _make_message(db, received_at=old, dedup_key="old-1")
    todo_id = _make_todo(db, message_id, dedup_key="old-1")
    fake = _FakeRetention(raise_exc=RuntimeError("模拟回收故障"))
    notifier = make_recording_notifier("recording")
    scheduler = _make_scheduler(
        tmp_settings, clock=manual_clock, retention=fake, notifiers=[notifier], db=db
    )
    manual_clock.set(LOCAL_TRIGGER)

    with _capture(LOG) as records:
        result = scheduler.run_once()

    assert result == 1, "回收失败绝不能改变汇总的返回语义"
    assert fake.calls == 1
    assert any(r.levelno == logging.WARNING for r in records), "异常必须打一条 WARNING"
    from notify_hub.services.digest import DigestService

    assert DigestService(db, manual_clock).state_for(TODAY).delivered is True
    assert todo_id in _todo_ids(db), "假 retention 什么都没删"
    assert notifier.sent, "汇总本身仍然发出去了"


def test_scheduler_settles_but_deletes_nothing_when_days_zero(
    db, manual_clock, tmp_settings, make_recording_notifier
):
    """异常场景②：``days=0`` 时调度器照样结算，但一条数据都不删。"""
    notifier = make_recording_notifier("recording")
    scheduler, _, ids = _scheduler_fixture(
        db, manual_clock, tmp_settings, days=0, notifiers=(notifier,)
    )
    manual_clock.set(LOCAL_TRIGGER)

    result = scheduler.run_once()

    assert result == 1
    from notify_hub.services.digest import DigestService

    assert DigestService(db, manual_clock).state_for(TODAY).delivered is True
    assert ids["old_todo"] in _todo_ids(db), "days=0 关闭回收"
    assert ids["old_message"] in _message_ids(db)


def test_scheduler_retention_is_optional_keyword(db, manual_clock, tmp_settings):
    """§4.4：``retention`` 可空（``None`` 时跳过回收，其余行为不变）。"""
    zone = tmp_settings.reminders.zone
    manual_clock.set(T0)
    old = T0 - timedelta(days=100)
    message_id = _make_message(db, received_at=old, dedup_key="old-1")
    todo_id = _make_todo(db, message_id, status=TodoStatus.DONE.value, completed_at=old)
    scheduler = _make_scheduler(tmp_settings, clock=manual_clock, retention=None, db=db)
    manual_clock.set(LOCAL_TRIGGER)

    result = scheduler.run_once()

    assert result == 0, "没有 pending 待办 → 空待办分支，定案但返回 0"
    from notify_hub.services.digest import DigestService

    assert DigestService(db, manual_clock).state_for(TODAY).delivered is True
    assert todo_id in _todo_ids(db), "retention=None 时必须跳过回收"
    assert _service(db, manual_clock, days=DAYS, zone=zone).enabled is True
