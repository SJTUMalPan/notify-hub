"""M2（数据模型与持久化）模块测试。

**输入**：``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 6 节「模块 M2 / 4. 验证方法」
（外加 1.2 时间约定、1.3 已实测事实、2 节接口定义）。实现（``src/notify_hub/db.py``、
``models.py``）由另一个子代理交付，本文件写作时不存在。

**导入策略**：实现模块**只在函数体内惰性导入**（与 ``tests/conftest.py`` 的约定一致）。
这样 pytest 能成功收集本文件；实现缺失时的失败全部发生在运行时
（``ModuleNotFoundError: notify_hub.db``），而不是收集阶段。

本文件不依赖 ``conftest.py`` 的 ``db`` / ``ctx`` fixture（那些依赖 M1/M4，尚未实现），
而是按规格的构造方式直接 ``Database(tmp_path / "x.db")``。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from notify_hub.clock import as_utc
from notify_hub.domain import AckReason, DeliveryEvent, Level, TodoEventKind, TodoStatus

UTC = timezone.utc
T0 = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)

TABLES = {"messages", "todos", "deliveries", "todo_events"}


# --------------------------------------------------------------------------- #
# 惰性导入与构造辅助
# --------------------------------------------------------------------------- #
def _load():
    """惰性导入 M2 的实现。实现缺失时在此抛 ModuleNotFoundError（运行期，非收集期）。"""
    from notify_hub.db import Database  # 实现缺失 -> ModuleNotFoundError: notify_hub.db
    from notify_hub import models  # noqa: E402  (同属 M2 实现，一并惰性导入)

    return Database, models


def _env(tmp_path: Path, name: str = "x.db"):
    """规格的构造方式：``Database(tmp_path / "x.db")`` + ``init_schema()``。"""
    Database, models = _load()
    database = Database(tmp_path / name)
    database.init_schema()
    return database, models


def _message(models, **overrides):
    values = {
        "source": "svc",
        "title": "title",
        "body": None,
        "level": Level.INFO.value,
        "need_ack_declared": False,
        "dedup_key": None,
        "occurred_at": T0,
        "received_at": T0,
        "meta": {},
        "labels": [],
        "rule_id": None,
        "category": None,
        "needs_ack": None,
        "ack_reason": None,
        "preferred_channel": None,
    }
    values.update(overrides)
    return models.Message(**values)


def _todo(models, message_id, **overrides):
    values = {
        "message_id": message_id,
        "source": "svc",
        "dedup_key": None,
        "category": None,
        "title": "title",
        "status": TodoStatus.PENDING.value,
        "ack_reason": AckReason.CALLER_DECLARED.value,
        "preferred_channel": None,
        "created_at": T0,
        "first_notified_at": T0,
        "last_notified_at": T0,
        "reminder_count": 0,
        "completed_at": None,
    }
    values.update(overrides)
    return models.Todo(**values)


def _delivery(models, **overrides):
    values = {
        "message_id": None,
        "todo_id": None,
        "channel_id": "ch",
        "attempted_at": T0,
        "ok": True,
        "error_reason": None,
        "receipt": None,
        "is_preferred": True,
        "is_fallback": False,
        "fallback_reason": None,
        "event": DeliveryEvent.FIRST_NOTICE.value,
    }
    values.update(overrides)
    return models.DeliveryRecord(**values)


def _todo_event(models, todo_id, **overrides):
    values = {
        "todo_id": todo_id,
        "kind": TodoEventKind.CREATED.value,
        "occurred_at": T0,
        "detail": None,
        "channel_id": None,
        "delivery_ok": None,
    }
    values.update(overrides)
    return models.TodoEvent(**values)


def _insert(db, obj) -> int:
    """在 ``db.session()`` 中插入并返回主键（flush 取 id，退出时由上下文管理器提交）。"""
    with db.session() as session:
        session.add(obj)
        session.flush()
        return obj.id


def _count(db, entity) -> int:
    with db.session() as session:
        return len(session.scalars(select(entity)).all())


# --------------------------------------------------------------------------- #
# 1. 建表
# --------------------------------------------------------------------------- #
def test_init_schema_creates_the_four_tables(tmp_path):
    database, _models = _env(tmp_path)
    names = set(inspect(database.engine).get_table_names())
    assert TABLES <= names, names


# --------------------------------------------------------------------------- #
# 2. 幂等（tasks 2.4）
# --------------------------------------------------------------------------- #
def test_init_schema_is_idempotent_and_keeps_data(tmp_path):
    database, models = _env(tmp_path)
    _insert(database, _message(models, source="keep"))

    database.init_schema()
    database.init_schema()

    assert _count(database, models.Message) == 1
    # 注意：SQLAlchemy 提交后属性会过期，属性访问必须在 session 上下文内完成
    with database.session() as session:
        rows = session.scalars(select(models.Message)).all()
        assert [row.source for row in rows] == ["keep"]


# --------------------------------------------------------------------------- #
# 3. 消息往返（tasks 2.1）
# --------------------------------------------------------------------------- #
def test_message_round_trip_of_json_and_scalar_columns(tmp_path):
    database, models = _env(tmp_path)
    message_id = _insert(
        database,
        _message(
            models,
            source="db-backup",
            title="备份失败",
            body="exit code 1",
            level=Level.ERROR.value,
            need_ack_declared=True,
            dedup_key="dk-1",
            meta={"k": [1, 2]},
            labels=["a", "b"],
            rule_id="backup-failure",
            category="backup-failure",
            needs_ack=True,
            ack_reason=AckReason.RULE.value,
            preferred_channel="email",
        ),
    )

    with database.session() as session:
        row = session.get(models.Message, message_id)
        assert row.id == message_id
        assert row.source == "db-backup"
        assert row.title == "备份失败"
        assert row.body == "exit code 1"
        assert row.level == Level.ERROR.value
        assert row.need_ack_declared is True
        assert row.dedup_key == "dk-1"
        # JSON 列（meta -> meta_json / labels -> labels_json）结构相等
        assert row.meta == {"k": [1, 2]}
        assert row.labels == ["a", "b"]
        assert row.rule_id == "backup-failure"
        assert row.category == "backup-failure"
        assert row.needs_ack is True
        assert row.ack_reason == AckReason.RULE.value
        assert row.preferred_channel == "email"


def test_message_round_trip_of_nullable_columns(tmp_path):
    database, models = _env(tmp_path)
    message_id = _insert(database, _message(models, source="minimal"))

    with database.session() as session:
        row = session.get(models.Message, message_id)
        assert row.body is None
        assert row.dedup_key is None
        assert row.rule_id is None
        assert row.category is None
        assert row.needs_ack is None
        assert row.ack_reason is None
        assert row.preferred_channel is None
        assert row.meta == {}
        assert row.labels == []


# --------------------------------------------------------------------------- #
# 4. 时间往返（1.2 约定：SQLite 丢时区，读出必须经 as_utc 归一化）
# --------------------------------------------------------------------------- #
def test_aware_utc_datetimes_round_trip_through_as_utc(tmp_path):
    database, models = _env(tmp_path)
    occurred_at = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)
    received_at = datetime(2024, 5, 1, 12, 0, 30, 123456, tzinfo=UTC)

    message_id = _insert(
        database,
        _message(models, occurred_at=occurred_at, received_at=received_at),
    )
    todo_id = _insert(
        database,
        _todo(
            models,
            message_id,
            source="s",
            dedup_key="k",
            created_at=occurred_at,
            first_notified_at=received_at,
            last_notified_at=received_at,
        ),
    )
    _insert(
        database,
        _todo_event(models, todo_id, kind=TodoEventKind.CREATED.value, occurred_at=occurred_at),
    )

    with database.session() as session:
        message = session.get(models.Message, message_id)
        todo = session.get(models.Todo, todo_id)
        event = session.scalars(select(models.TodoEvent)).one()

        assert as_utc(message.occurred_at) == occurred_at
        assert as_utc(message.received_at) == received_at
        # 归一化之后才能参与算术（微秒精度也不得丢失）
        assert (as_utc(message.received_at) - as_utc(message.occurred_at)) == timedelta(
            seconds=30, microseconds=123456
        )
        assert as_utc(todo.created_at) == occurred_at
        assert as_utc(todo.first_notified_at) == received_at
        assert as_utc(todo.last_notified_at) == received_at
        assert as_utc(event.occurred_at) == occurred_at


# --------------------------------------------------------------------------- #
# 5. 部分唯一索引（tasks 2.2）
# --------------------------------------------------------------------------- #
def test_partial_unique_index_rejects_second_pending_todo(tmp_path):
    database, models = _env(tmp_path)
    first_message = _insert(database, _message(models, source="s", dedup_key="k"))
    second_message = _insert(database, _message(models, source="s", dedup_key="k"))
    _insert(database, _todo(models, first_message, source="s", dedup_key="k"))

    with pytest.raises(IntegrityError):
        _insert(database, _todo(models, second_message, source="s", dedup_key="k"))

    assert _count(database, models.Todo) == 1


def test_null_dedup_key_is_never_constrained(tmp_path):
    database, models = _env(tmp_path)
    first_message = _insert(database, _message(models, source="s"))
    second_message = _insert(database, _message(models, source="s"))

    _insert(database, _todo(models, first_message, source="s", dedup_key=None))
    _insert(database, _todo(models, second_message, source="s", dedup_key=None))

    assert _count(database, models.Todo) == 2


def test_completed_todo_frees_the_dedup_key(tmp_path):
    database, models = _env(tmp_path)
    first_message = _insert(database, _message(models, source="s", dedup_key="k"))
    second_message = _insert(database, _message(models, source="s", dedup_key="k"))
    first_todo = _insert(database, _todo(models, first_message, source="s", dedup_key="k"))

    with database.session() as session:
        row = session.get(models.Todo, first_todo)
        row.status = TodoStatus.DONE.value
        row.completed_at = T0 + timedelta(minutes=5)

    _insert(database, _todo(models, second_message, source="s", dedup_key="k"))

    with database.session() as session:
        statuses = sorted(row.status for row in session.scalars(select(models.Todo)).all())
    assert statuses == [TodoStatus.DONE.value, TodoStatus.PENDING.value]


def test_dedup_key_uniqueness_is_scoped_to_source(tmp_path):
    database, models = _env(tmp_path)
    first_message = _insert(database, _message(models, source="s"))
    second_message = _insert(database, _message(models, source="s2"))

    _insert(database, _todo(models, first_message, source="s", dedup_key="k"))
    _insert(database, _todo(models, second_message, source="s2", dedup_key="k"))

    assert _count(database, models.Todo) == 2


def test_todos_partial_unique_index_ddl_has_where_clause(tmp_path):
    """规格「完成后必须成立」：``.schema todos`` 中出现带 WHERE 的唯一索引。"""
    database, _models = _env(tmp_path)
    with database.engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'todos' AND sql IS NOT NULL"
        ).all()
    statements = [" ".join(str(row[0]).lower().split()) for row in rows]

    def matches(statement: str) -> bool:
        return (
            "unique" in statement
            and re.search(r"dedup_key\s+is\s+not\s+null", statement) is not None
            and re.search(r"status\s*=\s*'pending'", statement) is not None
        )

    assert any(matches(statement) for statement in statements), statements


# --------------------------------------------------------------------------- #
# 6. Todo.message_id 唯一
# --------------------------------------------------------------------------- #
def test_todo_message_id_is_unique(tmp_path):
    database, models = _env(tmp_path)
    message_id = _insert(database, _message(models, source="s"))

    _insert(database, _todo(models, message_id, source="s", dedup_key=None))

    with pytest.raises(IntegrityError):
        # dedup_key 为 NULL，部分唯一索引不适用；此处唯一可能的冲突是 message_id
        _insert(database, _todo(models, message_id, source="s2", dedup_key=None))

    assert _count(database, models.Todo) == 1


def test_todo_defaults_are_pending_and_zero_reminders(tmp_path):
    database, models = _env(tmp_path)
    message_id = _insert(database, _message(models))
    with database.session() as session:
        session.add(
            models.Todo(
                message_id=message_id,
                source="s",
                title="t",
                ack_reason=AckReason.RULE.value,
                created_at=T0,
                first_notified_at=T0,
                last_notified_at=T0,
            )
        )
        session.flush()
        todo_id = session.scalars(select(models.Todo)).one().id

    with database.session() as session:
        row = session.get(models.Todo, todo_id)
        assert row.status == TodoStatus.PENDING.value
        assert row.reminder_count == 0


# --------------------------------------------------------------------------- #
# 7. 关联查询（tasks 2.3）
# --------------------------------------------------------------------------- #
def test_related_rows_are_queryable_and_time_ordered(tmp_path):
    database, models = _env(tmp_path)
    message_id = _insert(database, _message(models, source="svc"))
    todo_id = _insert(database, _todo(models, message_id, source="svc", dedup_key="k"))

    msg_t0 = T0
    msg_t1 = T0 + timedelta(seconds=10)
    todo_t0 = T0 + timedelta(seconds=20)
    todo_t1 = T0 + timedelta(seconds=30)
    done_at = T0 + timedelta(seconds=40)

    _insert(
        database,
        _delivery(
            models,
            message_id=message_id,
            channel_id="ch1",
            attempted_at=msg_t0,
            event=DeliveryEvent.FIRST_NOTICE.value,
            receipt="r1",
        ),
    )
    _insert(
        database,
        _delivery(
            models,
            message_id=message_id,
            channel_id="ch2",
            attempted_at=msg_t1,
            ok=False,
            error_reason="投递失败",
            is_preferred=False,
            is_fallback=True,
            fallback_reason="首选渠道失败",
            event=DeliveryEvent.FIRST_NOTICE.value,
        ),
    )
    _insert(
        database,
        _delivery(
            models,
            todo_id=todo_id,
            channel_id="ch1",
            attempted_at=todo_t0,
            event=DeliveryEvent.REMINDER.value,
        ),
    )
    _insert(
        database,
        _delivery(
            models,
            todo_id=todo_id,
            channel_id="ch2",
            attempted_at=todo_t1,
            event=DeliveryEvent.REMINDER.value,
        ),
    )
    for kind, moment in (
        (TodoEventKind.CREATED.value, msg_t0),
        (TodoEventKind.REMINDER.value, todo_t0),
        (TodoEventKind.COMPLETED.value, done_at),
    ):
        _insert(database, _todo_event(models, todo_id, kind=kind, occurred_at=moment))

    with database.session() as session:
        message_deliveries = session.scalars(
            select(models.DeliveryRecord)
            .where(models.DeliveryRecord.message_id == message_id)
            .order_by(models.DeliveryRecord.attempted_at)
        ).all()
        todo_deliveries = session.scalars(
            select(models.DeliveryRecord)
            .where(models.DeliveryRecord.todo_id == todo_id)
            .order_by(models.DeliveryRecord.attempted_at)
        ).all()
        events = session.scalars(
            select(models.TodoEvent)
            .where(models.TodoEvent.todo_id == todo_id)
            .order_by(models.TodoEvent.occurred_at)
        ).all()

        assert [row.channel_id for row in message_deliveries] == ["ch1", "ch2"]
        assert [as_utc(row.attempted_at) for row in message_deliveries] == [msg_t0, msg_t1]
        assert message_deliveries[0].ok is True
        assert message_deliveries[1].ok is False
        assert message_deliveries[1].error_reason == "投递失败"
        assert message_deliveries[1].is_fallback is True
        assert message_deliveries[1].fallback_reason == "首选渠道失败"

        assert [row.channel_id for row in todo_deliveries] == ["ch1", "ch2"]
        assert [as_utc(row.attempted_at) for row in todo_deliveries] == [todo_t0, todo_t1]
        assert [row.event for row in todo_deliveries] == [
            DeliveryEvent.REMINDER.value,
            DeliveryEvent.REMINDER.value,
        ]

        assert [event.kind for event in events] == [
            TodoEventKind.CREATED.value,
            TodoEventKind.REMINDER.value,
            TodoEventKind.COMPLETED.value,
        ]
        assert [as_utc(event.occurred_at) for event in events] == [msg_t0, todo_t0, done_at]


def test_todo_event_json_detail_round_trip(tmp_path):
    database, models = _env(tmp_path)
    message_id = _insert(database, _message(models))
    todo_id = _insert(database, _todo(models, message_id, source="s", dedup_key="k"))
    _insert(
        database,
        _todo_event(
            models,
            todo_id,
            kind=TodoEventKind.REMINDER.value,
            detail={"attempt": 2, "channels": ["ch1"]},
            channel_id="ch1",
            delivery_ok=False,
        ),
    )

    with database.session() as session:
        event = session.scalars(select(models.TodoEvent)).one()
        assert event.detail == {"attempt": 2, "channels": ["ch1"]}
        assert event.channel_id == "ch1"
        assert event.delivery_ok is False


# --------------------------------------------------------------------------- #
# 8. session() 异常语义
# --------------------------------------------------------------------------- #
def test_session_rolls_back_and_propagates_on_exception(tmp_path):
    database, models = _env(tmp_path)

    with pytest.raises(RuntimeError, match="boom"):
        with database.session() as session:
            session.add(_message(models, source="tx"))
            session.flush()
            raise RuntimeError("boom")

    assert _count(database, models.Message) == 0


# --------------------------------------------------------------------------- #
# 异常场景 (b)：父目录不存在
# --------------------------------------------------------------------------- #
def test_init_schema_creates_missing_parent_directory(tmp_path):
    Database, _models = _load()
    db_path = tmp_path / "nested" / "deep" / "x.db"
    assert not db_path.parent.exists()

    database = Database(db_path)
    database.init_schema()

    assert db_path.parent.is_dir()
    assert db_path.exists()
    assert TABLES <= set(inspect(database.engine).get_table_names())
