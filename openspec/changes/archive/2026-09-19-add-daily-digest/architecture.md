# add-daily-digest 模块规格

> `design.md` 是设计基线（为什么这样做、有哪些备选），本文档是**契约**（每个模块交付什么、
> 边界在哪、怎么验证）。两者冲突时以 `design.md` 与 `../specs/**/spec.md` 为准。
>
> 本次是**新变更**：`add-notify-hub` 已归档。基线模块规格见
> `../archive/2026-09-18-add-notify-hub/architecture.md`，本文档只写**本次的增量与移除**。

---

## 1. 本次变更的性质

**这是「移除一个已交付特性 + 新增一个特性」，不是参数调整。** 需要评审者据此区分
「移除被替换的特性」与「为了让新代码通过而删测试」。

### 1.1 部署前提：**单进程**（继承自基线设计，此处显式重述）

本变更的每日去重依赖 `digest_runs.local_date` 的**唯一约束**，但「检查是否已发 → 投递 → 写记录」
这三步之间**没有跨进程锁**。因此：

**前提**：同一份 SQLite 库**只由一个服务进程使用**——这是基线
`../archive/2026-09-18-add-notify-hub/design.md` 决策 8 确立的部署模型
（「单进程 uvicorn 常驻，数据落 SQLite 单文件，部署时在每台服务器跑一个实例」）。
**多进程共享同一库不受支持。**

若两个进程同时越过触发时刻，理论上可能**双发**（唯一约束只能防止重复**行**，防不住重复**投递**：
两个进程都会先投递成功，再各自尝试插入，后者才撞上约束）。

**为什么不做防护（刻意取舍，非遗漏）**：把顺序改为「先占位再投递」即可消除双发，
但代价是**进程在占位后、投递前崩溃就会吞掉当天的汇总**——而「当天必须送到」正是决策 3
要用重试换来的性质。在单进程前提下，**双发不可能发生**；为不存在的场景牺牲「不吞掉当天」，
方向是反的。将来若真要多实例，应引入外部锁或改为「占位 + 投递 + 失败回滚占位」，
那是另一个变更。

**评审时请据此判断**：这是一条**已声明的部署前提**，不是未处理的缺陷。

| 类别 | 内容 |
|---|---|
| **移除** | 单项超时间隔提醒：`reminders.first_reminder_after_seconds`、`reminders.reminder_interval_seconds`、`TodoService.due_for_reminder()`、**`notification_for_message()` 的 `kind=REMINDER` 分支**，以及断言这些行为的测试 |
| **新增** | 每日汇总提醒：固定本地时间一条消息列出全部未完成待办，含当日去重、持久化状态、失败重试、逐条记账 |
| **不变** | 接入、分类、适配器契约、投递降级、待办生成与完成入口 |

## 2. 共享文件与架构师改动（fan-out 之前完成）

| 文件 | 为什么是共享的 | 处置 |
|---|---|---|
| `src/notify_hub/context.py` | 组合根：本次要给 `ReminderScheduler` 注入新的 `DigestService`，并把 `digest` 加进 `AppContext` | **架构师阶段 0** |
| `src/notify_hub/app.py`、`main.py`、`domain.py`、`errors.py`、`clock.py` | 无改动 | 仅架构师 |
| `tests/conftest.py` | `tmp_settings` 写的是旧的提醒键，本次必须改为 `at`/`timezone` | **架构师阶段 0** |

> **注意**：`tests/conftest.py` 里的 `tmp_settings` 目前写入
> `first_reminder_after_seconds: 2` 等键。这些键一旦被移除，**所有用到该 fixture 的测试都会在
> fixture 装配阶段失败**（不是断言失败，是错误）。所以它必须在阶段 0 与配置改动同时落地，
> 否则会造成大面积假红。

### 2.0 环境事实：本机**没有**系统时区数据库（已由架构师修复）

实测：`/usr/share/zoneinfo` **不存在**，`zoneinfo.ZoneInfo("UTC")` 抛 `ZoneInfoNotFoundError`，
`available_timezones()` 返回 **0**，系统也未装 `tzdata` 包。

