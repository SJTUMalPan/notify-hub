# 架构与模块规格：add-retention-and-web-ui

> 本文件是**冻结接口**的唯一来源。子代理任务书只引用章节号，不重述内容。
> 设计基线见 `design.md`；决策编号（D1–D8）在那边。

## 1. 交付范围与规模

| 项 | 值 |
|---|---|
| 模块数 | **2**（M-A 数据保留、M-B 待办网页） |
| 预计派发次数 | **6**（每模块：测试 1 + 实现 1，外加集成 1 + 审查 1） |
| 阈值 | 未超过 8 个模块，无需分批 |
| 依赖方向 | **M-B 依赖 M-A**（网页的清空按钮调用 `ctx.retention`），M-A 必须先完成 |

阶段 0 的共享文件由架构师完成，不计入派发。

## 2. 阶段 0：共享文件处置

| 文件 | 处置 | 内容 |
|---|---|---|
| `src/notify_hub/config.py` | 架构师，**阶段 0** | 新增 `RetentionSettings`（`days: int = 30`，含 `enabled` 属性）与 `Settings.retention` |
| `src/notify_hub/config.py` 的 `_parse_retention` | 架构师，**阶段 0** | `retention` 段的**冻结语义**：段缺失或段内无 `days` → 30；`days` **必须是真正的整数**（`bool` / `float` / 字符串一律 `ConfigurationError`——**刻意不复用 `_as_int`**，因为它会 `int(30.7) -> 30` 静默截断）；`days < 0` → `ConfigurationError`；`days == 0` 合法＝关闭回收 |
| `src/notify_hub/context.py` | 架构师，**M-A 实现落地之后** | `AppContext` 增 `retention: RetentionService`；`_assemble` 构造它并传给 `ReminderScheduler` |
| `tests/conftest.py` | **不改** | 调度器的 `retention` 参数可空（§4.4），`make_context` 无需传它。**这是刻意的**——改动可加，不产生破坏窗口 |
| `config.example.yaml` | 架构师 | 新增 `retention` 段并注释 |
| `docs/configuration.md`、`docs/deployment.md` | 架构师 | 保留期参数、两个窗口的区别、备份建议、回滚 |
| `openspec/**` | 架构师 | 本变更全部产物 |

**`context.py` 为什么延后**：它要 `from notify_hub.services.retention import RetentionService`，
而该模块在 M-A 实现之前不存在。提前改会让所有 `import notify_hub.context` 的测试在收集期失败
（`add-notify-hub` 的 D 系列踩过这个坑）。装配行在 M-A 落地后补，属预期，不是遗漏。

**因此执行顺序是串行的**：M-A 测试 → M-A 实现 → 架构师补 `context.py` → M-B 测试 → M-B 实现。

## 3. 数据回收规则（两个模块共同遵守）

**删除顺序不可颠倒**，因为 `db.py` 开启了 `PRAGMA foreign_keys=ON`：

1. 删已完成待办 → 先删其 `todo_events`，并把引用它的 `deliveries.todo_id` **置 NULL（保留行）**
2. 删消息 → 先删 `deliveries` 中 `message_id` 指向它的行，再删消息
3. 删过期 `digest_runs`

**两条硬约束（不是可配置项）**：

- `todos.status = pending` 的待办**永不删除**
- 被**任何** todo 引用的 `messages` 行**永不删除**（`todos.message_id` 是外键）

**时间基准**：一律先把时间戳换算到本地时区（`settings.reminders.zone`，即 `Asia/Shanghai`）
再取日期。待办用 `completed_at`，消息用 `received_at`，汇总用 `digest_runs.local_date`。

**「不留悬空引用」覆盖全部三个外键**：回收后不得存在
`deliveries.todo_id`、`todo_events.todo_id`、`deliveries.message_id` 指向已不存在的行。
前两个靠 §3 的删除顺序保证；第三个由 `PRAGMA foreign_keys=ON` 在数据库层兜底
（删消息前必须先删它的投递记录，否则删除本身会被拒绝）。
**禁止**在回收前临时关闭外键强制。

