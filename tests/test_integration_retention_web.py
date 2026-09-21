"""作用域 2 集成测试：回收引擎 × 调度器 × 真实 SQLite × 网页（architecture.md §6）。

**本文件不打任何内部桩。** 模块测试（``test_retention.py`` / ``test_web_recent_completed.py``）
在冻结接口上打桩，证明不了「模块之间真的接上」；这里只走真货：

1. **调度器 → 真实 DB → 网页**：``ManualClock`` 推进跨过本地触发时刻，让**真实**
   ``ReminderScheduler.run_once()`` 完成当天结算并触发回收；随后用**真实**
   ``create_web_app(ctx)`` 取页面，断言被回收的记录确实不再出现（主表与「最近完成」栏目）。
2. **回收 → 投递记录账本完整性（D4）**：删除一条已完成待办后，其消息详情页的「投递记录」
   **行数不变**——``deliveries.todo_id`` 被解绑但行还在。
   按 §4.2 的明文，这个承诺只能在 ``purge_completed_older_than`` 路径上观察
   （``purge_expired`` 第 2 步会把消息连同记录一起删），所以本条走
   ``POST /todos/purge-completed`` 那条路径。
3. **pending 保护**：跨越保留期后，pending 待办与其消息在网页与库里都仍在。
4. **幂等**：第二次回收报告为空（``purge_expired`` 的返回值 + 第二条结算周期不产生删除）。
5. **生产装配 §4.4**：``build_context`` 造出来的调度器真的会回收——``retention`` 参数可空，
   接线漏传会**静默关闭**回收，这是唯一能发现该故障的测试。

时间基准（``settings.reminders``：``at=21:00`` / ``Asia/Shanghai``，保留期缺省 30 天）::

    TODAY_LOCAL  = 2024-01-01 10:00 +08:00  ==  2024-01-01T02:00Z   （时钟起点）
    TRIGGER      = 2024-01-01 21:00 +08:00  ==  2024-01-01T13:00Z   （advance(11h)）
    cutoff       = 2023-12-02（本地自然日）

**历史记录一律由真货产出**：把 ``ManualClock`` 倒拨到过去，让真实
``POST /api/v1/messages``（IngestPipeline → classifier → MessageService/TodoService → SQLite）
与真实 Web 完成表单产出「过去的消息 / 待办 / 完成时间 / ``todo_events``」，
再把时钟拨回今天。唯一的直接插入是**消息详情页要审计的历史投递记录**——
它由渠道适配器的 ``send()`` 产出，测试里没有真实渠道可发（``default_channel: null``），
只能按 ``models.DeliveryRecord`` 的结构补造，这属于夹具数据而非模块间替身。

禁止真实等待：全部时间由 ``ManualClock`` 驱动；所有 app 都用 ``create_web_app`` /
``create_api_app``（**不启动后台线程**），调度由测试显式调用 ``run_once()``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from notify_hub.clock import ManualClock, as_utc
from notify_hub.config import load_settings
from notify_hub.context import build_context, build_test_context

#: 时钟起点：北京时间（Asia/Shanghai，固定 +08:00）2024-01-01 10:00。
TODAY_LOCAL = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone(timedelta(hours=8)))
TZ = timezone(timedelta(hours=8))
#: 本地触发时刻（``reminders.at = "21:00"``）。
TRIGGER_LOCAL = datetime(2024, 1, 1, 21, 0, 0, tzinfo=TZ)
#: 今天（本地自然日）。
TODAY = date(2024, 1, 1)
#: 倒拨到过去 61 天，让真货产出的记录稳稳早于 ``cutoff = 今天 - 30``。
PAST_BACK = timedelta(days=61)
#: 倒拨到过去 2 天，让真货产出的记录留在保留期之内（早于今天，但晚于 cutoff）。
IN_WINDOW_BACK = timedelta(days=2)

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


# --------------------------------------------------------------------------- #
# 真实配置装配（``load_settings``，与 conftest 的 tmp_settings 同形）
# --------------------------------------------------------------------------- #
def _make_settings(tmp_path: Path, *, retention_days: int | None = None):
    """真实 ``load_settings``：无渠道、``default_channel: null``、秒级扫描周期。"""
    (tmp_path / "rules.yaml").write_text(
        yaml.safe_dump(_MINIMAL_RULES, allow_unicode=True), encoding="utf-8"
    )
    config = {
        "server": {"host": "127.0.0.1", "port": 0, "log_level": "WARNING"},
        "storage": {"db_path": "./data/notify.db"},
        "rules": {"path": "./rules.yaml", "poll_interval_seconds": 5},
        "reminders": {
            "at": "21:00",
            "timezone": "Asia/Shanghai",
            "scan_interval_seconds": 1,
        },
        "default_channel": None,
        "channels": [],
    }
    if retention_days is not None:
        config["retention"] = {"days": retention_days}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return load_settings(config_path)


@pytest.fixture
def manual_clock() -> ManualClock:
    """起点＝本地 2024-01-01 10:00（早于触发时刻，不会意外结算）。"""
    return ManualClock(TODAY_LOCAL)


@pytest.fixture
def settings(tmp_path: Path):
    return _make_settings(tmp_path)


def _context(settings, clock, notifiers=()):
    """测试装配（``run_inline=True``，首次通知同步完成，断言确定）。"""
    from notify_hub.notifiers import NotifierRegistry

    registry = NotifierRegistry()
    for notifier in notifiers:
        registry.register(notifier)
    return build_test_context(settings, clock=clock, registry=registry)


@pytest.fixture
def ctx(settings, manual_clock, recording_notifier):
    """真实组合根 + 一个记录型渠道（让日汇总能成功结算，从而走到回收那一步）。"""
    return _context(settings, manual_clock, [recording_notifier])


@pytest.fixture
def web(ctx):
    """真实 ``create_web_app(ctx)``（不启动任何后台线程），``follow_redirects=False``。"""
    from notify_hub.web import create_web_app

    with TestClient(create_web_app(ctx), follow_redirects=False) as client:
        yield client


@pytest.fixture
def api(ctx):
    """真实 ``create_api_app(ctx)``（不启动任何后台线程）——受理入口。"""
    from notify_hub.api import create_api_app

    with TestClient(create_api_app(ctx)) as client:
        yield client


# --------------------------------------------------------------------------- #
# 真实入口的动作
# --------------------------------------------------------------------------- #
def _ingest(client, *, title: str, dedup_key: str) -> tuple[int, int]:
    """经真实 HTTP 受理一条需要待办的消息；返回 ``(message_id, todo_id)``。"""
    response = client.post(
        "/api/v1/messages",
        json={
            "source": "cron",
            "title": title,
            "body": f"{title} 的正文",
            "level": "error",
            "need_ack": True,
            "dedup_key": dedup_key,
        },
    )
    assert response.status_code == 202, response.text[:500]
    body = response.json()
    assert isinstance(body["todo_id"], int), body
    return body["message_id"], body["todo_id"]


def _complete(client, todo_id: int) -> None:
    """点真实列表页上的那个完成表单（硬编码冻结路由，不解析 HTML）。"""
    response = client.post(f"/todos/{todo_id}/done", data={})
    assert response.status_code == 303, response.text[:500]


def _reclaim(web) -> None:
    """点真实列表页上的「清空已完成」表单；返回前先断言 303 → /todos。"""
    response = web.post("/todos/purge-completed", data={})
    assert response.status_code == 303, response.text[:500]
    assert response.headers["location"] == "/todos"


# --------------------------------------------------------------------------- #
# 只读的库内复核（新开 session 读真实 SQLite 文件，不看返回值）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Db:
    todos: dict[str, str]  # title -> status
    todo_ids: dict[str, int]  # title -> id
    message_ids: set[int]
    delivery_rows: dict[int, int | None]  # delivery id -> todo_id
    todo_event_ids: set[int]
    digest_dates: set[date]


def _snapshot(ctx) -> _Db:
    """从真实数据库读出整个相关状态（``ctx.db.session()`` 是新连接）。"""
    from sqlmodel import select

    from notify_hub.models import DeliveryRecord, DigestRun, Message, Todo, TodoEvent

    with ctx.db.session() as session:
        todos = list(session.exec(select(Todo)).all())
        messages = list(session.exec(select(Message)).all())
        deliveries = list(session.exec(select(DeliveryRecord)).all())
        events = list(session.exec(select(TodoEvent)).all())
        digests = list(session.exec(select(DigestRun)).all())
    return _Db(
        todos={row.title: row.status for row in todos},
        todo_ids={row.title: row.id for row in todos},
        message_ids={row.id for row in messages},
        delivery_rows={row.id: row.todo_id for row in deliveries},
        todo_event_ids={row.id for row in events},
        digest_dates={row.local_date for row in digests},
    )


def _delivery_ids_for(ctx, message_id: int) -> set[int]:
    """真实库内查某条消息名下的投递记录 id（消息详情页「投递记录」的账本）。"""
    from sqlmodel import select

    from notify_hub.models import DeliveryRecord

    with ctx.db.session() as session:
        rows = session.exec(
            select(DeliveryRecord).where(DeliveryRecord.message_id == message_id)
        ).all()
        return {row.id for row in rows}


def _sync_delivery_row(ctx, message_id: int, todo_id: int) -> None:
    """补造一条历史投递记录（消息详情页的审计数据）。

    **为什么必须直接插**：投递记录只能由渠道适配器的 ``send()`` 产出，而本套配置
    ``default_channel: null``、零渠道（避免后台线程制造干扰性投递），没有真实渠道可发。
    这里补的是夹具数据，不是模块间替身——它落在消息详情页要审计的那张表里。
    """
    from notify_hub.models import DeliveryRecord

    with ctx.db.session() as session:
        session.add(
            DeliveryRecord(
                message_id=message_id,
                todo_id=todo_id,
                channel_id="recording",
                attempted_at=ctx.clock.now(),
                ok=True,
                receipt=f"receipt-{todo_id}",
                event="first_notice",
            )
        )


def _add_digest_run(ctx, local_date: date) -> None:
    """补造一条历史汇总状态行（自动回收第 3 步的对象）。"""
    from notify_hub.models import DigestRun

    with ctx.db.session() as session:
        session.add(
            DigestRun(
                local_date=local_date,
                checked_at=ctx.clock.now(),
                fired_at=ctx.clock.now(),
                todo_count=0,
                delivered=True,
                attempts=1,
            )
        )


def _detail_row_count(html: str) -> int:
    """消息详情页「投递记录」表体的行数（按冻结的表头切段后数 ``<tr>``）。"""
    section = html.split("投递记录", 1)
    assert len(section) == 2, "详情页缺少「投递记录」区块"
    body = section[1].split("</table>", 1)[0].split("<tbody>", 1)[-1]
    return len(re.findall(r"<tr[ >]", body))


def _cross_trigger(manual_clock: ManualClock) -> None:
    """把时钟推过本地触发时刻（不真实等待）。"""
    manual_clock.advance((TRIGGER_LOCAL - TODAY_LOCAL).total_seconds())


# --------------------------------------------------------------------------- #
# 接缝 1：调度器 → 真实 DB → 网页
# --------------------------------------------------------------------------- #
def test_scheduler_settlement_purges_and_page_stops_showing_record(ctx, manual_clock, web, api):
    """真实调度器结算后触发回收；随后真实网页上被回收的记录不再出现。

    两条记录都由**真货**产出（倒拨时钟受理 + 真实完成表单），跨越触发时刻后
    ``purge_expired`` 删掉 61 天前的那条，保留 1 天前的那条。
    """
    # 1. 真货产出：61 天前受理并完成一条 —— 已越过保留期
    manual_clock.advance(-PAST_BACK.total_seconds())
    old_message, old_todo = _ingest(api, title="旧备份失败", dedup_key="old-backup")
    _complete(web, old_todo)

    # 2. 真货产出：2 天前受理并完成一条 —— 仍在保留期内（早于今天，但晚于 cutoff）
    manual_clock.set(TODAY_LOCAL - IN_WINDOW_BACK)
    recent_message, recent_todo = _ingest(api, title="近期备份失败", dedup_key="recent-backup")
    _complete(web, recent_todo)

    # 3. 回到今天
    manual_clock.set(TODAY_LOCAL)
    before = _snapshot(ctx)
    assert set(before.todos) == {"旧备份失败", "近期备份失败"}
    assert before.todos["旧备份失败"] == "done"
    assert {old_message, recent_message} <= before.message_ids

    # 未到触发时刻：真实调度器必须什么都不做
    assert ctx.scheduler.run_once() == 0
    assert set(_snapshot(ctx).todos) == {"旧备份失败", "近期备份失败"}

    # 4. 跨过触发时刻：真实结算（无 pending → 空待办分支）→ 触发真实回收
    _cross_trigger(manual_clock)
    assert ctx.scheduler.run_once() == 0
    assert ctx.digest.state_for(TODAY) is not None, "当天未结算，回收不可能被触发"

    after = _snapshot(ctx)

    # 5. 库里：旧待办与其事件没了，近期待办还在
    assert "旧备份失败" not in after.todos
    assert after.todos["近期备份失败"] == "done"
    assert before.todo_event_ids - after.todo_event_ids, "夹具应有 todo_events 被回收"

    # 6. 网页（真实 create_web_app(ctx)）：被回收的记录确实不再出现
    all_page = web.get("/todos?status=all")
    assert all_page.status_code == 200
    assert "旧备份失败" not in all_page.text
    assert "近期备份失败" in all_page.text

    # 被回收待办的详情页 404；保留期内的仍可访问
    assert web.get(f"/todos/{old_todo}").status_code == 404
    recent_page = web.get(f"/todos/{recent_todo}")
    assert recent_page.status_code == 200
    assert "近期备份失败" in recent_page.text

    # 第 2 步（删消息）：旧消息已从真实库里删掉，保留期内的消息仍在
    assert old_message not in after.message_ids
    assert recent_message in after.message_ids
    assert web.get(f"/messages/{old_message}").status_code == 404


# --------------------------------------------------------------------------- #
# 接缝 2：回收 → 投递记录账本完整性（D4，只走 purge_completed_older_than）
# --------------------------------------------------------------------------- #
def test_purge_completed_keeps_delivery_ledger_rows(ctx, manual_clock, web, api):
    """删除已完成待办后，消息详情页的「投递记录」行数不变（``todo_id`` 解绑、行仍在）。

    按 §4.2：这条承诺**只能**在 ``purge_completed_older_than``（网页按钮那条路径）上
    观察；``purge_expired`` 第 2 步会把消息连同记录一起删，构造不出来。
    """
    # 真货产出：61 天前受理并完成一条。真实受理本身会写一条「无可用渠道」的投递记录
    # （``default_channel: null`` 且零渠道），另加 2 条历史投递记录构成审计账本。
    manual_clock.advance(-PAST_BACK.total_seconds())
    message_id, todo_id = _ingest(api, title="旧投递待办", dedup_key="old-ledger")
    _complete(web, todo_id)
    _sync_delivery_row(ctx, message_id, todo_id)
    _sync_delivery_row(ctx, message_id, todo_id)
    manual_clock.set(TODAY_LOCAL)

    before = _snapshot(ctx)
    ledger_ids = _delivery_ids_for(ctx, message_id)
    before_rows = _detail_row_count(web.get(f"/messages/{message_id}").text)
    assert before_rows == len(ledger_ids) >= 3, (before_rows, ledger_ids)
    assert ledger_ids <= set(before.delivery_rows)

    # 走真实网页按钮（cutoff = 今天）
    _reclaim(web)

    # 1. 账本行数不变 —— D4 的核心承诺
    after_html = web.get(f"/messages/{message_id}").text
    assert after_html  # 消息本身仍然存活
    assert _detail_row_count(after_html) == before_rows
    assert f"receipt-{todo_id}" in after_html

    # 2. 库里复核：行仍在，``todo_id`` 已解绑为 NULL，``message_id`` 不变
    after = _snapshot(ctx)
    assert set(after.delivery_rows) == ledger_ids, "投递记录行被删了（应只解绑）"
    assert all(after.delivery_rows[row_id] is None for row_id in ledger_ids)

    # 3. 待办确实被删了（回收真的发生了），消息没被删（该方法明文不动 messages）
    assert "旧投递待办" not in after.todos
    assert message_id in after.message_ids


# --------------------------------------------------------------------------- #
# 接缝 3：pending 保护
# --------------------------------------------------------------------------- #
def test_pending_todo_and_its_message_survive_across_retention_window(
    ctx, manual_clock, web, api
):
    """跨越保留期后，pending 待办与其消息在网页与库里都仍在（§3 硬约束 1+2）。"""
    manual_clock.advance(-PAST_BACK.total_seconds())
    message_id, todo_id = _ingest(api, title="拖了很久的待办", dedup_key="old-pending")
    manual_clock.set(TODAY_LOCAL)

    before = _snapshot(ctx)
    assert before.todos["拖了很久的待办"] == "pending"

    _cross_trigger(manual_clock)
    # 有 pending 待办 + 记录型渠道 → 汇总投递成功（返回 1），随后触发回收
    assert ctx.scheduler.run_once() == 1

    after = _snapshot(ctx)
    assert after.todos.get("拖了很久的待办") == "pending", "pending 待办被回收了"
    assert message_id in after.message_ids, "pending 待办引用的消息被回收了"

    # 网页与库里都在
    assert "拖了很久的待办" in web.get("/todos").text
    detail = web.get(f"/todos/{todo_id}")
    assert detail.status_code == 200
    assert "拖了很久的待办" in detail.text
    assert web.get(f"/messages/{message_id}").status_code == 200


# --------------------------------------------------------------------------- #
# 接缝 4：幂等
# --------------------------------------------------------------------------- #
def test_repeated_purge_is_idempotent_across_settlement_cycles(ctx, manual_clock, web, api):
    """连续两次回收：第二次报告为空；第二个自然日的结算周期不产生任何删除。"""
    manual_clock.advance(-PAST_BACK.total_seconds())
    _, todo_id = _ingest(api, title="会过期的待办", dedup_key="old-idem")
    _complete(web, todo_id)
    manual_clock.set(TODAY_LOCAL)

    _cross_trigger(manual_clock)
    assert ctx.scheduler.run_once() == 0

    # 紧接着再触发一次：报告必须为空（什么都没删、也没解绑）
    second = ctx.retention.purge_expired()
    assert second.is_empty, second
    assert second.deleted_total == 0
    assert second.deliveries_unlinked == 0

    # 真库里确实什么都没少
    snapshot = _snapshot(ctx)
    assert "会过期的待办" not in snapshot.todos

    # 第二个自然日的结算周期：同样不产生删除
    manual_clock.set(TODAY_LOCAL + timedelta(days=1))
    _cross_trigger(manual_clock)
    assert ctx.scheduler.run_once() == 0
    third = ctx.retention.purge_expired()
    assert third.is_empty, third


# --------------------------------------------------------------------------- #
# §4.4 点名：生产装配 build_context 造出来的调度器真的会回收
# --------------------------------------------------------------------------- #
def test_production_build_context_scheduler_actually_reclaims(settings, tmp_path):
    """``build_context`` 的调度器必须真的回收——漏传 ``retention`` 会静默关闭回收。

    ``ReminderScheduler.retention`` 可空（缺省 ``None`` = 跳过回收），因此接线漏传时
    一切照常、只是永远不回收。本测试是唯一能发现该故障的地方：
    用一个**只有回收才会删掉**的历史 ``digest_runs`` 行作为探针。
    """
    clock = ManualClock(TODAY_LOCAL)
    # 生产装配（run_inline=False，真实组合根；这里不启动任何后台线程）
    ctx = build_context(settings, clock=clock)

    assert ctx.retention.enabled is True  # 缺省 30 天
    # 网页按钮与调度器必须共用同一个回收实例（没有第二处删除逻辑）
    assert ctx.scheduler._retention is ctx.retention

    _add_digest_run(ctx, date(2023, 11, 5))  # 探针：早于 cutoff(2023-12-02)
    _add_digest_run(ctx, date(2024, 1, 2))  # 近期：必须保留

    before = _snapshot(ctx)
    assert date(2023, 11, 5) in before.digest_dates

    # 未到触发时刻 → 不结算、不回收
    assert ctx.scheduler.run_once() == 0
    assert date(2023, 11, 5) in _snapshot(ctx).digest_dates

    # 跨过触发时刻 → 真实结算 → 回收
    _cross_trigger(clock)
    assert ctx.scheduler.run_once() == 0
    assert ctx.digest.state_for(TODAY) is not None, "当天未结算，回收不可能被触发"

    after = _snapshot(ctx)
    assert date(2023, 11, 5) not in after.digest_dates, (
        "生产装配的调度器没有回收：retention 很可能没接进 build_context"
    )
    assert date(2024, 1, 2) in after.digest_dates, "保留期内的汇总状态被误删"


# --------------------------------------------------------------------------- #
# §4.4 逆向对照：漏传 retention 会静默关闭回收（上面那条测试的「咬合力」证明）
# --------------------------------------------------------------------------- #
def test_production_wiring_test_has_teeth_when_retention_leaks(settings, monkeypatch):
    """把「漏传 ``retention``」注入**生产装配**，上一条测试必须变红。

    这是对上面那条测试自身的对照：``retention`` 参数可空，漏传时 ``run_once`` 一切照常、
    只是永远不回收。若上一条测试不能变红，它就只是「实现今天恰好全绿」的装饰。
    只改 ``ReminderScheduler.__init__`` 的传参行为（模拟接线缺陷），不触碰任何 ``src/`` 文件。
    """
    from notify_hub.services.scheduler import ReminderScheduler

    original = ReminderScheduler.__init__

    def leak(self, *, retention=None, **kwargs):  # noqa: ANN001 - 模拟漏传
        original(self, retention=None, **kwargs)

    monkeypatch.setattr(ReminderScheduler, "__init__", leak)

    clock = ManualClock(TODAY_LOCAL)
    ctx = build_context(settings, clock=clock)
    assert ctx.scheduler._retention is None, "漏传注入未生效，对照无意义"
    _add_digest_run(ctx, date(2023, 11, 5))

    _cross_trigger(clock)
    assert ctx.scheduler.run_once() == 0
    assert ctx.digest.state_for(TODAY) is not None, "当天未结算，回收不可能被触发"

    # 漏传时该历史行**必须**还在——这正是上面那条测试会变红的原因
    assert date(2023, 11, 5) in _snapshot(ctx).digest_dates


# --------------------------------------------------------------------------- #
# 异常场景：days=0 时调度器照样结算，但一条数据都不删
# --------------------------------------------------------------------------- #
def test_days_zero_disables_recall_but_scheduler_still_settles(tmp_path):
    """``retention.days=0``：调度器照常结算，回收整体关闭，库里一条不少。"""
    settings = _make_settings(tmp_path, retention_days=0)
    clock = ManualClock(TODAY_LOCAL)
    ctx = _context(settings, clock)

    assert ctx.retention.enabled is False
    _add_digest_run(ctx, date(2023, 11, 5))
    # 哨兵：一条**未来**的汇总状态行。回收关闭时它绝不可能被删；若回收被误执行
    # （``_purge_digest_runs`` 只按 ``local_date < cutoff`` 删），它会被删掉，故障立刻可见。
    _add_digest_run(ctx, date(2030, 1, 1))
    before = _snapshot(ctx)

    _cross_trigger(clock)
    assert ctx.scheduler.run_once() == 0
    # 当天确实结案了（结算照常发生；回收被调用但自行判定为关闭）
    assert ctx.digest.state_for(TODAY) is not None

    after = _snapshot(ctx)
    assert date(2030, 1, 1) in after.digest_dates, "days=0 时回收不该删任何东西"
    assert date(2023, 11, 5) in after.digest_dates
    assert after.todos == before.todos
    assert after.delivery_rows == before.delivery_rows

    # 直接调用也返回全 0 报告（不执行任何删除）
    report = ctx.retention.purge_expired()
    assert report.is_empty, report
    assert date(2030, 1, 1) in _snapshot(ctx).digest_dates