**影响**：设计决策 6「用 IANA 时区名」在本机**完全无法工作**——不是边缘情况，是地基缺失。

**处置（架构师，阶段 0 已完成）**：在 `pyproject.toml` 的**运行时依赖**中加入
`tzdata>=2024.1`（PyPI 包，纯数据、无系统依赖）。`zoneinfo` 在系统库缺失时会自动回退到它。
已实测：安装后 `available_timezones()` 返回 **598**，`Asia/Shanghai` 正常加载，
且 UTC 13:00 → 北京 21:00、UTC 16:00 → 北京**次日** 00:00 的跨日边界均正确。

**给子代理的约束**：**不得**把时区换算改成硬编码 `+08:00` 偏移来「绕开」这个问题——
既定的设计决策 6 明确否决了写死偏移，且 `tzdata` 已经解决。若你在实现中仍遇到
`ZoneInfoNotFoundError`，那是环境异常，应在报告里写 `BLOCKERS`，不要自行改方案。

### 2.1 这两处改动**推迟到阶段 B 末尾**执行（重要，勿提前）

实测发现：**若在 fan-out 之前就应用它们，会让 4 个测试文件在「收集阶段」直接失败**
（`context.py` 顶层 import 了尚不存在的 `services/digest.py`），也就是本项目一直在避免的
「坏红」——测试作者连跑都跑不起来，无法分辨「我的测试写错了」与「依赖还没到位」。

因此顺序调整为：

| 时点 | 动作 | 谁做 |
|---|---|---|
| 阶段 A 之前 | **两者都保持原样**（`git checkout -- src/notify_hub/context.py tests/conftest.py`） | 架构师已完成 |
| 阶段 A | 写测试。新测试**自带 Settings 与 Database**，不依赖 `tmp_settings`，因此不受影响 | verify |
| 阶段 B | 实现 config / models / services / scheduler | dev |
| **阶段 B 末尾** | 重新应用本文件 2 节描述的两处改动 | **架构师** |
| 阶段 B 验收 | 跑全量，应回到全绿 | 架构师 |

**要重新应用的具体内容**：
1. `tests/conftest.py` 的 `tmp_settings`：`reminders` 段改为
   `{"at": "21:00", "timezone": "Asia/Shanghai", "scan_interval_seconds": 1}`。
2. `src/notify_hub/context.py`：`from notify_hub.services.digest import DigestService`；
   `AppContext` 增加 `digest: DigestService` 字段；`_assemble()` 中构造
   `digest = DigestService(db, resolved_clock)` 并作为关键字参数传给 `ReminderScheduler`。

**给子代理的硬约束**：模块**不得**自行修改 `context.py` 与 `tests/conftest.py`。
若实现过程中确实需要它们变化，停下来在报告里写 `BLOCKERS`。

## 3. 模块 M11：每日汇总提醒

**文件边界**

- **实现路径**（交给 `subagent_dev`）：
  - `src/notify_hub/config.py`（`ReminderSettings` 与校验）
  - `src/notify_hub/models.py`（`DigestRun` 表）
  - `src/notify_hub/services/digest.py`（新建：汇总状态服务）
  - `src/notify_hub/services/scheduler.py`（重写调度）
  - `src/notify_hub/services/notifications.py`（汇总文案）
  - `src/notify_hub/services/todos.py`（删除 `due_for_reminder`）
  - `config.example.yaml`
  - `docs/configuration.md`、`docs/architecture-overview.md`、`docs/architecture-map.html`
- **测试路径**（交给 `subagent_verify`）：
  - `tests/test_config.py`、`tests/test_models.py`、`tests/test_todos.py`、
    `tests/test_services.py`、`tests/test_digest.py`（新建）、`tests/test_integration.py`

### 3.1 功能

按配置的**本地时刻**（默认 `21:00` Asia/Shanghai）每天发**一条**汇总，列出全部未完成待办。
**不负责**：消息接入、分类、渠道适配、待办生成与完成。