**禁止**在回收路径里调用 `datetime.now()`——必须用注入的 `Clock`（跨模块不变量）。

## 4. 模块 M-A：数据保留与自动回收

**文件边界**

- **实现路径**（`subagent_dev`）：`src/notify_hub/services/retention.py`（**新建**）、
  `src/notify_hub/services/scheduler.py`
- **测试路径**（`subagent_verify`）：`tests/test_retention.py`（**新建**）

`config.py`、`context.py`、`models.py`、`db.py` 归架构师或既有模块，**两者都不得修改**。

### 4.1 功能

按保留期回收历史数据，使长期运行后的存储占用有界。

**做**：计算截止日、按固定顺序删四类记录、返回计数、在每日结算点被触发、记录一条汇总日志。

**不做**：不新增数据表、不改表结构、不做按条数保留、不做归档导出、不删除 pending 待办、
不删除被待办引用的消息、**不在服务启动时回收**（D3 的理由）、不直接记录逐行日志。

### 4.2 接口

```python
@dataclass(frozen=True)
class PurgeReport:
    todos: int = 0
    todo_events: int = 0
    deliveries_unlinked: int = 0   # deliveries.todo_id 被置 NULL 的**行数**（不是删除数）
    deliveries: int = 0            # 被**删除**的 deliveries 行数
    messages: int = 0
    digest_runs: int = 0

    @property
    def deleted_total(self) -> int:
        """被删除的行数合计（不含 ``deliveries_unlinked``，那些行还在）。"""

    @property
    def is_empty(self) -> bool:
        """是否什么都没删、也没解绑。"""
```

**两个计数字段度量的是不同的事，允许重叠**：同一行投递记录可能先在第 1 步被解绑
（计入 `deliveries_unlinked`），随后在第 2 步随其消息被删（计入 `deliveries`）。
`deleted_total` 只统计**真正被删除**的行，绝不包含 `deliveries_unlinked`。

```python
class RetentionService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        *,
        days: int = 30,
        zone: ZoneInfo,
        logger: logging.Logger | None = None,
    ) -> None: ...

    @property
    def enabled(self) -> bool: ...

    def cutoff_date(self, now: datetime | None = None) -> date: ...

    def purge_expired(self, now: datetime | None = None) -> PurgeReport: ...

    def purge_completed_older_than(self, local_date: date) -> PurgeReport: ...
```

- `enabled`：`days > 0`。
- `cutoff_date(now=None)`：返回 `(now or clock.now()).astimezone(zone).date() - timedelta(days=days)`。
  `now` 为 naive 时**必须**先经 `clock.as_utc()` 归一化（SQLite 读出的时间是裸的）。
  **`days` 极大时不得抛异常**：减法溢出（`OverflowError`）时返回 `date.min`，
  语义上等价于「没有任何记录早于它」＝不回收任何东西。配置层不设上界，
  所以这一层必须兜住。
- `purge_completed_older_than(local_date)`：删除 `status=done` 且 `completed_at` 的**本地日期
  `< local_date`** 的待办；删它们的 `todo_events`；把引用它们的 `deliveries.todo_id` 置 NULL。
  **不动 messages 与 digest_runs。** `completed_at` 为 NULL 的 done 待办**不删**（数据异常，保守处理）。
- `purge_expired(now=None)`：`enabled` 为假时**立即返回全 0 的报告且不执行任何查询级删除**；
  否则按 §3 的顺序执行三步，cutoff 取 `cutoff_date(now)`。

**两个方法的差别（重要，测试作者踩过一次）**：`purge_expired` 里第 1 步的**解绑是过渡性的**——
被回收待办的 `completed_at <= received_at`，所以它的消息**必然也超过截止日**，第 2 步紧接着
就会把那条消息连同它的投递记录一起删掉。因此「**解绑而不是删行**」这个承诺，在
`purge_expired` 的完整一轮里体现不出来。

