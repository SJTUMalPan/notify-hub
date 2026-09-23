"""M-B 模块测试（``add-retention-and-web-ui`` §5）：待办网页「最近完成」栏目与「清空已完成」。

作用域 1：**本模块的实现尚不存在**——``src/notify_hub/web/routes.py`` 与
``src/notify_hub/web/templates/`` 还没有为本次需求做任何改动，这是刻意的。因此本文件
当前必须**红**，且红的理由是「实现缺失」而不是语法/收集错误。

规格来源：``openspec/changes/add-retention-and-web-ui/architecture.md`` §5.1–§5.4
（§5.4 是验收清单），辅以同变更的 ``specs/todo-web-ui/spec.md``。

装配（照规格 §5.4）：``notify_hub.web.create_web_app(ctx)``（既有，**不启动后台线程**）
+ ``fastapi.testclient.TestClient``；``ctx`` 取自 conftest（走 ``build_test_context``，
天然带 ``ctx.retention``）。时间一律经 ``ManualClock`` 控制——禁止真实等待、禁止
``datetime.now()``。

**「栏目内」如何被孤立地观察**：既有主表默认只含 pending（``routes._status_filter``），
所以一条**已完成**待办的标题在默认视图 ``GET /todos``（pending）上**只可能**来自
「最近完成」栏目。因此：

- 「进了栏目」＝ 其标题出现在默认视图上；
- 「没进栏目」＝ 其标题不出现在默认视图上（而 ``?status=all`` 的主表里仍能看到它）。

本文件所有「栏目内 / 栏目外」的断言都只依赖这一结构性事实，**不依赖任何 class/id 命名**。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from notify_hub.clock import as_utc

# --------------------------------------------------------------------------- #
# 规格冻结的常量
# --------------------------------------------------------------------------- #
#: §5.2 冻结的空态占位文案（逐字）。
PLACEHOLDER = "今天还没有完成的待办"

#: §5.1/§5.2 给该栏目定的名字。**渲染出的标题文案本身未被逐字冻结**（见文件末 SPEC-GAPS）。
SECTION_TITLE = "最近完成"

#: 既有模板的空态文案（§5.2：主表结构保持不变，七列 / ``colspan="7"`` 都不得改动）。
MAIN_EMPTY = "没有匹配的待办"

#: §5.2 冻结的表单契约。
PURGE_ACTION = "/todos/purge-completed"
CONFIRM_TEXT = "确定清空今天之前完成的待办吗？此操作不可撤销。"

#: 既有过滤器的时间格式（§5.4：``YYYY-MM-DD HH:MM:SS``、UTC、去微秒）。
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# --------------------------------------------------------------------------- #
# 时间线（本地时区 = Asia/Shanghai = UTC+8，与 ``settings.reminders.zone`` 一致）
# --------------------------------------------------------------------------- #
DAY1 = datetime(2024, 1, 1, 1, 0, 0, tzinfo=timezone.utc)  # 本地 2024-01-01 09:00
DAY2 = datetime(2024, 1, 2, 1, 0, 0, tzinfo=timezone.utc)  # 本地 2024-01-02 09:00
TWENTY_MIN = timedelta(minutes=20)
#: 请求时刻：本地 2024-01-02 13:00 —— 于是「今天」＝ 2024-01-02，DAY1 完成的那条＝「昨天」。
NOW = DAY2 + timedelta(hours=4)


# --------------------------------------------------------------------------- #
# 惰性装配
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(ctx):
    """挂在 ``create_web_app(ctx)`` 上的同步 ``TestClient``（不跟随重定向）。

    不跟随重定向是**必需**的：否则 ``303`` 会被自动跟成 ``200``，规格要求的
    「POST → 303」根本无法被观察到。
    """
    from fastapi.testclient import TestClient  # 惰性

    from notify_hub.web import create_web_app  # M-B，惰性

    return TestClient(create_web_app(ctx), follow_redirects=False)


# --------------------------------------------------------------------------- #
# 辅助：造数据（只经 service 层）
# --------------------------------------------------------------------------- #
def _html(response) -> str:
    assert response.status_code == 200, f"页面未正常返回: {response.status_code}"
    return response.content.decode("utf-8")


def _accept(ctx, clock, moment, *, source: str, title: str, dedup_key: str):
    """把时钟设到 ``moment``，写一条消息并生成待办，返回 ``(message, todo)``。"""
    from notify_hub.domain import AckReason, ClassificationVerdict, Level  # 惰性
    from notify_hub.services.messages import MessageDraft  # 惰性

    clock.set(moment)
    message = ctx.messages.create(
        MessageDraft(
            source=source,
            title=title,
            body=f"{title} 的正文",
            level=Level.ERROR,
            need_ack=True,
            dedup_key=dedup_key,
        ),
        ClassificationVerdict(
            rule_id="rule-1",
            category="cat-1",
            labels=("infra",),
            need_ack=True,
            ack_reason=AckReason.RULE,
            preferred_channel=None,
        ),
    )
    todo = ctx.todos.ensure_for_message(message)
    assert todo is not None, "用例前提不成立：消息未生成待办"
    return message, todo


def _accept_and_complete(
    ctx, clock, *, created_at, completed_at, source: str, title: str, dedup_key: str
):
    """走**真实完成路径**造一条已完成待办，``completed_at`` 由时钟决定。"""
    message, todo = _accept(
        ctx, clock, created_at, source=source, title=title, dedup_key=dedup_key
    )
    clock.set(completed_at)
    outcome = ctx.todos.complete(todo.id)
    assert outcome.status == "completed", f"用例前提不成立：完成失败 {outcome.status}"
    return message, todo


def _view(ctx, todo_id: int):
    """取 ``TodoView``（规格把 ``TodoView.completed_at`` 定为断言对象）。"""
    for view in ctx.todos.list(status=None):
        if view.id == todo_id:
            return view
    return None


def _duration_text(seconds: float) -> str:
    """既有过滤器 ``format_duration`` 的冻结文案（单位天/小时/分钟/秒、空格分隔）。

    这里只是把冻结输出复述成期望值供比对，**不从 web 层导入**——避免让断言与实现自证。
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