### 3.2 接口（冻结，不得改名或改参数顺序）

```python
# config.py —— ReminderSettings 整体替换
@dataclass(frozen=True)
class ReminderSettings:
    at: str = "21:00"                       # 本地时间 HH:MM
    timezone: str = "Asia/Shanghai"         # IANA 时区名
    scan_interval_seconds: float = 60.0     # 必须 > 0 且 <= 3600

    @property
    def zone(self) -> ZoneInfo:
        """返回配置时区。加载失败不应发生（load_settings 已校验）。"""
    @property
    def trigger_time(self) -> time:
        """返回 datetime.time(hh, mm)，无 tzinfo。"""
```

**校验契约（补齐三处未定义，冻结）**

| 规则 | 裁定 |
|---|---|
| **旧键检查的范围** | **只在 `reminders` 段内检查**。这两个键原本就住在 `reminders` 段，顶层出现同名键属于「本来就不认识的键」，与旧版本行为一致（顶层未知键不报错，也不解析）。 |
| **旧键「出现」的判定** | **键存在即算出现，值与值无关**——`first_reminder_after_seconds:` 后接空值（YAML `null`）同样必须报错。理由：静默忽略与「只处理非空值」是两种漏检，都要堵住。 |
| **错误消息内容** | 消息**必须包含该键在 YAML 中的字面键名**（`at` / `timezone` / `scan_interval_seconds` / `first_reminder_after_seconds` / `reminder_interval_seconds`）**以及非法值本身**（旧键无值时可只报键名）。不要只写「触发时刻」这类自然语言描述——测试按字面键名断言。 |

```python
# models.py —— 新增表
class DigestRun(SQLModel, table=True):
    __tablename__ = "digest_runs"
    id: int | None                      # 主键
    local_date: date                    # 本地日期；**唯一索引**
    checked_at: datetime                # UTC aware；首次定案时刻
    fired_at: datetime | None           # UTC aware；成功发出汇总的时刻
    todo_count: int = 0                 # 本次汇总覆盖的待办数；0 表示「当日无待办」定案
    delivered: bool = False             # 汇总是否已成功送达
    attempts: int = 0                   # 尝试次数（含失败）
    last_error: str | None              # 最近一次失败原因；**必须已脱敏**
```

```python
# services/digest.py —— 新建
@dataclass(frozen=True)
class DigestState:
    local_date: date
    checked_at: datetime
    fired_at: datetime | None
    todo_count: int
    delivered: bool
    attempts: int
    last_error: str | None

class DigestService:
    def __init__(self, db: Database, clock: Clock) -> None: ...

    def state_for(self, local_date: date) -> DigestState | None:
        """查该本地日期的记录；不存在返回 None。"""

    def record(self, local_date: date, *, todo_count: int, delivered: bool,
               fired_at: datetime | None = None,
               error: str | None = None) -> DigestState:
        """写入或更新该本地日期的记录（按 local_date 幂等）。
        - 首次调用：attempts = 1、checked_at = clock.now()
        - 重复调用：attempts += 1、checked_at 保持不变、其余字段以本次为准
        - **fired_at 为 None 时保留已有值，不得置空**（失败重试不应抹掉此前成功发出的时刻）
        - **error 为 None 时保留已有值，不得置空**（同上）
        - error 必须是**已脱敏**的字符串
        返回更新后的状态。
        """
```

```python
# services/notifications.py —— 新增
def notification_for_digest(todos: Sequence[TodoView], *, now: datetime) -> NotificationMessage:
    """把未完成待办渲染成一条汇总通知。
    前置：todos 非空（为空时调用方不应调用本函数）。
    排序：调用方保证已按 overdue_seconds 降序；本函数不重排。
    """
```