它的耐久形态只出现在 `purge_completed_older_than`（网页按钮那条路径）：该方法**明文不动
messages**，所以被回收待办的消息**必然存活**，于是消息活着、投递记录活着、只有 `todo_id`
被置空——这才是 D4 承诺真正能观察到的场景。
**验证 D4 必须用 `purge_completed_older_than` 构造，不能用 `purge_expired`。**
- **不变量**：两个方法都是**幂等**的——紧接着再跑一次，报告的 `deleted_total == 0`
  且 `deliveries_unlinked == 0`。
- **错误契约**：不吞异常，由调用方隔离（调度器已整体兜异常）。
- 日志：`deleted_total > 0` 或 `deliveries_unlinked > 0` 时打**一条** INFO，含各字段计数；
  什么都没发生时**不打日志**。日志经**注入的 `logger`** 发出（缺省用
  `logging.getLogger("notify_hub.services.retention")`），级别为 INFO。文案不冻结，
  但必须含各类计数——测试只断言「恰好一条 INFO」，不校验文案。
- **前提**：`days >= 0` 由配置层保证（`_parse_retention` 拒绝负值），本类不做重复校验；
  `days < 0` 不是受支持的输入。

### 4.3 内部实现

- 依赖：`notify_hub.db.Database`、`notify_hub.clock`（`Clock`/`as_utc`）、`notify_hub.models`、
  标准库 `dataclasses`/`datetime`/`zoneinfo`/`logging`。**禁止引入新依赖。**
- 复用既有模式：`src/notify_hub/services/todos.py` 的 `with self._db.session() as session:` 写法、
  中文 docstring、`__all__`。
- 删除用 SQLAlchemy `delete()` 或 `session.exec(select(...))` + `session.delete(...)` 均可；
  但**解绑**（`deliveries.todo_id = NULL`）必须真的写 NULL，而不是删除行。
- 消息是否可删的判定：`NOT EXISTS (SELECT 1 FROM todos WHERE todos.message_id = messages.id)`。
  用 SQL 子查询一次算完，**不要**在 Python 里逐行判断（避免 N+1 与竞态窗口）。
- 每条 `session` 内完成一个步骤；三步之间各自独立提交，避免一个大事务长时间持锁。

### 4.4 scheduler.py 的改动（同一模块内）

- `ReminderScheduler.__init__` 新增**关键字**参数 `retention: RetentionService | None = None`。
  - **为什么可空**：`tests/conftest.py` 的 `make_context` 会构造调度器；若该参数必填，
    阶段 A（`retention.py` 尚不存在）会让**所有**用到 `ctx` 夹具的测试在收集期失败——
    前两次变更都踩过这个坑。可空是刻意换取的「改动可加、不破坏既有 397 条测试」。
  - 为 `None` 时**跳过回收**，其余行为一字不变（不得改变返回值、不得跳过结算）。
  - **这个默认值的代价**：接线漏传会**静默关闭回收**。该风险由阶段 D 的集成测试兜底——
    它必须断言**生产装配 `build_context`** 造出来的调度器真的会回收。
- 在 `_run_once` 里，**当天结算完成之后**调用 `self._retention.purge_expired()`：
  - 空待办分支的 `self._digest.record(today, todo_count=0, delivered=True)` **之后**
  - 成功分支的 `self._digest.record(...delivered=True)` **之后**
  - **失败（未定案）分支不得调用**
- 该调用必须包在自己的 `try/except Exception` 里，异常时打一条 WARNING 并**继续**——
  回收失败绝不能影响汇总本身的返回语义，也不能让调度线程退出。
- **不得**改变 `run_once` 既有的返回值语义与「当天 settled 则早退」的行为。

### 4.5 验证方法

测试文件 `tests/test_retention.py`；命令 `.venv/bin/python -m pytest tests/test_retention.py -q`。

**单元测试**