def _add_delivery(ctx, *, todo_id: int, message_id: int, moment: datetime) -> int:
    """给某个待办挂一条投递记录（用于验证「解绑而非删除」）。"""
    from notify_hub.models import DeliveryRecord  # 惰性

    with ctx.db.session() as session:
        record = DeliveryRecord(
            message_id=message_id,
            todo_id=todo_id,
            channel_id="recording",
            attempted_at=moment,
            ok=True,
            event="reminder",
        )
        session.add(record)
        session.flush()
        return record.id


# --------------------------------------------------------------------------- #
# §5.4：今天完成的进栏目
# --------------------------------------------------------------------------- #
def test_today_completed_appears_in_recent_section(ctx, manual_clock, client):
    """今天完成的一条待办 → 默认（pending）视图上出现它，并带进入详情的链接。"""
    _message, todo = _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY2,
        completed_at=DAY2 + TWENTY_MIN,
        source="s-today",
        title="今天完成的待办",
        dedup_key="k-today",
    )
    manual_clock.set(NOW)

    html = _html(client.get("/todos"))
    # 主表默认只含 pending —— 它出现在这里只可能来自「最近完成」栏目。
    assert "今天完成的待办" in html, "今天完成的待办未出现在列表页（栏目缺失）"
    assert SECTION_TITLE in html, "列表页缺少栏目"
    assert f'href="/todos/{todo.id}"' in html, "栏目内的标题未链接到详情"
    # 主表此时为空（没有 pending 待办），可确认上面的出现不是主表带来的。
    assert MAIN_EMPTY in html