```python
# services/scheduler.py —— 签名变更（多一个 digest 参数）
class ReminderScheduler:
    def __init__(self, *, todos: TodoService, delivery: DeliveryService,
                 digest: DigestService, clock: Clock,
                 settings: ReminderSettings, logger: logging.Logger) -> None: ...
    def run_once(self) -> int:
        """执行一轮检查。返回本次**成功投递**的汇总条数（0 或 1）。"""
    def start(self) -> None: ...
    def stop(self, timeout: float = 5.0) -> None: ...
    @property
    def running(self) -> bool: ...
```

```python
# services/todos.py —— 删除
# 移除 TodoService.due_for_reminder(...)。其余方法不变。
# 保留 record_reminder(todo_id, *, delivered, channel_id)。
```

### 3.3 内部实现（要点与禁止项）

**汇总文案（冻结）**

下面两段要分清——**第一行是 `msg.title`，不在 `msg.body` 里**（适配器负责渲染
`title` + 空行 + `body`，见基线规格 4.6 节「正文渲染归属」不变量）：

```
msg.title :  [待办汇总] <N> 项未完成

msg.body  :  1. <标题>（已超时 <format_duration>；来源 <source>；分类 <category>）
             2. ...
             <空行>
             请到待办页面处理。
```
- `title = f"[待办汇总] {N} 项未完成"`，`level = Level.WARNING`，`kind = DeliveryEvent.REMINDER`，
  `source = "notify-hub"`，`occurred_at = now`，`todo_id = None`，`overdue_seconds = None`
- 时长**复用** `notify_hub.services.notifications.format_duration`（同一裁定：用户可见文案不得有两份实现）
- `category` 为 None 时该分句省略；**正文原样放在 `msg.body`**，
  适配器不得再拼表头（见基线规格 4.6 节「正文渲染归属」不变量）

**调度判定（冻结，按此顺序）**
```
now       = clock.now()
local     = now.astimezone(settings.zone)
if local.time() < settings.trigger_time:      -> 返回 0
today     = local.date()
state     = digest.state_for(today)
settled   = state is not None and (state.delivered or state.todo_count == 0)
if settled:                                    -> 返回 0
todos     = todos.list(status=PENDING, limit=1000)      # 见下方上限说明
if not todos:
    digest.record(today, todo_count=0, delivered=True)  -> 返回 0
msg       = notification_for_digest(todos, now=now)
outcome   = delivery.deliver(msg, preferred_channel=None,   # 见下方「汇总渠道」说明
                             message_id=None, todo_id=None)
if outcome.ok:
    digest.record(today, todo_count=len(todos), delivered=True, fired_at=clock.now())
    for t in todos: todos.record_reminder(t.id, delivered=True, channel_id=outcome.channel_id)
    -> 返回 1
else:
    digest.record(today, todo_count=len(todos), delivered=False, error=outcome.error_reason)
    -> 返回 0
```

- **汇总渠道 = `default_channel`**：调度器持有的是 `DeliveryService`，其
  `default_channel` 已在构造时注入；调用 `deliver(..., preferred_channel=None)` 即可让它走默认渠道与既有降级链。
  **不得**在调度器里重新实现渠道选择。
  （原文伪码此处误写为 `preferred_channel=settings_default_channel`，与同一段的说明相反；
  调度器只拿到 `ReminderSettings`，**没有** `default_channel` 可用。已更正为 `None`。）
- **降级成功即为送达**：若默认渠道失败但降级到其它可用渠道成功，`outcome.ok` 为真，
  **本轮就算成功、不重试**。构造「投递失败」的测试**必须让所有候选渠道都失败**，
  只让默认渠道失败是不够的（降级链会接管）。

**行为变化（需评审者注意）：待办详情页的「投递记录」区块在汇总模型下为空**

汇总以 `todo_id=None` 投递（决策 5），因此 `deliveries` 里**没有**挂到单条待办的汇总记录。
而既有能力规格 `todo-tracking` 的「待办列表与查看」要求详情「包含该待办历次通知的渠道、时间与投递结果」。