| 验收点 | 可观察结果 |
|---|---|
| `enabled` | `days=0` → `False`；`days=30` → `True` |
| `cutoff_date` | `ManualClock` 推进到某本地日期，`days=30` 时为 `今天-30`；`days=0` 时为 `今天` |
| `cutoff_date` 对 naive 时间 | 传入 naive `now` 不抛错，按 UTC 归一化 |
| `PurgeReport.deleted_total` | 各字段求和，**不含** `deliveries_unlinked` |
| `is_empty` | 全 0 时为 `True` |
| `cutoff_date` 极大 `days` | `days=10**9` 时不抛异常（返回 `date.min`） |

**单元测试（阶段 0 共享文件组）**——覆盖架构师在阶段 0 改的 `config.py`，语义以 §2 为准：

| 验收点 | 可观察结果 |
|---|---|
| `retention` 段缺失 | `settings.retention.days == 30` |
| `retention` 为空段 | 同上 |
| `days: 0` | `days == 0` 且 `enabled is False` |
| `days: 90` | `days == 90` 且 `enabled is True` |
| `days: -1` | `load_settings` 抛 `ConfigurationError` |
| `days: 30.7` | 抛 `ConfigurationError`（**不得**截断成 30） |
| `days: "30"` | 抛 `ConfigurationError` |
| `days: true` | 抛 `ConfigurationError`（`bool` 不是整数） |
| `RetentionSettings()` 默认 | `days == 30`、`enabled is True` |

**这组若失败，是架构师实现的问题**：不要改期望值去迁就代码，写进 `SPEC-GAPS` 报告。

**行为测试（每条都要先造好数据，再断言删没删）**

| 验收点 | 可观察结果 |
|---|---|
| **pending 待办永不删** | 造一条 100 天前的 pending 待办 → 回收后仍在，且其消息仍在 |
| **被待办引用的消息永不删** | 同上：该消息的 `deliveries` 也一行不少 |
| 过期的已完成待办被删 | 100 天前完成 → todo 行与它的 `todo_events` 都没了 |
| 投递记录**解绑而非删除** | 引用该待办的 `deliveries` **行仍在**、`todo_id` 为 `None`、`message_id` 不变 |
| 保留期内的记录不动 | 完成于 1 天前（`days=30`）→ todo 与消息都还在 |
| 无待办引用的旧消息被删 | 100 天前收到、没有任何 todo 指向它 → 消息与其 `deliveries` 都被删 |
| 近期消息不动 | 1 天前收到的消息仍在 |
| `digest_runs` 按 `local_date` 删 | 100 天前的记录没了，今天的不动 |
| `days=0` 关闭 | 什么都不删，返回全 0 |
| **幂等** | 连续跑两次：第二次 `deleted_total == 0` 且 `deliveries_unlinked == 0` |
| **无悬空引用** | 回收后查库：不存在 `deliveries.todo_id` / `todo_events.todo_id` 指向已不存在的待办，也不存在 `deliveries.message_id` 指向已不存在的消息 |
| `completed_at` 为 NULL 的 done 待办 | 不删（保守处理） |

**调度器集成测试（同一文件内，用 `ManualClock` 推进，禁止真实等待）**

| 验收点 | 可观察结果 |
|---|---|
| 结算后触发 | 推进时钟跨过触发时刻 → 汇总结算 → 过期数据被删 |
| 未到时刻不触发 | 时钟早于触发时刻 → 数据一条不少 |
| 当天已定案不重复触发 | 再跑一轮 → 不再回收（幂等，且不产生第二条日志） |
| 汇总失败时不触发 | 投递失败（未定案）→ 不回收 |
| 回收抛异常不影响汇总 | 注入一个会抛异常的假 retention → `run_once` 仍正常返回、调度器不崩 |

**必须覆盖的异常场景（至少 2 条）**：① 回收过程中抛异常——不得让 `run_once` 抛出，
也不得改变其返回值；② `days=0` 时调度器照样结算，但一条数据都不删。