# --------------------------------------------------------------------------- #
# §5.4：昨天完成的不进栏目
# --------------------------------------------------------------------------- #
def test_yesterday_completed_absent_from_section_but_visible_in_all(ctx, manual_clock, client):
    """昨天完成的一条：默认（pending）视图上没有它；``?status=all`` 的主表里仍有它。"""
    _message, todo = _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY1,
        completed_at=DAY1 + TWENTY_MIN,
        source="s-yesterday",
        title="昨天完成的待办",
        dedup_key="k-yesterday",
    )
    manual_clock.set(NOW)

    default_html = _html(client.get("/todos"))
    assert "昨天完成的待办" not in default_html, "昨天完成的待办混进了「最近完成」栏目"

    all_html = _html(client.get("/todos", params={"status": "all"}))
    assert "昨天完成的待办" in all_html, "主表 status=all 下仍应能看到昨天完成的待办"
    assert f'href="/todos/{todo.id}"' in all_html
    # 反向防漏：上面的「不在默认视图」也可能因为**整个栏目缺失**而通过，
    # 因此必须同时确认栏目确实存在（否则本用例会在实现缺失时假绿）。
    assert SECTION_TITLE in all_html, "列表页缺少栏目（本用例的否定断言随之失效）"


# --------------------------------------------------------------------------- #
# §5.4：栏目按完成时间降序
# --------------------------------------------------------------------------- #
def test_recent_section_orders_by_completed_at_desc(ctx, manual_clock, client):
    """两条今天完成的（较早 / 较晚）→ 页面中较晚的那条排在前。"""
    _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY2,
        completed_at=DAY2 + timedelta(minutes=10),
        source="s-early",
        title="今天较早完成",
        dedup_key="k-early",
    )
    _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY2,
        completed_at=DAY2 + timedelta(minutes=40),
        source="s-late",
        title="今天较晚完成",
        dedup_key="k-late",
    )
    manual_clock.set(NOW)

    html = _html(client.get("/todos"))
    assert "今天较早完成" in html and "今天较晚完成" in html, "两条今天完成的待办未出现在页面"
    assert html.index("今天较晚完成") < html.index("今天较早完成"), (
        "「最近完成」栏目未按 completed_at 降序（最近完成的应在最前）"
    )


# --------------------------------------------------------------------------- #
# §5.4：栏目在三种筛选下都显示
# --------------------------------------------------------------------------- #
def test_recent_section_rendered_under_three_status_filters(ctx, manual_clock, client):
    """``?status=pending`` / ``done`` / ``all`` 三种视图下都能看到栏目。"""
    _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY2,
        completed_at=DAY2 + TWENTY_MIN,
        source="s-three",
        title="三种视图都该看到的待办",
        dedup_key="k-three",
    )
    manual_clock.set(NOW)

    for status in ("pending", "done", "all"):
        html = _html(client.get("/todos", params={"status": status}))
        assert SECTION_TITLE in html, f"status={status} 视图下栏目缺失"


# --------------------------------------------------------------------------- #
# §5.4：栏目为空时的占位
# --------------------------------------------------------------------------- #
def test_recent_section_placeholder_when_no_today_completion(ctx, manual_clock, client):
    """只有昨天完成的待办 → 栏目显示占位文案。"""
    _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY1,
        completed_at=DAY1 + TWENTY_MIN,
        source="s-only-yesterday",
        title="昨天完成的孤例",
        dedup_key="k-only-yesterday",
    )
    manual_clock.set(NOW)

    html = _html(client.get("/todos", params={"status": "all"}))
    assert PLACEHOLDER in html, "今天无完成项时栏目未显示占位文案"


# --------------------------------------------------------------------------- #
# §5.4：清空按钮存在且带确认
# --------------------------------------------------------------------------- #
def test_purge_form_present_with_confirmation(ctx, manual_clock, client):
    """页面含指向 ``/todos/purge-completed`` 的 POST 表单，且带 ``confirm`` 二次确认。"""
    manual_clock.set(NOW)

    html = _html(client.get("/todos"))
    assert f'action="{PURGE_ACTION}"' in html, "缺少清空按钮的表单 action"
    assert 'method="post"' in html, "清空表单不是 POST"
    assert "onsubmit=" in html, "清空表单缺少 onsubmit 二次确认"
    assert "confirm(" in html, "清空表单缺少 confirm(...) 二次确认"
    assert CONFIRM_TEXT in html, "确认文案与规格 §5.2 冻结的文案不一致"