**该要求仍然满足，但承载位置变了**：逐条待办的「历次通知（渠道 / 时间 / 结果）」由
`todo_events` 的 `REMINDER` 事件承载（它带 `channel_id` 与 `delivery_ok`），渲染在详情页的
**时间序列**列里；「投递记录」区块保留给**消息首次通知**的记录。

这是刻意取舍，不是遗漏：
- 若为每条被覆盖的待办各写一条 `DeliveryRecord`，就把**一次投递**记成 N 次，与「投递记录」的语义对立；
- 逐条可追溯的需求由 `todo_events` 满足，信息没有丢失，只是不再重复出现在两张表里。

**评审时请据此判断**：若认为「投递记录区块为空」不可接受，那属于**规格问题**（需要重新定义
汇总与逐条待办的记账关系），请明确指出，不要当作实现缺陷。
- **单轮上限 1000 条**：`todos.list(limit=1000)`。超出部分本轮不汇总，写日志告警。
  这是刻意的上限（避免异常情况下构造出超大消息），不是分页实现。
- **跨天不补发**：判定只针对 `today`；昨天的记录即使 `delivered=False` 也不再重试。
- **重启安全**：状态全在 `digest_runs` 表，不依赖内存标记。
- **异常隔离**：单条待办记账失败不得中断整轮；整轮任何异常必须捕获并记日志，线程不得退出。

**禁止项**
- 禁止在调度器里直接 `import` 具体适配器或做渠道选择。
- 禁止把「已发送」状态放到内存或模块级变量。
- 禁止保留 `due_for_reminder` 作为兼容垫片——它属于被移除的特性，必须删净。
- 禁止为了兼容旧配置而静默忽略 `first_reminder_after_seconds`：出现即 `ConfigurationError`。

### 3.4 验证方法（`subagent_verify` 的测试规格）

命令：`.venv/bin/python -m pytest tests/test_config.py tests/test_models.py tests/test_todos.py tests/test_services.py tests/test_digest.py`
以及集成：`.venv/bin/python -m pytest tests/test_integration.py`

必须覆盖的可观察结果：

**配置（`tests/test_config.py`）**
1. `ReminderSettings()` 默认值为 `at="21:00"`、`timezone="Asia/Shanghai"`、`scan_interval_seconds=60.0`；
   `trigger_time == time(21, 0)`；`zone.key == "Asia/Shanghai"`。
2. 合法自定义：`at: "07:30"`、`timezone: "UTC"` → 解析成功且 `trigger_time == time(7, 30)`。
3. 非法 `at`：`"25:00"`、`"21:60"`、`"9"`、`"abc"` → 各自 `ConfigurationError`，消息含键名与非法值。
4. 非法 `timezone`：`"Mars/Olympus"` → `ConfigurationError`，消息含键名与非法值。
5. `scan_interval_seconds`：`0` 与 `3601` → `ConfigurationError`；`3600` 合法。
6. **旧键报错**：配置含 `first_reminder_after_seconds` → `ConfigurationError`，
   消息含该键名与「已移除」字样；`reminder_interval_seconds` 同理。

**数据模型（`tests/test_models.py`）**
7. `init_schema()` 后 `digest_runs` 表存在；连续调用两次幂等。
8. `local_date` 唯一：插入两条相同日期 → `IntegrityError`。
9. 插入并读回一条 `DigestRun`，各字段往返一致；`checked_at` / `fired_at` 经 `as_utc()` 后等于写入值。

**汇总状态服务（`tests/test_digest.py`）**
10. `state_for(未来日期)` → `None`。
11. `record(d, todo_count=0, delivered=True)` → `attempts == 1`、`checked_at == clock.now()`；
    再次 `record(d, todo_count=2, delivered=False, error="x")` → `attempts == 2`、
    **`checked_at` 与第一次相同**、`todo_count == 2`、`delivered is False`、`last_error == "x"`。
12. `error` 为 None 时 `last_error` 保持原值或为 None 均可，但**不得**抛异常。