**完成后必须成立**：

```bash
.venv/bin/python -m pytest tests/test_retention.py -q
.venv/bin/python -m pytest -q          # 全量仍全绿（现为 397 收集）
```

## 5. 模块 M-B：待办网页改版

**文件边界**

- **实现路径**（`subagent_dev`）：`src/notify_hub/web/routes.py`、
  `src/notify_hub/web/templates/*.html`
- **测试路径**（`subagent_verify`）：`tests/test_web_recent_completed.py`（**新建**）

`tests/test_web.py`（既有，635 行）与 `tests/test_docs_contract.py` **都不得修改**。

### 5.1 功能

主列表页新增「最近完成」栏目与「清空已完成」按钮，并整体美化。

**做**：今天的已完成待办单独成块；一键清空更早的已完成；纯 CSS 美化。

**不做**：不新增页面路由、不改既有筛选的语义、不引入任何外部资源或 JS 库、
不让网页直接访问数据库、**不加新的写操作**（除清空按钮外）。

### 5.2 接口

**路由改动（`web/routes.py`）**

- `todos_list` 的模板上下文**新增** `recent_completed`：
  `tuple[TodoView, ...]`，即「`completed_at` 的**本地日期等于今天**」的已完成待办。
  - 「今天」取自 **`ctx.clock.now()`** 换算到 `ctx.settings.reminders.zone` 后的日期
  - 排序：`completed_at` **降序**（最近完成的在前）
  - 既有上下文键（`todos`、`status`）**不得改名或删除**
- **新增路由** `POST /todos/purge-completed`：
  - 调用 `ctx.retention.purge_completed_older_than(今天)`（今天 = 同上换算所得）
  - 返回 `303` 重定向到 `/todos`
  - **不接任何参数、不接受任何用户输入**（避免把 cutoff 变成可注入的量）
- **栏目始终显示**：`status=pending|done|all` 三种视图下都渲染
  （它回答的是「今天完成了什么」，与主表筛选无关）

**模板改动**

- `todos_list.html`：新增「最近完成」区块（标题、完成时间、处理耗时 `format_duration`、
  标题链接到详情）。空时显示占位文案「今天还没有完成的待办」。
  区块内放「清空已完成」表单：`method="post"`、`action="/todos/purge-completed"`、
  `onsubmit="return confirm('确定清空今天之前完成的待办吗？此操作不可撤销。')"`。

  **文案冻结范围（测试作者问过，这里定死）**：
  - 栏目**标题**冻结为字面量 **`最近完成`**，空态占位冻结为 **`今天还没有完成的待办`**——测试据此断言
  - **按钮文案不冻结**，由实现自选；测试只断言表单契约（action / method / onsubmit 含 `confirm(`），
    不断言按钮上的字
- `base.html`：重写内联 `<style>`（见 §5.3）。
- **主表结构保持不变**：七列、`colspan="7"` 的空态行、`?status=` 三个筛选链接都不得改动
  ——既有 `tests/test_web.py` 依赖它们。

### 5.3 内部实现

- **零外部资源**：不得新增 `<link rel=...>`、`@import`、webfont、`<script src>`；
  不得引入任何 CSS/JS 框架。全部样式写在 `base.html` 的 `<style>` 内。
- 手机优先：保留 `viewport` meta；正文最大宽度受限并居中；表格在窄屏**不得撑破页面**
  （用 `overflow-x:auto` 包裹或等价方案）；触控目标（按钮）高度不小于约 2rem。
- 视觉：系统字体栈；浅色背景 + 卡片式分区；主色建议用现有 `theme-color` 同族色；
  待完成/已完成用不同色的徽章；表头弱化；行悬停高亮。
- 可访问性：语义结构（`<table>/<thead>/<tbody>/<nav>/<main>`）保持；
  颜色对比不过低；`confirm` 用内联属性，不新增脚本文件。