# --------------------------------------------------------------------------- #
# §5.4：清空只删更早的
# --------------------------------------------------------------------------- #
def test_purge_removes_only_earlier_completed(ctx, manual_clock, client):
    """今天完成 + 昨天完成各一条 → POST 后今天那条仍在、昨天那条被删。"""
    _y_msg, yesterday = _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY1,
        completed_at=DAY1 + TWENTY_MIN,
        source="s-p-old",
        title="将被清空的昨天待办",
        dedup_key="k-p-old",
    )
    _t_msg, today = _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY2,
        completed_at=DAY2 + TWENTY_MIN,
        source="s-p-today",
        title="清空后应保留的今天待办",
        dedup_key="k-p-today",
    )
    manual_clock.set(NOW)

    response = client.post(PURGE_ACTION)
    assert response.status_code == 303

    assert ctx.todos.get(yesterday.id) is None, "昨天完成的待办未被清空"
    kept = ctx.todos.get(today.id)
    assert kept is not None, "今天完成的待办被误删"
    assert kept.status == "done"


def test_purge_with_nothing_older_is_noop(ctx, manual_clock, client):
    """只有今天完成的待办 → 清空不删任何内容，操作正常完成（303）。"""
    _msg, today = _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY2,
        completed_at=DAY2 + TWENTY_MIN,
        source="s-nothing-old",
        title="唯一且今天完成的待办",
        dedup_key="k-nothing-old",
    )
    manual_clock.set(NOW)

    response = client.post(PURGE_ACTION)
    assert response.status_code == 303
    assert ctx.todos.get(today.id) is not None, "没有更早的已完成待办时不应删除任何内容"


# --------------------------------------------------------------------------- #
# §5.4：清空后重定向
# --------------------------------------------------------------------------- #
def test_purge_redirects_303_to_todos(ctx, manual_clock, client):
    """POST ``/todos/purge-completed`` → 303，``Location`` 为 ``/todos``。"""
    manual_clock.set(NOW)

    response = client.post(PURGE_ACTION)
    assert response.status_code == 303, f"期望 303，实际 {response.status_code}"
    location = response.headers.get("location", "")
    assert location.rstrip("/").endswith("/todos"), f"意外 Location: {location}"


# --------------------------------------------------------------------------- #
# §5.4 / spec「未完成的待办不受影响」
# --------------------------------------------------------------------------- #
def test_purge_keeps_pending_todo_and_its_message(ctx, manual_clock, client):
    """一条很旧的 pending 待办 → 清空后待办与其消息都仍在。"""
    message, pending = _accept(
        ctx,
        manual_clock,
        DAY2 - timedelta(days=100),
        source="s-old-pending",
        title="很久以前仍未完成的待办",
        dedup_key="k-old-pending",
    )
    manual_clock.set(NOW)

    assert client.post(PURGE_ACTION).status_code == 303

    assert ctx.todos.get(pending.id) is not None, "pending 待办被清空误删"
    assert ctx.messages.get(message.id) is not None, "pending 待办的消息被清空误删"


# --------------------------------------------------------------------------- #
# §5.4：清空后消息未被删
# --------------------------------------------------------------------------- #
def test_purge_keeps_message_of_removed_todo(ctx, manual_clock, client):
    """被清空待办引用的消息必须仍在库里（§4.2：该方法不动 messages）。"""
    message, yesterday = _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY1,
        completed_at=DAY1 + TWENTY_MIN,
        source="s-keep-msg",
        title="消息必须活下来的待办",
        dedup_key="k-keep-msg",
    )
    manual_clock.set(NOW)

    assert client.post(PURGE_ACTION).status_code == 303

    assert ctx.todos.get(yesterday.id) is None, "用例前提不成立：该待办未被清空"
    assert ctx.messages.get(message.id) is not None, "清空把消息一起删了"


