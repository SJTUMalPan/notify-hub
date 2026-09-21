"""M8 模块测试：Web 待办界面（``architecture.md`` 第 6 节「模块 M8 · 4. 验证方法」）。

作用域 1：**实现尚不存在**。本文件必须能被 pytest 成功**收集**，失败发生在运行时
（``ModuleNotFoundError: notify_hub.web``）——这是本模块测试的正确红色。

**惰性导入是硬要求**（阶段 A：``notify_hub.web`` 尚不存在）：本文件顶层只导入标准库、
pytest 与阶段 0 的共享模块。所有对 ``notify_hub.web`` 的导入都写在 fixture / 辅助函数体内。

**造数据只经 service 层**（``ctx.messages`` / ``ctx.todos`` / ``ctx.pipeline`` 与
``manual_clock.advance()``），不走 HTTP 接口——避免把 M7 的失败混进 M8 的测试。

时间格式的假设见文件末尾「规格空白」注释；**时长格式已裁定**（M8 第 3 段：复用 M6 的
``notify_hub.services.notifications.format_duration``，禁止 web 层再实现一份），
见文件末尾第 1 条。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pytest

from notify_hub.clock import as_utc
from notify_hub.domain import AckReason, ClassificationVerdict, Level, TodoStatus

#: M8 页面不得出现的原始脚本片段（XSS 安全关键）。
RAW_SCRIPT = "<script>alert(1)</script>"

#: 完成表单的 action（spec 3 段冻结：``<form method="post" action="/todos/{id}/done">``）。
_DONE_FORM_RE = re.compile(r'action="/todos/(\d+)/done"')


# --------------------------------------------------------------------------- #
# 惰性装配
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(ctx):
    """挂在 ``create_web_app(ctx)`` 上的同步 ``TestClient``。

    ``create_web_app`` 只挂 web 路由、不启动后台线程（M8 spec 3 段），因此不进
    ``with`` 上下文也不影响页面测试。

    ``follow_redirects=False`` 是**必需**的（spec 4 段第 4 条冻结）：httpx 的 ``TestClient``
    默认跟随重定向，会把 ``/`` 与 ``POST /todos/{id}/done`` 的 303 自动跟成 200，
    导致 303 这一可观察结果**根本无法被观察到**。关掉跟随，客户端如实暴露每个路由的
    真实状态码；本文件所有用例都直接命中 200 路由，不依赖跟随行为。
    """
    from fastapi.testclient import TestClient  # 惰性

    from notify_hub.web.routes import create_web_app  # M8，惰性

    return TestClient(create_web_app(ctx), follow_redirects=False)


# --------------------------------------------------------------------------- #
# 辅助：造数据（只经 service 层）
# --------------------------------------------------------------------------- #
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


def _draft(
    *,
    source: str = "db-backup",
    title: str = "数据库备份失败",
    body: str | None = "exit code 1",
    level: Level = Level.ERROR,
    need_ack: bool = True,
    dedup_key: str | None = None,
):
    from notify_hub.services.messages import MessageDraft  # 惰性

    return MessageDraft(
        source=source,
        title=title,
        body=body,
        level=level,
        need_ack=need_ack,
        dedup_key=dedup_key,
        meta={"host": "db-01"},
    )


def _accept_at(ctx, clock, moment, *, make_todo: bool = True, **draft_kwargs):
    """把时钟设到 ``moment``，写一条消息（可选生成待办），返回 ``(message, todo)``。"""
    clock.set(moment)
    message = ctx.messages.create(
        _draft(**draft_kwargs),
        _verdict(
            need_ack=make_todo,
            preferred_channel="recording" if make_todo else None,
        ),
    )
    todo = ctx.todos.ensure_for_message(message) if make_todo else None
    assert todo is not None
    return message, todo


def _accept(ctx, clock, *, make_todo: bool = True, **draft_kwargs):
    """时钟不动（用当前时刻）写一条消息。"""
    return _accept_at(ctx, clock, clock.now(), make_todo=make_todo, **draft_kwargs)


# --------------------------------------------------------------------------- #
# 辅助：页面文本与时间匹配
# --------------------------------------------------------------------------- #
def _render(body: bytes) -> str:
    return body.decode("utf-8")


def _time_candidates(moment: datetime) -> list[str]:
    """一个时刻在页面上可能的渲染形式（UTC 本地时间 / 保留或省略微秒）。"""
    unaware = as_utc(moment).replace(tzinfo=None)
    candidates: list[str] = []
    for value in (unaware, unaware.replace(microsecond=0)):
        iso = value.isoformat()
        candidates.append(iso)
        candidates.append(value.strftime("%Y-%m-%d %H:%M:%S"))
    aware = as_utc(moment).astimezone(timezone.utc)
    for value in (aware, aware.replace(microsecond=0)):
        candidates.append(value.isoformat())
        candidates.append(value.strftime("%Y-%m-%d %H:%M:%S"))
        candidates.append(value.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return candidates


def _time_index(html: str, moment: datetime) -> int | None:
    """页面上首次出现该时刻的下标；完全找不到时返回 ``None``。"""
    found = [html.index(c) for c in _time_candidates(moment) if c in html]
    return min(found) if found else None


def _require_time(html: str, moment: datetime, what: str) -> int:
    index = _time_index(html, moment)
    assert index is not None, (
        f"页面未渲染{what}（{as_utc(moment).isoformat()}）；"
        "规格要求展示时间，且实现不得修改本测试"
    )
    return index


def _format_duration(seconds: float) -> str:
    """本文件用于**对照**的期望时长文案（``'59 秒'``、``'20 分钟'``、``'1 小时 1 分钟 1 秒'``）。

    现行规格（``architecture.md`` 第 6 节 M8 第 3 段）：web 层**必须复用**
    ``notify_hub.services.notifications.format_duration``，**不得**在 web 层再实现一份。
    因此该函数只是把那份冻结的输出复述成期望值供断言比对——实现无论经导入复用还是
    内联同一逻辑，都必须产出这里的文案（对两种实现都成立，故断言与实现选择无关）。
    """
    total = int(seconds) if seconds > 0 else 0
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分钟")
    if secs or not parts:
        parts.append(f"{secs} 秒")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# 规格第 4 段第 1 条：列表按超时时长降序
# --------------------------------------------------------------------------- #
def test_todo_list_orders_by_overdue_desc(ctx, manual_clock, client):
    """3 条待办 first_notified_at = now-1h / now-5h / now-20m → 页面顺序 5h、1h、20m。"""
    base = manual_clock.now()
    _, oldest = _accept_at(
        ctx,
        manual_clock,
        base - timedelta(hours=5),
        source="s-5h",
        title="待办五小时前",
        dedup_key="k-5h",
    )
    _, middle = _accept_at(
        ctx,
        manual_clock,
        base - timedelta(hours=1),
        source="s-1h",
        title="待办一小时前",
        dedup_key="k-1h",
    )
    _, newest = _accept_at(
        ctx,
        manual_clock,
        base - timedelta(minutes=20),
        source="s-20m",
        title="待办二十分钟前",
        dedup_key="k-20m",
    )
    manual_clock.set(base)

    # 数据前提（与 M6 的排序语义一致）：超时时长 18000 / 3600 / 1200 秒。
    views = ctx.todos.list()
    assert [v.id for v in views] == [oldest.id, middle.id, newest.id]

    response = client.get("/todos")
    assert response.status_code == 200
    html = _render(response.content)

    index_oldest = html.index("待办五小时前")
    index_middle = html.index("待办一小时前")
    index_newest = html.index("待办二十分钟前")
    assert index_oldest < index_middle < index_newest


# --------------------------------------------------------------------------- #
# 规格第 4 段第 2 条：列表字段
# --------------------------------------------------------------------------- #
def test_todo_list_shows_source_category_title_first_notified_and_overdue(
    ctx, manual_clock, client
):
    """每行含来源、分类、标题、首次通知时间、超时时长。"""
    base = manual_clock.now()
    _, one = _accept_at(
        ctx,
        manual_clock,
        base - timedelta(minutes=20),
        source="src-alpha",
        title="待办甲",
        dedup_key="k-a",
    )
    _, two = _accept_at(
        ctx,
        manual_clock,
        base - timedelta(minutes=21),
        source="src-beta",
        title="待办乙",
        dedup_key="k-b",
    )
    _, three = _accept_at(
        ctx,
        manual_clock,
        base - timedelta(minutes=22),
        source="src-gamma",
        title="待办丙",
        dedup_key="k-c",
    )
    manual_clock.set(base)

    response = client.get("/todos")
    assert response.status_code == 200
    html = _render(response.content)

    # 来源、分类、标题
    for source in ("src-alpha", "src-beta", "src-gamma"):
        assert source in html, f"列表页缺少来源 {source}"
    assert "backup-failure" in html, "列表页缺少分类"
    for title in ("待办甲", "待办乙", "待办丙"):
        assert title in html, f"列表页缺少标题 {title}"

    # 首次通知时间（三个待办各自的时刻都必须渲染出来）
    for todo_row, what in ((one, "待办甲"), (two, "待办乙"), (three, "待办丙")):
        row = ctx.todos.get(todo_row.id)
        assert row is not None
        _require_time(html, row.first_notified_at, f"{what}的首次通知时间")

    # 超时时长（1200 / 1260 / 1320 秒 → 与冻结的 format_duration 一致）
    for seconds in (1200.0, 1260.0, 1320.0):
        assert _format_duration(seconds) in html, (
            f"列表页缺少超时时长 {_format_duration(seconds)}"
        )

    # 每行都有进入详情与「完成」表单
    assert {int(m) for m in _DONE_FORM_RE.findall(html)} == {one.id, two.id, three.id}
    assert f'href="/todos/{one.id}"' in html, "列表页缺少进入详情的入口"


# --------------------------------------------------------------------------- #
# 规格第 4 段第 3 条：状态过滤
# --------------------------------------------------------------------------- #
def test_todo_list_defaults_to_pending_and_supports_status_filter(ctx, manual_clock, client):
    """一条 pending + 一条 done：默认只含 pending；all 含两者；done 只含已完成那条。"""
    base = manual_clock.now()
    _, pending = _accept_at(
        ctx,
        manual_clock,
        base - timedelta(minutes=10),
        source="s-pending",
        title="尚未完成的待办",
        dedup_key="k-pending",
    )
    _, finished = _accept_at(
        ctx,
        manual_clock,
        base - timedelta(minutes=30),
        source="s-done",
        title="已经完成的待办",
        dedup_key="k-done",
    )
    manual_clock.set(base)
    assert ctx.todos.complete(finished.id).status == "completed"

    default_html = _render(client.get("/todos").content)
    assert "尚未完成的待办" in default_html
    # R5 修正：栏目按 §5.2 渲染在主表下方，今天完成的待办必然出现在整页上，
    # 因此「主表不得列出已完成待办」只能对**主表区域**（字面量「最近完成」之前）断言。
    default_main = default_html.split("最近完成")[0]
    assert "已经完成的待办" not in default_main

    all_html = _render(client.get("/todos", params={"status": "all"}).content)
    assert "尚未完成的待办" in all_html
    assert "已经完成的待办" in all_html

    pending_html = _render(client.get("/todos", params={"status": "pending"}).content)
    assert "尚未完成的待办" in pending_html
    pending_main = pending_html.split("最近完成")[0]
    assert "已经完成的待办" not in pending_main

    done_html = _render(client.get("/todos", params={"status": "done"}).content)
    assert "已经完成的待办" in done_html
    assert "尚未完成的待办" not in done_html


# --------------------------------------------------------------------------- #
# 规格第 4 段第 4 条：完成操作（POST 表单 → 303）
# --------------------------------------------------------------------------- #
def test_post_done_redirects_to_todos_and_completes(ctx, manual_clock, client):
    """POST /todos/{id}/done → 303 + Location=/todos；随后从待完成列表消失、详情仍可见。"""
    base = manual_clock.now()
    _, todo = _accept_at(
        ctx,
        manual_clock,
        base - timedelta(minutes=15),
        source="s-complete",
        title="等待被点完成的待办",
        dedup_key="k-complete",
    )
    manual_clock.set(base)

    response = client.post(f"/todos/{todo.id}/done")
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.rstrip("/").endswith("/todos"), f"意外 Location: {location}"

    row = ctx.todos.get(todo.id)
    assert row.status == TodoStatus.DONE
    assert row.completed_at is not None

    main_region = _render(client.get("/todos").content).split("最近完成")[0]
    assert "等待被点完成的待办" not in main_region

    detail = client.get(f"/todos/{todo.id}")
    assert detail.status_code == 200
    assert "已完成" in _render(detail.content)


# --------------------------------------------------------------------------- #
# 规格第 4 段第 5 条：详情时间序列（创建 → 2 次提醒 → 完成）
# --------------------------------------------------------------------------- #
def test_todo_detail_renders_event_timeline_and_deliveries(ctx, manual_clock, client):
    """创建 → **两个自然日各一次**汇总提醒 → 完成：4 个事件按时间顺序出现，页面含渠道与结果。

    **改写自旧的「同一自然日按间隔提醒两次」场景（已随 add-daily-digest 移除）。**
    旧断言：``advance(61)`` 与 ``advance(120)`` 各得 ``run_once() == 1``，即同一自然日内按
    ``reminder_interval_seconds`` 重复提醒。新模型按**本地日期**去重，同一自然日绝不发第二次，
    该场景不存在；因此改为**跨两个自然日各一次**汇总（与
    ``test_integration_web.py::test_web_detail_audit_timeline_after_real_digests`` 同思路），
    并补一条防回归断言：同一自然日内再跑一轮必须返回 0。断言方向只加强不放松。
    """
    from sqlmodel import select  # 惰性

    from notify_hub.domain import TodoEventKind  # 惰性（阶段 0 共享）
    from notify_hub.models import DeliveryRecord  # 惰性

    zone = ctx.settings.reminders.zone
    trigger = ctx.settings.reminders.trigger_time

    _, todo = _accept(ctx, manual_clock)

    # ---- 第 1 个自然日（本地 2024-01-01 21:00）：一次汇总 ----
    manual_clock.set(datetime.combine(date(2024, 1, 1), trigger, tzinfo=zone))
    assert ctx.scheduler.run_once() == 1

    # 防回归（被替换掉的旧场景所违背的约束）：同一自然日稍后（本地 22:00）再跑一轮必须返回 0，
    # 且不得新增任何投递记录。
    manual_clock.advance(3600)
    assert ctx.scheduler.run_once() == 0

    # ---- 第 2 个自然日（本地 2024-01-02 21:00）：再一次汇总 ----
    manual_clock.set(datetime.combine(date(2024, 1, 2), trigger, tzinfo=zone))
    assert ctx.scheduler.run_once() == 1

    manual_clock.advance(5)
    assert ctx.todos.complete(todo.id).status == "completed"

    detail = ctx.todos.detail(todo.id)
    assert detail is not None
    assert [event.kind for event in detail.events] == [
        TodoEventKind.CREATED.value,
        TodoEventKind.REMINDER.value,
        TodoEventKind.REMINDER.value,
        TodoEventKind.COMPLETED.value,
    ]
    assert detail.todo.reminder_count == 2
    # 两次提醒分属**两个不同的本地自然日**（新模型的核心约束）。
    reminder_dates = [
        as_utc(event.occurred_at).astimezone(zone).date()
        for event in detail.events
        if event.kind == TodoEventKind.REMINDER.value
    ]
    assert reminder_dates == [date(2024, 1, 1), date(2024, 1, 2)]

    # 新模型：汇总以 ``todo_id=None`` 投递，因此该待办名下**没有**投递记录；
    # 渠道与投递结果由时间序列的「渠道/投递结果」两列承载（见下方页面断言）。
    assert len(detail.deliveries) == 0

    # 全局对账（加强）：恰好 2 条 reminder 投递记录，均不归属任何待办/消息，渠道与结果正确。
    with ctx.db.session() as session:
        records = list(
            session.exec(
                select(DeliveryRecord)
                .where(DeliveryRecord.event == "reminder")
                .order_by(DeliveryRecord.id.asc())
            ).all()
        )
    assert len(records) == 2
    assert all(record.todo_id is None and record.message_id is None for record in records)
    assert all(record.channel_id == "recording" for record in records)
    assert all(record.ok is True for record in records)

    response = client.get(f"/todos/{todo.id}")
    assert response.status_code == 200
    html = _render(response.content)

    # 4 个事件的时间都必须渲染，且按时间升序出现
    indexes = [
        _require_time(html, event.occurred_at, f"第 {n} 个事件（{event.kind}）")
        for n, event in enumerate(detail.events, start=1)
    ]
    assert indexes == sorted(indexes), f"详情页事件顺序错误: {indexes}"

    # 历次通知的渠道与投递结果
    assert "recording" in html, "详情页缺少通知渠道 id"
    assert ("成功" in html) or ("ok" in html.lower()), "详情页缺少投递结果"


# --------------------------------------------------------------------------- #
# 规格第 4 段第 6 条：详情含原始消息
# --------------------------------------------------------------------------- #
def test_todo_detail_contains_original_message_fields(ctx, manual_clock, client):
    """详情页含关联消息的标题、正文、来源、级别、分类。"""
    _, todo = _accept(
        ctx,
        manual_clock,
        source="src-detail",
        title="详情页专用标题",
        body="详情页专用正文内容",
        level=Level.WARNING,
        dedup_key="k-detail",
    )

    response = client.get(f"/todos/{todo.id}")
    assert response.status_code == 200
    html = _render(response.content)

    for token in ("详情页专用标题", "详情页专用正文内容", "src-detail", "backup-failure"):
        assert token in html, f"详情页缺少 {token}"
    assert "warning" in html.lower(), "详情页缺少原始消息级别"


# --------------------------------------------------------------------------- #
# 规格第 4 段第 7 条：消息列表与消息详情
# --------------------------------------------------------------------------- #
def test_messages_list_is_reverse_chronological_and_respects_limit(ctx, manual_clock, client):
    """GET /messages 按接收时间降序；?limit= 生效。"""
    base = manual_clock.now()
    _accept_at(
        ctx,
        manual_clock,
        base - timedelta(minutes=30),
        source="s-msg-1",
        title="最早的消息",
        dedup_key="k-m1",
    )
    _accept_at(
        ctx,
        manual_clock,
        base - timedelta(minutes=20),
        source="s-msg-2",
        title="中间的消息",
        dedup_key="k-m2",
    )
    _accept_at(
        ctx,
        manual_clock,
        base - timedelta(minutes=10),
        source="s-msg-3",
        title="最新的消息",
        dedup_key="k-m3",
    )
    manual_clock.set(base)

    html = _render(client.get("/messages").content)
    assert html.index("最新的消息") < html.index("中间的消息") < html.index("最早的消息")

    limited = _render(client.get("/messages", params={"limit": 2}).content)
    assert "最新的消息" in limited
    assert "中间的消息" in limited
    assert "最早的消息" not in limited


def test_message_detail_shows_classification_and_delivery_channel(ctx, manual_clock, client):
    """GET /messages/{id} 含分类结果（rule_id/category/labels）与实际使用的渠道 id。"""
    message, _todo = _accept(
        ctx,
        manual_clock,
        source="src-message-detail",
        title="消息详情页标题",
        dedup_key="k-message-detail",
    )
    outcome = ctx.pipeline.dispatch(message.id)
    assert outcome is not None
    assert outcome.ok is True
    assert outcome.channel_id == "recording"

    response = client.get(f"/messages/{message.id}")
    assert response.status_code == 200
    html = _render(response.content)

    for token in ("backup-failure", "infra", "backup", "recording"):
        assert token in html, f"消息详情页缺少 {token}"


# --------------------------------------------------------------------------- #
# 规格第 4 段第 8 条：XSS 转义（安全关键）
# --------------------------------------------------------------------------- #
def test_xss_payload_is_escaped_on_list_and_detail_pages(ctx, client):
    """标题含 ``<script>alert(1)</script>`` 的待办：页面不得出现原始标签，须含 ``&lt;script&gt;``。"""
    outcome = ctx.pipeline.accept(
        _draft(
            source="src-xss",
            title=RAW_SCRIPT,
            body="正文 <img src=x onerror=alert(2)>",
            need_ack=True,
            dedup_key="k-xss",
        )
    )
    assert outcome.todo_id is not None, "消息未被判定入待办，用例前提不成立"
    todo_id = outcome.todo_id

    # 直接经 ctx 复核数据前提（不经 HTTP 造数据）
    row = ctx.todos.get(todo_id)
    assert row is not None
    assert row.title == RAW_SCRIPT
    assert ctx.todos.detail(todo_id) is not None

    list_html = _render(client.get("/todos").content)
    # 前置检查：待办确实出现在列表页。断言对象必须是**待办 id**（或其转义形态），
    # 而**不是**原始串——转义正确时原始串必然不在页面里，断言它出现会与下面
    # 「必须转义」的安全断言直接冲突（spec 4 段第 8 条：只允许否定式与肯定式断言）。
    assert f'href="/todos/{todo_id}"' in list_html, "待办未出现在列表页，用例前提不成立"
    assert "&lt;script&gt;" in list_html, "列表页未转义 <script>"
    assert "&lt;/script&gt;" in list_html, "列表页未转义 </script>"
    assert RAW_SCRIPT not in list_html.replace("&lt;script&gt;", "").replace(
        "&lt;/script&gt;", ""
    ), "列表页出现了未转义的 <script> 标签"

    detail_html = _render(client.get(f"/todos/{todo_id}").content)
    assert "&lt;script&gt;" in detail_html, "详情页未转义 <script>"
    assert RAW_SCRIPT not in detail_html.replace("&lt;script&gt;", "").replace(
        "&lt;/script&gt;", ""
    ), "详情页出现了未转义的 <script> 标签"
    # 正文里的 HTML（规格明确点名的场景）同样必须被转义
    assert "&lt;img src=x onerror=alert(2)&gt;" in detail_html, "详情页未转义消息正文里的 HTML"
    assert "<img src=x onerror=alert(2)>" not in detail_html, "详情页原样输出了消息正文里的 HTML"


# --------------------------------------------------------------------------- #
# 规格第 4 段第 9 条：404
# --------------------------------------------------------------------------- #
def test_missing_todo_returns_404_for_get_and_post(ctx, manual_clock, client):
    """GET /todos/99999 → 404；POST /todos/99999/done → 404。"""
    assert ctx.todos.list(status=None) == [], "用例前提：库中不应有待办"

    assert client.get("/todos/99999").status_code == 404
    assert client.post("/todos/99999/done").status_code == 404


# --------------------------------------------------------------------------- #
# 规格第 4 段第 10 条：根路径
# --------------------------------------------------------------------------- #
def test_root_redirects_to_todos(client):
    """GET / → 303 且 Location 以 /todos 结尾。"""
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"].rstrip("/").endswith("/todos")


# --------------------------------------------------------------------------- #
# 规格 3 段：模板目录与工厂契约（`importlib.resources`/`__file__` 定位，不依赖 cwd）
# --------------------------------------------------------------------------- #
def test_templates_dir_is_absolute_and_exists():
    """``TEMPLATES_DIR`` 必须可经 ``__file__`` 定位且真实存在（不依赖 cwd）。"""
    from notify_hub.web.routes import TEMPLATES_DIR  # M8，惰性

    assert TEMPLATES_DIR.is_absolute()
    assert TEMPLATES_DIR.is_dir()
    for name in (
        "base.html",
        "todos_list.html",
        "todo_detail.html",
        "messages_list.html",
        "message_detail.html",
    ):
        assert (TEMPLATES_DIR / name).is_file(), f"缺少模板 {name}"


# --------------------------------------------------------------------------- #
# 规格空白（本文件做出的假设，已作为 SPEC-GAPS 上报）
# --------------------------------------------------------------------------- #
# 1. 超时时长的文本格式**已由架构师裁定，不再是规格空白**：web 层必须复用 M6 的
#    ``notify_hub.services.notifications.format_duration``，不得再实现一份；格式为单位
#    「天/小时/分钟/秒」、空格分隔、只展开非零单位、全零为 ``0 秒``
#    （``architecture.md`` 第 6 节 M8 第 3 段）。上方 ``_format_duration`` 仅作期望值对照。
# 2. 时间的**文本格式**未被冻结（规格只说「本地可读形式」）。``_time_candidates`` 接受
#    ISO 形式、"YYYY-MM-DD HH:MM:SS"、去掉微秒与带 ``Z`` 的变体，UTC 与省略时区两种写法都接受；
#    但不接受把时间渲染成本地非 UTC 时区（部署约定为 UTC）。
# 3. 「投递结果」的措辞未冻结，仅断言页面出现成功语义（含「成功」或 ``ok``）。