- Jinja2 自动转义是硬约束（消息正文可能含 HTML），**不得**使用 `|safe` 或任何绕过转义的写法。
- 复用既有过滤器：`format_time` / `status_label` / `event_kind_label` / `format_duration`，
  **不得**在模板里自造时间格式。

### 5.4 验证方法

测试文件 `tests/test_web_recent_completed.py`；
命令 `.venv/bin/python -m pytest tests/test_web_recent_completed.py -q`。

装配用 `create_web_app(ctx)`（既有，**不启动后台线程**）+ `fastapi.testclient.TestClient`；
`ctx` 用 conftest 的 `make_context` + `manual_clock` 造，时间通过 `ManualClock` 控制。

| 验收点 | 可观察结果 |
|---|---|
| 今天完成的进栏目 | 用 `ManualClock` 把「今天」定住，造一条今天完成的待办 → 页面出现它 |
| 昨天完成的不进栏目 | 造一条昨天完成的 → 栏目里**没有**它（但主表 `status=all` 时仍有） |
| 栏目按完成时间降序 | 造两条今天完成的 → 页面中靠前的那条完成时间更晚 |
| 栏目在三种筛选下都显示 | `?status=pending` / `done` / `all` → 三处都能看到栏目标题 |
| 栏目为空时的占位 | 没有今天完成的待办 → 出现占位文案 |
| 清空按钮存在且带确认 | 页面含 `action="/todos/purge-completed"` 的 form，且含 `onsubmit=` 与 `confirm(` |
| **清空只删更早的** | 造「今天完成」与「昨天完成」各一条 → POST 后今天那条还在、昨天那条没了 |
| 清空后重定向 | POST → `303`，`Location` 为 `/todos` |
| 清空不碰 pending | 造一条很旧的 pending 待办 → POST 后仍在 |
| 清空后消息未被删 | 被清空待办的消息仍在库里 |
| 页面自包含 | 渲染出的 HTML 里**不含** `src="http` / `href="http` / `@import` |
| 既有过滤器仍生效 | 页面里的时间形如 `YYYY-MM-DD HH:MM:SS`（UTC、去微秒） |

**必须覆盖的异常场景（至少 2 条）**：① 库里没有任何已完成待办时访问列表页——栏目显示占位、
主表显示空态，**不得 500**；② `?status=done` 且今天无完成项——栏目占位与主表空态同时出现，
两处文案不串。

**完成后必须成立**：

```bash
.venv/bin/python -m pytest tests/test_web_recent_completed.py -q
.venv/bin/python -m pytest tests/test_web.py -q          # 既有 635 行必须仍全绿
.venv/bin/python -m pytest -q                            # 全量
```

## 6. 集成测试范围（阶段 D）

作用域 2，`tests/test_integration_retention_web.py`，**不打桩**：

- **调度器 -> 真实 DB -> 网页**：用 `ManualClock` 推进跨过触发时刻，让真实调度器完成结算并
  触发回收；随后用真实 `create_web_app(ctx)` 取页面，断言被回收的记录确实不再出现。
- **回收 -> 投递记录账本完整性**：删除一条已完成待办后，其消息详情页的「投递记录」
  **行数不变**（`todo_id` 被解绑但行还在）——这是 D4 的核心承诺。
- **pending 保护**：跨越保留期后，pending 待办与其消息在网页与库里都仍在。
- **幂等**：连续两次结算周期之间重复触发回收，第二次报告为空。

## 7. 门禁

1. M-A 测试（红）→ M-A 实现（绿）→ 架构师补 `context.py`
2. M-B 测试（红）→ M-B 实现（绿）→ 全量绿
3. 阶段 D 集成测试
4. 阶段 E `subagent_review`（对照 `design.md` / `proposal.md` 查设计符合性）
5. security-scan → commit → push

## 8. 规格修订记录