**调度（`tests/test_digest.py`，全部用 `ManualClock`，禁止真实等待）**
13. **未到时刻不发送**：本地时间 20:59 → `run_once() == 0`，`digest_runs` 无记录，无投递记录。
14. **到达时刻发一条**：本地时间 21:00、2 条待完成 → `run_once() == 1`；
    投递记录**恰好 1 条**且 `message_id is None and todo_id is None`；
    该条记录的渠道为 `default_channel`。
15. **明细按超时时长降序**：3 条待办超时分别 1h/20h/5h → 汇总正文中三个标题的出现顺序为 20h、5h、1h。
16. **含标题与已超时时长**：正文含每条待办的标题与形如 `已超时 ` 的片段。
17. **逐条记账**：汇总覆盖 3 条 → 这 3 条的 `reminder_count` 各 +1、`last_notified_at` 更新为 `clock.now()`，
    且各新增一条 `todo_events(kind=REMINDER)`；`deliveries` 仍是 1 条。
18. **同一自然日不重复**：再次 `run_once()` → 0，投递记录数不变。
19. **空待办不发且定案**：无待完成待办、时间已过 → `run_once() == 0`、无投递记录，
    但 `digest_runs` 有该日期记录且 `todo_count == 0`。
20. **当天稍后新建待办不触发**：第 19 条之后再建一条待办 → `run_once() == 0`。
21. **次日正常触发**：把时钟推进到次日同一时刻 → `run_once() == 1`，且本次只覆盖仍未完成的那些。
22. **失败后当日重试**：令渠道投递失败 → `run_once() == 0`、`digest_runs.delivered is False`、
    `attempts == 1`、`last_error` 非空；把渠道改为成功后再 `run_once()` → 1，`delivered is True`、
    `attempts == 2`。
23. **跨天不补发**：失败后把时钟直接推到次日已过触发时刻之前 → 昨日记录不再被重试（
    断言昨日 `delivered` 仍为 False 且没有新的投递记录指向它）。
24. **重启安全**：新建一个 `ReminderScheduler` 实例（模拟进程重启），在同一自然日再次 `run_once()` → 0。
25. **异常隔离**：令 `record_reminder` 对第一条待办抛异常 → `run_once()` 不抛异常，
    其余待办的记账仍完成，汇总本身仍算成功。

**移除项的验证（必须显式覆盖）**
26. `TodoService` 上**不存在** `due_for_reminder` 属性（`not hasattr(TodoService, "due_for_reminder")`）。
27. `ReminderSettings` 的字段集合**恰好**是 `{at, timezone, scan_interval_seconds}`
    （`set(ReminderSettings.__dataclass_fields__) == {"at","timezone","scan_interval_seconds"}`）。

**集成（`tests/test_integration.py`）**
28. 经真实 `create_app` + 真实 SQLite：投递 2 条 `need_ack=true` 消息 → 把时钟推过触发时刻 →
    `run_once() == 1` → 真实 webhook 桩**新增恰好 1 条**请求 →
    标记其中一条完成 → 次日汇总只含剩下那条。
    **口径澄清（原文「桩收到恰好 1 条请求」与前置条件冲突）**：那 2 条消息的**首次通知也走同一个桩**，
    所以全局请求数必然多于 1。要断言的是：
    - 越过触发时刻后桩**新增**的请求数恰好为 1；
    - 全局**恰好 1 条** `kind == "reminder"` 的载荷；
    - 该载荷正文同时含两条待办的标题（即**不是**为每条待办各发一条）；
    - 每条被覆盖的待办各新增 1 条提醒事件、`reminder_count` 各 +1，
      而该次汇总在 `deliveries` 中只对应**1 条**记录。

**至少 2 个异常场景**：(a) 第 22 条投递失败后重试；(b) 第 25 条记账异常隔离。

**完成后必须成立**：两条命令全绿；`grep -rn "due_for_reminder" src/ tests/` 无输出；
`grep -rn "first_reminder_after_seconds\|reminder_interval_seconds" src/ docs/ config.example.yaml`
只出现在「已移除」的说明性文字与「旧键必须报错」的测试中。