# --------------------------------------------------------------------------- #
# spec「投递记录不被删除」（D4 的核心承诺）
# --------------------------------------------------------------------------- #
def test_purge_unlinks_deliveries_instead_of_deleting(ctx, manual_clock, client):
    """被清空待办名下的投递记录行仍在，只是 ``todo_id`` 被置 NULL。"""
    from notify_hub.models import DeliveryRecord  # 惰性

    message, yesterday = _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY1,
        completed_at=DAY1 + TWENTY_MIN,
        source="s-delivery",
        title="有投递记录的待办",
        dedup_key="k-delivery",
    )
    delivery_id = _add_delivery(
        ctx, todo_id=yesterday.id, message_id=message.id, moment=DAY1
    )
    manual_clock.set(NOW)

    assert client.post(PURGE_ACTION).status_code == 303

    with ctx.db.session() as session:
        record = session.get(DeliveryRecord, delivery_id)
        assert record is not None, "投递记录被删除（规格要求解绑而非删行）"
        assert record.todo_id is None, "投递记录未与已删待办解绑"
        assert record.message_id == message.id, "投递记录的 message_id 被改动"


# --------------------------------------------------------------------------- #
# §5.2：路由不接任何参数
# --------------------------------------------------------------------------- #
def test_purge_ignores_user_supplied_parameters(ctx, manual_clock, client):
    """带查询参数的 POST 与不带时行为一致——cutoff 不可被外部注入。"""
    _msg, yesterday = _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY1,
        completed_at=DAY1 + TWENTY_MIN,
        source="s-inject-old",
        title="参数注入场景的昨天待办",
        dedup_key="k-inject-old",
    )
    _msg2, today = _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY2,
        completed_at=DAY2 + TWENTY_MIN,
        source="s-inject-today",
        title="参数注入场景的今天待办",
        dedup_key="k-inject-today",
    )
    manual_clock.set(NOW)

    response = client.post(
        PURGE_ACTION, params={"days": "0", "cutoff": "2099-01-01", "before": "today"}
    )
    assert response.status_code == 303
    assert ctx.todos.get(yesterday.id) is None, "带参数的清空未清掉更早的已完成待办"
    assert ctx.todos.get(today.id) is not None, "用户参数影响了 cutoff（今天那条被误删）"


# --------------------------------------------------------------------------- #
# §5.4：页面自包含
# --------------------------------------------------------------------------- #
def test_page_contains_no_external_resources(ctx, manual_clock, client):
    """渲染出的 HTML 不含 ``src="http`` / ``href="http`` / ``@import``。"""
    _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY2,
        completed_at=DAY2 + TWENTY_MIN,
        source="s-selfcontained",
        title="自包含检查用待办",
        dedup_key="k-selfcontained",
    )
    manual_clock.set(NOW)

    html = _html(client.get("/todos", params={"status": "all"}))
    for forbidden in ('src="http', 'href="http', "@import"):
        assert forbidden not in html, f"页面引用了外部资源: {forbidden}"


# --------------------------------------------------------------------------- #
# §5.4：既有过滤器仍生效
# --------------------------------------------------------------------------- #
def test_time_rendered_with_frozen_filter_format(ctx, manual_clock, client):
    """完成时间按 ``YYYY-MM-DD HH:MM:SS``（UTC、去微秒）渲染。"""
    _msg, todo = _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY2,
        completed_at=DAY2 + TWENTY_MIN,
        source="s-time",
        title="时间格式检查用待办",
        dedup_key="k-time",
    )
    manual_clock.set(NOW)

    view = _view(ctx, todo.id)
    assert view is not None and view.completed_at is not None
    expected = as_utc(view.completed_at).strftime(TIME_FORMAT)
    assert expected == "2024-01-02 01:20:00", f"用例前提不成立：{expected}"

    html = _html(client.get("/todos"))
    assert expected in html, "完成时间未按既有过滤器格式（UTC、去微秒）渲染"
    assert f"{expected}.000000" not in html, "完成时间带上了微秒"