**R1（阶段 A 之后，冻结前）**——测试作者报告 4 处规格不够精确，架构师逐条补充，测试断言
在新表述下依然成立，故未重派测试轮：`PurgeReport` 两个计数字段**允许重叠**；
「不留悬空引用」覆盖**三个**外键并禁止临时关闭外键强制；日志经注入 logger 发出、文案不冻结；
明确前提 `days >= 0` 由配置层保证。此外把调度器的 `retention` 参数定为**可空**
（缺省 `None` = 跳过回收），代价是漏传会静默关闭回收，由阶段 D 断言生产装配来兜底。

**R2（阶段 B 之后，实现已存在）**——开发工报告 5 条 `test-conflict` 并**停下来未改任何测试**
（处理正确）。架构师逐条读测试原文后裁定：**5 条全部是测试缺陷，规格与实现正确。**

| # | 测试缺陷 | 裁定依据 |
|---|---|---|
| 1 | `:401` 写 `_report_cls().is_empty`，**漏了一层实例化**（同文件 `:405`/`:410` 都是 `_report_cls()(...)`） | `is_empty` 是 `@property`，类访问只能得到 property 对象。机械笔误 |
| 2 | `:360` 把 naive `2024-04-10 07:00` 当作**北京时间**换算，期望得到 04-09 | 规格 §3「SQLite 读出的时间是裸的」与**紧邻的上一条测试 `:350`** 都确立「naive 即 UTC」。该测试的假设与规格相反 |
| 3 | `:561` 用 `purge_expired` 验证「解绑而非删行」，但造的是 100 天前的消息——它在第 2 步必然被删 | 与 `:601`（旧且无引用的消息**必须**被删）直接互斥。D4 的耐久形态只能用 `purge_completed_older_than` 观察（见 §4.2 的说明） |
| 4 | `:868`/`:903`/`:971` 断言 `run_once() == 1`，但夹具 `_scheduler_fixture` 未传 `notifiers` → 空渠道注册表 → 汇总投递失败 | `_make_scheduler` 本来就有 `notifiers=()` 参数；`:868` 请求了 `make_recording_notifier` 却从未传入。与 retention 无关 |
| 5 | `:875` 用 `_capture(LOG)`，而 `LOG = logging.getLogger("notify_hub.tests.retention")` 并非服务 logger 的后代 | 同文件其他用例都用 `_capture(_retention_logger())` |

**对审查者的提示**：这 5 处修正属于**架构师下令的测试修正，不是开发工篡改测试**。
开发工的 `TESTS-TOUCHED: no` 属实。M-A 的提交包含「实现 + 按本裁定的测试修正」，
因此 HEAD 全程保持全绿。

**冻结点**：R1、R2 之后 §2–§5 的接口即为最终版。此后任何改动都必须走「重开冻结 →
复核受影响测试与实现」的流程。

**R3（阶段 B 修正轮之后）**——修正轮修好 5 处后，暴露出**第 6 处**同类缺陷（此前被
`run_once() == 1` 的先失败掩盖，R2 #4 修好后首次可见）。架构师裁定：**测试缺陷，同类错误。**

`:913` 断言 `ids["old_message"] in _message_ids(db)`，与 §4.2 明文**相反**。夹具里
`old_message` 是 100 天前的，其唯一引用者 `old_todo` 已在第 1 步被删，因此第 2 步按 §3 必然
删除它及其投递记录；同一组里 `pending_message`（`:912` 那一行）已经单独覆盖了
「pending 待办的消息永不删」这个真正要守的承诺。故 `:913` 应改为 `not in`。

修正轮执行者**未擅自修改**该处并停下请求裁定——判断正确：单方面翻转断言方向正是「迁就实现」
的形态，必须由架构师下令。

**R4（阶段 A′ 之后，M-B 实现之前）**——M-B 的测试作者问了两处「渲染文案是否冻结」。架构师裁定：
**栏目标题与空态占位冻结为字面量**（测试已据此断言，无需改动）；**按钮文案不冻结**，实现自选，
测试只断言表单契约。已写入 §5.2 的「文案冻结范围」。