# --------------------------------------------------------------------------- #
# §5.2：处理耗时复用 format_duration
# --------------------------------------------------------------------------- #
def test_duration_rendered_with_frozen_format(ctx, manual_clock, client):
    """栏目内的处理耗时用既有 ``format_duration`` 文案（此处 20 分钟）。"""
    _msg, todo = _accept_and_complete(
        ctx,
        manual_clock,
        created_at=DAY2,
        completed_at=DAY2 + TWENTY_MIN,
        source="s-duration",
        title="耗时格式检查用待办",
        dedup_key="k-duration",
    )
    manual_clock.set(NOW)

    view = _view(ctx, todo.id)
    assert view is not None
    expected = _duration_text(view.overdue_seconds)
    assert expected == "20 分钟", f"用例前提不成立：{expected}"

    html = _html(client.get("/todos"))
    assert expected in html, f"栏目内未按既有过滤器渲染处理耗时（期望「{expected}」）"


# --------------------------------------------------------------------------- #
# §5.2：主表结构保持不变
# --------------------------------------------------------------------------- #
def test_main_table_structure_unchanged(ctx, manual_clock, client):
    """七列表头、``colspan="7"`` 空态行、三个 ``?status=`` 筛选链接都仍在。"""
    manual_clock.set(NOW)

    html = _html(client.get("/todos"))
    for header in ("来源", "分类", "标题", "首次通知时间", "超时时长", "状态", "操作"):
        assert header in html, f"主表表头缺失: {header}"
    assert 'colspan="7"' in html, "主表空态行不再是 colspan=7"
    for status in ("pending", "done", "all"):
        assert f'href="/todos?status={status}"' in html, f"筛选链接缺失: status={status}"


# --------------------------------------------------------------------------- #
# §5.4 异常场景 ①：库里没有任何已完成待办
# --------------------------------------------------------------------------- #
def test_list_page_with_no_completed_todo_does_not_fail(ctx, manual_clock, client):
    """库中空无一物 → 200；栏目显示占位、主表显示空态。"""
    manual_clock.set(NOW)

    response = client.get("/todos")
    assert response.status_code == 200, "空库访问列表页不应 500"
    html = _html(response)
    assert PLACEHOLDER in html, "栏目未显示占位文案"
    assert MAIN_EMPTY in html, "主表未显示空态"


# --------------------------------------------------------------------------- #
# §5.4 异常场景 ②：?status=done 且今天无完成项
# --------------------------------------------------------------------------- #
def test_status_done_empty_shows_both_empty_states(ctx, manual_clock, client):
    """栏目占位与主表空态同时出现，且两处文案不串。"""
    manual_clock.set(NOW)

    html = _html(client.get("/todos", params={"status": "done"}))
    assert PLACEHOLDER in html, "栏目未显示占位文案"
    assert MAIN_EMPTY in html, "主表未显示空态"
    assert PLACEHOLDER != MAIN_EMPTY, "两处空态文案被写成了同一句（串了）"
    assert html.count(PLACEHOLDER) == 1, "栏目占位文案重复出现"
    assert html.count(MAIN_EMPTY) == 1, "主表空态文案重复出现"


# --------------------------------------------------------------------------- #
# SPEC-GAPS（本文件做出的假设，已作为 SPEC-GAPS 上报）
# --------------------------------------------------------------------------- #
# 1. 「最近完成」栏目的**渲染标题文案**没有被逐字冻结：§5.1/§5.2 用「最近完成」给栏目命名，
#    §5.4 只要求「三处都能看到栏目标题」，但从未给出标题字符串。本文件据栏目名断言
#    ``SECTION_TITLE = "最近完成"``。若实现用了别的标题文案，需由架构师裁定文案而非改测试。
# 2. §5.3 的「手机优先」中两条无法在此测试层观察（见 UNCOVERED）：
#    窄屏不撑破、触控目标 ≥ 2rem —— 都需要布局渲染引擎，静态 HTML 断言只能测到写法。
