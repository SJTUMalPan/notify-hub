# notify-hub 架构与模块规格

> **本文档的地位**：这是 `add-notify-hub` 变更的**实现规格**（module-level specification）。
> `design.md` 是**设计基线**（为什么这样做、有哪些备选），本文档是**契约**（每个模块交付什么、
> 边界在哪、怎么验证）。两者冲突时，`design.md` 与 `specs/**/spec.md` 优先，且这属于
> 需要上报用户的**设计问题**，不是实现细节。
>
> **读法**：开发/验证子代理只需要读本文档的「4. 全局冻结契约」+「自己的模块」两节。
> 不要把整份文档塞进任务书——给路径。

---

## 1. 现状与约束

| 事实 | 影响 |
|---|---|
| `message/` 当前为空项目，只有 `openspec/` 与 `.git`，无既有代码 | 无迁移包袱，但也没有可参考的既有模式；所有约定必须在本文档冻结 |
| 需求规格：4 个 capability（`message-ingest` / `message-classification` / `todo-tracking` / `notification-delivery`），10 组任务 | 模块边界必须能一一映射回 tasks.md，见第 8 节映射表 |
| 运行时：**Python 3.10.12**（`/usr/bin/python3`） | **禁止**使用 3.11+ 语法：`datetime.UTC`（用 `timezone.utc`）、`typing.Self`、`except*`、`tomllib` 写操作 |
| 依赖可安装（pip + 网络可用），虚拟环境固定为项目根 `.venv` | 所有验证命令统一用 `.venv/bin/python -m pytest` |
| 部署形态：单进程常驻 + SQLite 单文件，内网/回环监听 | 不引入外部中间件；不引入认证体系 |
| 通知渠道首版 `webhook` + `email` | 适配器契约（M4）是本设计的核心资产，优先保证其可扩展性 |

### 1.1 并发模型（全项目统一，必须遵守）

**除 FastAPI 路由函数外，全部代码是同步代码。** 理由与后果：

- `Notifier.send()` 是**同步**的（`httpx.Client`、`smtplib` 都是同步库）；上层**不得**为适配器
  引入 `async def`。
- 两个后台组件——规则热加载（M3）与超时提醒调度（M6）——都是**守护线程 + `threading.Event`**，
  不是 asyncio task。`start()` / `stop()` 均为**同步**方法。
- 首次通知不阻塞响应：M7 的 `IngestPipeline` 用一个 `queue.Queue` + 单个守护工作线程承接投递
  （见第 9 节偏差记录 D-1 与 `design.md` 决策 4 的对照）。
- 因此**不需要** `pytest-asyncio`；测试用 FastAPI 同步 `TestClient`。

### 1.2 时间约定（最容易静默出错的地方）

- 所有时间点都是 **tz-aware 的 UTC `datetime`**；`Clock.now()` 返回 aware UTC。
- SQLite 不保留时区信息，**从 ORM 读出的 `datetime` 必然是 naive 的**（已在 `DateTime(timezone=True)`
  上实测确认：写入 `2024-05-01T12:00+00:00`，读回 `2024-05-01 12:00`，`tzinfo is None`）。因此：
  **任何从数据库读出、即将参与比较或算术的时间，必须先经 `clock.as_utc()` 归一化。**
  这条规则对所有模块生效，M2 的测试必须覆盖「写入 aware → 读出经 `as_utc()` 后仍等于原值」。
- 存储列一律使用 `DateTime(timezone=True)`。

### 1.3 已实测的环境事实（阶段 0 验证，子代理不必重复验证）

| 事实 | 结论 |
|---|---|
| 依赖栈可安装 | `.venv` 就绪；`pip install --no-build-isolation -e ".[dev]"` 成功，`import notify_hub` 通过 |
| **必须用 `--no-build-isolation`** | 默认的 PEP 517 隔离构建会创建 build env 并**挂死**（实测 15 分钟无进展）；venv 内已有 setuptools/wheel，禁用隔离即可 |
| 解析出的版本 | fastapi 0.141.1 / starlette 1.6.0 / pydantic 2.13.5 / sqlmodel 0.0.42 / SQLAlchemy 2.0.54 / uvicorn 0.53.0 / httpx 0.28.1 / typer 0.27.2 / pytest 9.1.1 / Jinja2 3.1.6 / aiosmtpd 1.4.6 / PyYAML 6.0.3 |
| FastAPI 默认 422 形状 | `{"detail":[{"loc":["body","source"],...}]}` —— 字段名确实出现在 `loc` 中，tasks 3.2 可用 |
| SQLite 部分唯一索引 | DDL 实测为 `CREATE UNIQUE INDEX ... ON todos (source, dedup_key) WHERE dedup_key IS NOT NULL AND status='pending'`，约束真实生效 |
| SQLModel JSON 列 | `sa_column=Column(JSON)` 往返正常 |
| `TestClient` + `BackgroundTasks` | **确认阻塞**：带 1 秒后台任务的请求耗时 1.014s，普通路由 0.004s → 偏差 D-1 成立 |
| `queue.Queue` + 工作线程 | 入队耗时 0.02ms，`q.join()` 可正常排空 → D-1 方案成立 |
| `aiosmtpd` `Controller` | **`port=0` 不可用**，必须先取空闲端口（见 M4 验证方法第 7 条） |
| `typer.testing.CliRunner` | 正常；缺必填选项时 `exit_code == 2`，与 M5 的退出码约定不冲突 |
| `httpx.MockTransport` | 可捕获请求体与请求头，可注入 `ConnectError` |
| pytest 9.1.1 | `caplog` / `tmp_path` / `monkeypatch` 行为正常 |
| 已知无害告警 | starlette 1.6 对 `httpx` 版 `TestClient` 发 `StarletteDeprecationWarning`（建议 `httpx2`）。当前可用；`pyproject.toml` 已按前缀过滤，**不要**为此改动依赖或测试 |

---

## 2. 分层架构与依赖方向

依赖方向**只允许向下**，不允许反向或成环。

```
 L4 入口层    ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
              │  M7 api/*    │   │  M8 web/*    │   │  M5 cli.py   │
              │  HTTP 接入层  │   │  Web 待办界面 │   │  命令行客户端 │
              └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
                     │                  │                  │ (仅 HTTP)
 L3 编排层    ┌──────▼──────────────────▼───────┐         │
              │  M7 pipeline.py（受理 + 后台派发） │         │
              └──────┬──────────────────────────┘         │
                     │                                    │
 L2 领域层    ┌──────▼────────────┐   ┌───────────────────▼────┐
              │ M6 services/*     │   │ M4 delivery.py         │
              │ 消息/待办/提醒调度  │──▶│ 渠道选择、降级、投递记录 │
              └──────┬────────────┘   └───────┬────────────────┘
                     │                        │
              ┌──────▼────────┐   ┌───────────▼────────────────┐
              │ M3 classifier │   │ M4 notifiers/*             │
              │ 规则加载/匹配  │   │ 契约 + 注册表 + webhook/email│
              └──────┬────────┘   └───────────┬────────────────┘
                     │                        │
 L1 基础层    ┌──────▼────────────────────────▼────────────────┐
              │ M1 config/redact/logging │ M2 db.py/models.py   │
              └────────────────────────────────────────────────┘
                     ▲
 L0 由架构师维护的共享契约（不属于任何模块）：
              │ domain.py  errors.py  clock.py  context.py  app.py  main.py │
              └────────────────────────────────────────────────────────────┘
```

**关键性质**：M7 是唯一的编排者。下游模块（M3/M4/M6）互不调用上层，且 M3 与 M6 之间没有直接
依赖——分类结论通过共享类型 `ClassificationVerdict` 传递，而不是通过模块间调用。

---

## 3. 共享文件清单与处置（阶段 0，`fan-out` 之前必须做完）

不属于任何模块、且两个模块并行编辑会互相覆盖的文件。**处置方式只有两种：架构师在阶段 0 亲手写，
或独立成基础模块。本文档全部选择「架构师亲手写」，因为它们都是接口声明与装配，不是业务逻辑。**

| 文件 | 为什么是共享的 | 处置 | 谁可以改 |
|---|---|---|---|
| `pyproject.toml` | 依赖、打包、`pytest` 配置、`notify` 入口点；多模块同时加依赖 → 后写覆盖先写 | **架构师阶段 0** | 仅架构师 |
| `src/notify_hub/__init__.py` | 包标记 + `__version__` | **架构师阶段 0** | 仅架构师 |
| `src/notify_hub/domain.py` | 跨模块共享枚举与 `ClassificationVerdict`（M3↔M6↔M7 的契约） | **架构师阶段 0** | 仅架构师 |
| `src/notify_hub/errors.py` | 跨模块共享异常类型 | **架构师阶段 0** | 仅架构师 |
| `src/notify_hub/clock.py` | 跨模块共享时钟抽象 + `as_utc()`；测试可控时钟的唯一来源 | **架构师阶段 0** | 仅架构师 |
| `src/notify_hub/context.py` | **组合根**：`AppContext` + `build_context()`，需要全局视野，任何单模块都看不到 | **架构师阶段 0** | 仅架构师 |
| `src/notify_hub/app.py` | **组合根**：`create_app()` + lifespan（启动/停止顺序） | **架构师阶段 0** | 仅架构师 |
| `src/notify_hub/main.py` | uvicorn 入口 `python -m notify_hub` | **架构师阶段 0** | 仅架构师 |
| `src/notify_hub/__main__.py` | `python -m notify_hub` 的模块入口（转发到 `main()`） | **架构师阶段 0** | 仅架构师 |
| `tests/conftest.py` | 全模块共享 fixture；每个模块各自加 fixture 必然冲突 | **架构师阶段 0** | 仅架构师 |
| `.venv/` | 依赖安装位置（不入库） | **架构师阶段 0** 创建并 `pip install -e ".[dev]"` | 仅架构师 |
| `config.example.yaml`（**项目根**） | 配置示例，被 M1 测试与 M9 文档共同引用 | 模块 M1 | M1 |
| `rules.example.yaml`（**项目根**） | 规则示例，被 M3 测试与 M9 文档共同引用 | 模块 M3 | M3 |

### 3.1 对子代理的硬约束

> **禁止修改上表中「处置=架构师」的任何文件，也禁止修改其它模块的 `rules.example.yaml` /
> `config.example.yaml`。需要改动时停下来，在报告里写 `BLOCKERS`。**

### 3.2 `tests/conftest.py` 的 fixture 契约（冻结）

`conftest.py` 顶部**只允许导入标准库与阶段 0 文件**；所有对模块的导入必须写在 fixture 函数体内
（惰性导入）。理由：阶段 A 时模块尚不存在，若在模块顶部导入，`pytest` 会在**收集阶段**失败，
导致所有测试文件（包括已经写好测试的模块）都无法运行。

| fixture | 返回 | 依赖 | 说明 |
|---|---|---|---|
| `manual_clock` | `ManualClock` | 无 | 起始时间固定 `2024-01-01T00:00:00+00:00` |
| `tmp_settings` | `Settings` | M1（惰性） | 临时目录下的 `data/notify.db` + `rules.yaml`；**无渠道、`default_channel=None`**；提醒参数秒级化（scan=1 / first=2 / interval=3） |
| `db` | `Database` | M2（惰性） | 已 `init_schema()`，指向 `tmp_settings.db_path` |
| `make_recording_notifier` | 工厂 callable | M4（惰性） | **签名与 `RecordingNotifier.__init__` 对齐**：`channel_id` 可位置也可关键字传递，其余关键字（`ok`/`delay`/`error_reason`/`receipt`/`raise_exc`）透传。三种用法都合法：`make_recording_notifier()`、`make_recording_notifier("slow", delay=2.0)`、`make_recording_notifier(channel_id="slow", delay=2.0)` |
| `recording_notifier` | `RecordingNotifier` | M4（惰性） | `channel_id="recording"`、投递恒成功的缺省替身 |
| `make_context` | 工厂 callable | M1–M6（惰性） | `make_context(settings, *, clock=None, notifiers=())` → 用给定渠道注册表装配 |
| `ctx` | `AppContext` | M1–M6（惰性） | `make_context(tmp_settings, clock=manual_clock, notifiers=[recording_notifier])`，内部走 **`build_test_context`**（即 `pipeline` 为 `run_inline=True`） |
| `api_client` | `TestClient` | M7（惰性） | `TestClient(create_api_app(ctx))`（`create_api_app` 不启动后台线程） |

**模块不得编辑 `conftest.py`**；需要额外 fixture 时，在自己的测试文件里定义。

---

## 4. 全局冻结契约（阶段 0 交付，所有模块依赖）

### 4.1 `src/notify_hub/domain.py`

```python
class Level(str, Enum):
    INFO = "info"; WARNING = "warning"; ERROR = "error"

class TodoStatus(str, Enum):
    PENDING = "pending"; DONE = "done"

class AckReason(str, Enum):
    """消息为什么需要（或不需要）进入待办。"""
    CALLER_DECLARED = "caller_declared"   # 调用方声明 need_ack=true
    RULE = "rule"                         # 命中规则的处置动作决定
    NONE = "none"                         # 不入待办

class DeliveryEvent(str, Enum):
    FIRST_NOTICE = "first_notice"; REMINDER = "reminder"

class DeliveryTarget(str, Enum):
    MESSAGE = "message"; TODO = "todo"

class TodoEventKind(str, Enum):
    CREATED = "created"; REMINDER = "reminder"; COMPLETED = "completed"

@dataclass(frozen=True)
class ClassificationVerdict:
    """M3 的唯一输出、M6/M7 的唯一输入。"""
    rule_id: str | None            # None 表示未命中任何规则、套用了 defaults
    category: str
    labels: tuple[str, ...]
    need_ack: bool
    ack_reason: AckReason
    preferred_channel: str | None
```

### 4.2 `src/notify_hub/errors.py`

```python
class NotifyHubError(Exception): ...            # 本项目所有自定义异常的基类
class ConfigurationError(NotifyHubError): ...   # 配置缺失/非法；消息中不得含凭据值
class RuleFileError(NotifyHubError): ...        # 规则文件语法/语义错误，消息需定位到具体位置
class TodoNotFound(NotifyHubError): ...
class MessageNotFound(NotifyHubError): ...
```

### 4.3 `src/notify_hub/clock.py`

```python
UTC = timezone.utc

def as_utc(value: datetime) -> datetime:
    """把可能 naive 的时间戳按 UTC 归一化为 aware；已是 aware 则转换为 UTC。"""

class Clock(Protocol):
    def now(self) -> datetime: ...            # 返回 tz-aware UTC

class SystemClock:
    def now(self) -> datetime: ...            # datetime.now(UTC)

class ManualClock:
    def __init__(self, start: datetime | None = None) -> None
    def now(self) -> datetime
    def advance(self, seconds: float) -> None
    def set(self, moment: datetime) -> None
```

不变量：`ManualClock` 是测试中唯一被允许的时间来源；`advance` 只前进。

### 4.4 `src/notify_hub/context.py`

```python
@dataclass(frozen=True)
class AppContext:
    settings: Settings
    clock: Clock
    logger: logging.Logger
    db: Database                    # M2
    classifier: RuleClassifier      # M3
    registry: NotifierRegistry      # M4
    delivery: DeliveryService       # M4
    messages: MessageService        # M6
    todos: TodoService              # M6
    pipeline: IngestPipeline        # M7
    scheduler: ReminderScheduler    # M6

def build_context(settings: Settings, *, clock: Clock | None = None,
                  registry: NotifierRegistry | None = None) -> AppContext:
    """
    组合根。装配顺序固定：
      1. secrets = settings 中所有已解析的凭据值（用于日志脱敏）
      2. logger  = setup_logging(settings.log_level, secrets=secrets)
      3. db      = Database(settings.db_path); db.init_schema()
      4. clock   = clock or SystemClock()
      5. classifier = RuleClassifier(RuleLoader(settings.rules_path,
                            poll_interval=settings.rules_poll_interval_seconds,
                            clock=clock, logger=logger), logger=logger)
      6. registry = registry or NotifierRegistry()
         若 registry is None -> registry.build_from_specs(settings.channels, secrets=secrets)
         若外部传入 registry -> 跳过 build_from_specs（测试注入用）
      7. delivery = DeliveryService(db, registry, default_channel=settings.default_channel,
                            channel_order=registry.ids(), clock=clock, logger=logger, secrets=secrets)
      8. messages = MessageService(db, clock)
      9. todos    = TodoService(db, clock, logger=logger)
     10. pipeline = IngestPipeline(classifier=classifier, messages=messages, todos=todos,
                            delivery=delivery, clock=clock, logger=logger, run_inline=False)
     11. scheduler = ReminderScheduler(todos=todos, delivery=delivery, clock=clock,
                            settings=settings.reminders, logger=logger)
    """

def build_test_context(settings: Settings, *, clock: Clock | None = None,
                       registry: NotifierRegistry | None = None) -> AppContext:
    """同 build_context，但 pipeline 以 run_inline=True 构造（测试确定性用）。"""
```

### 4.5 `src/notify_hub/app.py` 与 `main.py`

```python
def create_app(settings: Settings | None = None, *, ctx: AppContext | None = None) -> FastAPI:
    """settings 为 None 时调用 load_settings()。ctx 为 None 时调用 build_context(settings)。

    lifespan 启动顺序：classifier.start() -> pipeline.start() -> scheduler.start()
    lifespan 关闭顺序：scheduler.stop() -> pipeline.stop() -> classifier.stop() -> db.dispose()
    路由挂载：api 路由（M7） + web 路由（M8），二者都以 ctx 为依赖来源。
    """

def main() -> None:
    """`python -m notify_hub` / console script 入口：load_settings() -> uvicorn.run(create_app(...))。"""
```

依赖注入方式（冻结）：路由通过 **`request.app.state.ctx`** 取 `AppContext`；
`create_api_app(ctx)` / `create_web_app(ctx)` 会把 `ctx` 写入 `app.state.ctx`。
测试用 `app.dependency_overrides` 也算允许，但 `app.state.ctx` 是主路径。

### 4.6 `Notifier` 契约（M4 交付，M6/M7 依赖，此处冻结签名）

```python
@dataclass(frozen=True)
class ChannelCapabilities:
    supports_rich_text: bool = False
    max_body_length: int | None = None
    supports_headers: bool = False

@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    receipt: str | None = None
    error_reason: str | None = None
    @classmethod
    def success(cls, receipt: str | None = None) -> "DeliveryResult": ...
    @classmethod
    def failure(cls, reason: str) -> "DeliveryResult": ...

@dataclass(frozen=True)
class NotificationMessage:
    title: str
    body: str
    level: Level
    source: str
    occurred_at: datetime                       # tz-aware UTC
    kind: DeliveryEvent = DeliveryEvent.FIRST_NOTICE
    todo_id: int | None = None
    overdue_seconds: float | None = None
    category: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)

class Notifier(Protocol):
    channel_id: str
    def capabilities(self) -> ChannelCapabilities: ...
    def send(self, msg: NotificationMessage) -> DeliveryResult: ...
```

**不变量**：`send()` **MUST NOT 抛异常**——任何渠道侧/网络侧错误都必须转成 `DeliveryResult.failure()`。

---

## 5. 模块清单、批次与预算

> **已批准的开工范围（2026 用户裁定）**：**先做 6 个核心模块 M1 / M2 / M3 / M4 / M6 / M7**
> （≈24 次派发），跑完阶段 B 后向用户汇报，再由用户决定 M5（CLI）/ M8（Web）/ M9（文档）。
> 偏差 D-1（进程内队列替代 `BackgroundTasks`）已获用户认可。
>
> **由此产生的两处临时状态**（批准 M8/M5 后由架构师解除，模块不得自行处理）：
> 1. `src/notify_hub/app.py` 当前**不挂载 web 路由**（M8 未实施）。批准 M8 后由架构师在
>    `create_app()` 中加入 `app.include_router(create_web_router(ctx))` 一行。
>    **禁止**为了让 `app.py` 能跑而写 `try/except ImportError` 之类的静默降级。
> 2. `pyproject.toml` 的 `[project.scripts] notify` 指向尚未实现的 `notify_hub.cli:main`。
>    这不影响安装与测试；批准 M5 后自然生效。
>
> M8 的模块规格（第 6 节）与 M9 的文档契约保持原样冻结，批准后直接按 A→B 执行，无需重新设计。

| # | 模块 | 实现路径（`subagent_dev`） | 测试路径（`subagent_verify`） | 依赖 | 批次 |
|---|---|---|---|---|---|
| M1 | 配置、脱敏与日志基础 | `src/notify_hub/config.py`、`redact.py`、`logging_setup.py`、`config.example.yaml`（项目根） | `tests/test_config.py`、`tests/test_redact.py` | — | 1 |
| M2 | 数据模型与持久化 | `src/notify_hub/db.py`、`models.py` | `tests/test_models.py` | — | 1 |
| M3 | 分类器（规则加载/匹配/热加载） | `src/notify_hub/classifier/*`、`rules.example.yaml`（项目根） | `tests/test_classifier.py` | — | 1 |
| M5 | 命令行客户端 | `src/notify_hub/cli.py` | `tests/test_cli.py` | — | 1 |
| M4 | 通知投递层（契约/注册表/渠道/降级/记录） | `src/notify_hub/notifiers/*`、`src/notify_hub/delivery.py` | `tests/test_notifiers.py`、`tests/test_delivery.py` | M1, M2 | 2 |
| M6 | 消息/待办领域服务 + 提醒调度 | `src/notify_hub/services/*` | `tests/test_services.py`、`tests/test_todos.py` | M2, M4 | 3 |
| M7 | HTTP 接入层 + 受理编排 | `src/notify_hub/api/*`、`pipeline.py` | `tests/test_ingest.py`、`tests/test_api_todos.py` | M1–M4, M6 | 4 |
| M8 | Web 待办界面 | `src/notify_hub/web/*` | `tests/test_web.py` | M2, M6 | 4 |
| M9 | 文档与部署 | `README.md`、`docs/*.md` | `tests/test_docs_contract.py` | M1–M8 | 5 |
| — | 跨模块集成（阶段 D） | — | `tests/test_integration.py`、`tests/test_e2e.py` | 全部 | D |

**模块数：9。预计派发次数：≈ 36**（每模块：模块测试 + 实现 + 集成 + 审查，`9 × 4`；
精确下限为 9 次 verify + 9 次 dev + 1 次集成 + 1 次 review = 20，余量为返工与冲突裁决）。
**超过 8 个模块的阈值，已按流程单独向用户确认。**

批次内可完全并行（文件边界互不重叠，见第 9 节）；批次之间是硬屏障。

---

## 6. 模块规格

### 模块 M1：配置、脱敏与日志基础

**文件边界**
- 实现路径：`src/notify_hub/config.py`、`src/notify_hub/redact.py`、`src/notify_hub/logging_setup.py`、`config.example.yaml`
- 测试路径：`tests/test_config.py`、`tests/test_redact.py`

#### 1. 功能

把「服务怎么配、凭据从哪来、日志里绝不许出现什么」这三件事收敛到一处。**不负责**：渠道适配器的
构造（M4）、规则文件的内容语义（M3）、提醒的调度行为（M6，但**提醒参数的合法关系**在此校验）。

#### 2. 接口

```python
# config.py
@dataclass(frozen=True)
class ChannelSpec:
    id: str                              # 渠道实例标识，配置内唯一，被规则 channel 字段引用
    type: str                            # 适配器类型："webhook" | "email"
    enabled: bool = True
    params: Mapping[str, Any] = field(default_factory=dict)       # 非凭据参数
    credentials: Mapping[str, str | None] = field(default_factory=dict)
        # 逻辑名 -> 已解析的凭据值；None 表示「声明了环境变量但未设置」
    @property
    def credentials_complete(self) -> bool:
        """所有声明的凭据都已解析出非空值。凭据缺失 = 渠道不可用，不是启动错误。"""

@dataclass(frozen=True)
class ReminderSettings:
    scan_interval_seconds: float = 60.0
    first_reminder_after_seconds: float = 1800.0     # 首次提醒门槛（自 first_notified_at 起）
    reminder_interval_seconds: float = 3600.0        # 后续提醒间隔

@dataclass(frozen=True)
class Settings:
    db_path: Path
    rules_path: Path
    rules_poll_interval_seconds: float = 5.0
    host: str = "127.0.0.1"
    port: int = 8000
    default_channel: str | None = None
    channels: tuple[ChannelSpec, ...] = ()
    reminders: ReminderSettings = ReminderSettings()
    log_level: str = "INFO"

def load_settings(path: str | Path | None = None, *, env: Mapping[str, str] | None = None) -> Settings:
    """从 YAML 加载配置。path 为 None 时取环境变量 NOTIFY_HUB_CONFIG，再退回 ./config.yaml。
    env 为 None 时取 os.environ（测试注入用）。
    抛 ConfigurationError：文件不存在 / 非法 YAML / 缺必填键 / default_channel 指向不存在的渠道 /
    提醒参数非法（见下）。消息中 MUST NOT 出现任何凭据值。"""

def credential_values(settings: Settings) -> tuple[str, ...]:
    """返回用于日志/记录脱敏的**全部密钥字面量**。

    对每个渠道的每个已解析凭据值 `v`：
      1. 先加入 `v` 本身（非空时）；
      2. 若 `v` 形如 URL，再按 `extract_url_secrets(v)` **展开**，
         把其中内嵌的凭据成分也作为**独立密钥**加入。
    最后全局去重，剔除 None 与空串。顺序不作保证。

    **为什么必须展开（这是修复一个已确认的 P1 泄漏的关键）**：webhook 渠道的凭据值是
    「完整 URL」（如 `https://h/p?access_token=TOK`），而真正会出现在日志与平台错误信息里的
    往往是 **URL 中的裸 token**。`redact_text` 做的是**子串匹配**——只把整段 URL 当密钥时，
    文本里的裸 token 匹配不上，于是明文落进 `DeliveryRecord.error_reason`，再经
    `GET /api/v1/messages/{id}` 与日志外泄（已实测复现）。

    调用方可依赖：包含全部已解析凭据值**及其 URL 内嵌凭据成分**，且每个值只出现一次。"""

# redact.py
MASK = "***"
SENSITIVE_KEYS = ("token", "password", "passwd", "secret", "sign", "key",
                  "access_token", "api_key", "authorization")

def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """把 secrets 中每个非空值在 text 中的所有出现替换为 MASK。"""

def redact_url(url: str, secrets: Iterable[str] = ()) -> str:
    """掩码 URL 中的凭据，三处规则全部**无条件生效**（不依赖 secrets 参数）：
      1. userinfo 的密码部分：`scheme://user:pass@host` -> `scheme://user:***@host`
         （只掩码密码，保留用户名；无密码的 userinfo 不动）
      2. query 中键名命中 SENSITIVE_KEYS 的值（大小写不敏感，含后缀匹配如 access_token）
      3. 再叠加 redact_text(url, secrets) 处理调用方显式给出的密钥子串
    第 1、2 条是无条件的：即使 secrets 为空，也 MUST 掩码。这是防止含 basic-auth 的
    webhook 地址或带 token 的地址被写进日志/投递记录的兜底。"""

def extract_url_secrets(url: str, *, min_length: int = 6) -> tuple[str, ...]:
    """从 URL 中提取应被视为**独立密钥**的成分（供 `credential_values` 展开用）：
      - query 中键名命中 SENSITIVE_KEYS 的值（大小写不敏感、含后缀匹配）
      - userinfo 的密码部分
    输入不是合法 URL 时返回空元组。结果去重、剔除空串。

    **`min_length=6` 是刻意的安全阀，不是随手取的**：`SENSITIVE_KEYS` 含通用键名 `key`，
    若把 `?key=1` 的值 `"1"` 收进全局密钥集合，`redact_text` 会把**日志里所有出现的 `1`**
    都打成 `***`，日志立刻不可读。长度不足 `min_length` 的成分**不**进入全局密钥集合
    （它们仍会被 `redact_url` 在 URL 内部按键名掩码，所以不会因此暴露）。

    **「是否是 URL」的判定（冻结，勿自行发挥）**：以 `urlsplit(url).scheme` **非空**为准。
    因此裸 query 串（如 `"?ACCESS_TOKEN=abcdefgh"`）**不是** URL → 返回空元组。
    无 scheme 的输入一律按「非 URL」处理，不抛异常。"""

def redact_mapping(data: Mapping[str, Any], secrets: Iterable[str] = ()) -> dict[str, Any]:
    """递归（dict/list/str）脱敏，键名命中 SENSITIVE_KEYS 时整值替换为 MASK。"""

def redact_exception(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    """把异常渲染成 '<类型>: <脱敏后的消息>'。"""

# logging_setup.py
def setup_logging(level: str = "INFO", *, secrets: Iterable[str] = ()) -> logging.Logger:
    """配置根 logger（仅一次，重复调用不叠加 handler），安装 SecretFilter。返回 'notify_hub' logger。"""

def get_logger(name: str = "notify_hub") -> logging.Logger: ...

class SecretFilter(logging.Filter):
    """对每条 LogRecord 的 msg/args 施加 redact_text。"""
```

**不变量**
- `load_settings` 是纯函数：不读全局状态（除 `env` 参数），不写文件，不建立网络连接。
- 返回的 `Settings` 中，**路径类字段（`db_path`、`rules_path`）与数值类字段永不为 None**；
  「未配置」只允许由两类字段表达：`default_channel` 为 `None`，以及 `channels[].credentials`
  的某个值为 `None`（声明了环境变量但未设置）。这三种 None 都是**合法且必须支持**的状态，
  不得因此报错——`tests/conftest.py` 的 `tmp_settings` 就是以 `default_channel: null` 构造的。
- 凭据值只以 `credentials` 映射的值形式存在，不出现在 `params` 里。
- `redact_*` 的输出对同一输入是确定性的；且**输出中不包含任何输入 secret 的完整值**。

**错误契约**
| 条件 | 异常 | 消息要求 |
|---|---|---|
| 配置文件不存在 | `ConfigurationError` | 含绝对路径 |
| YAML 语法错误 | `ConfigurationError` | 含文件名 + 字面量 `line` + **行号数字**（建议 `problem_mark.line + 1`，即 1-based；测试只断言含文件名与 `line`，不锁定具体数值） |
| 缺 `db_path` / `rules_path` | `ConfigurationError` | 含缺失键名 |
| `default_channel` 不在 `channels` 中 | `ConfigurationError` | 含该值与该渠道列表（渲染形式不限，只要渠道 id 可被读到） |
| 同一 `channels[].id` 重复 | `ConfigurationError` | 含重复 id |
| `reminder_interval_seconds < scan_interval_seconds` | `ConfigurationError` | **消息必须同时出现两个参数名与两个数值**（tasks 6.6） |
| `first_reminder_after_seconds < scan_interval_seconds` | `ConfigurationError` | 同上 |

**边界语义（明确裁定，勿自行发挥）**：上述两条提醒参数校验都是**严格小于才报错**；
`reminder_interval_seconds == scan_interval_seconds` 与
`first_reminder_after_seconds == scan_interval_seconds` 都是**合法**配置（意味着每个扫描周期
都可以提醒一次）。即判定式必须是 `<` 而不是 `<=`。

#### 3. 内部实现

- 配置文件形状（`config.example.yaml`，被 M9 文档引用，必须与解析器一致）：
  ```yaml
  server: { host: 127.0.0.1, port: 8000, log_level: INFO }
  storage: { db_path: ./data/notify.db }
  rules: { path: ./rules.yaml, poll_interval_seconds: 5 }
  reminders:
    scan_interval_seconds: 60
    first_reminder_after_seconds: 1800
    reminder_interval_seconds: 3600
  default_channel: webhook
  channels:
    - id: webhook
      type: webhook
      enabled: true
      params: { url_env: NOTIFY_WEBHOOK_URL, field_map: {}, headers: {} }
      credentials: { url: NOTIFY_WEBHOOK_URL }
    - id: email
      type: email
      enabled: false
      params: { host: smtp.example.com, port: 587, use_tls: true,
                sender: notify@example.com, recipients: [me@example.com] }
      credentials: { password: NOTIFY_SMTP_PASSWORD }
  ```
  说明：`credentials` 的**值**是环境变量名；解析后 `credentials["password"]` 是环境变量的**值**
  或 `None`。`params` 只放非凭据参数。
- 路径字段（`db_path`、`rules.path`）相对**配置文件所在目录**解析，`resolve()` 成绝对路径。
- `setup_logging` 用模块级 `_configured` 标志防重复添加 handler；`SecretFilter` 装在根 logger 的
  每个 handler 上（不只 logger 上），保证 `caplog` 与真实输出都被脱敏。
- 依赖白名单（M1 修复 P1 后已更新）：标准库 + `pyyaml` + **同属 M1 的**
  `errors.ConfigurationError` 与 `redact.extract_url_secrets`。
  **禁止**引入新依赖；**禁止**导入 M1 之外的任何本项目模块。
  （`config.py` 导入 `notify_hub.redact` 是同模块内部依赖，无环；最初写成「禁止导入本项目
  其它模块」是笔误，与 P1 修复后的实现不符，此处按实现同步修正。）

#### 4. 验证方法（`subagent_verify` 的测试规格）

测试文件：`tests/test_config.py`、`tests/test_redact.py`；命令：`.venv/bin/python -m pytest tests/test_config.py tests/test_redact.py`。

必须覆盖的**可观察结果**：
1. 用 `tmp_path` 写出一份合法 YAML（含上述全部键）→ `load_settings(path)` 返回的 `Settings`
   字段逐项等于文件中的值；`db_path` 是绝对路径且父目录为配置文件所在目录。
2. 相对路径解析：配置文件在 `tmp_path/a/config.yaml`、`db_path: ./d/x.db` → 结果为 `tmp_path/a/d/x.db`。
3. 环境变量解析：`env={"NOTIFY_WEBHOOK_URL": "https://h/x?token=SECRET123"}` →
   `channels[0].credentials["url"]` 等于该值、`credentials_complete is True`；
   同一配置在 `env={}` 下 → 值为 `None`、`credentials_complete is False`，**且不抛异常**。
4. 错误契约逐条：文件不存在、YAML 语法错、缺 `db_path`、`default_channel` 指向不存在的渠道、
   渠道 id 重复、`reminder_interval_seconds(10) < scan_interval_seconds(60)` —— 每种都断言
   抛 `ConfigurationError`，且消息包含规格中要求的字符串（对提醒参数那条，断言消息同时含
   `reminder_interval_seconds`、`scan_interval_seconds` 与 `10`、`60`）。
5. `credential_values()`：
   - **两个渠道、两个互不相同的**已解析凭据 + 1 个未设置的渠道 → 返回元组长度为 2、
     `set(结果) == {值A, 值B}`、不含空串；
   - **两个渠道解析出同一个值** → 返回元组长度 **1**（全局去重，这是裁定要点）；
   - 全部渠道凭据都未设置 → 返回空元组。
   - **上面这两条必须使用「非 URL 的不可读串」作为凭据值**（例如 `"T0KEN_A_VALUE"`、
     `"SHARED_T0KEN_VALUE"`），**不得**用 `https://…?token=…` 这种 URL 形态。
     理由：URL 形态的值会被本函数**展开**，额外贡献条目，使长度断言与展开行为纠缠在一起。
     隔离后，「去重」由本条验证、「展开」由下一条与第 5b 条验证，各测一个性质。
   - **URL 凭据展开（P1 修复的回归点，必须覆盖）**：某渠道
     `credentials={"url": "https://h/p?access_token=TOPSECRET123"}` → 返回值**同时包含**
     完整 URL 与裸 token `TOPSECRET123`；
   - 展开带 `min_length` 安全阀：`credentials={"url": "https://h/p?key=1"}` →
     裸值 `"1"` **不**进入结果（否则会把日志里所有 `1` 都打成 `***`），
     但完整 URL 仍在结果中。
5b. `extract_url_secrets()`（新增，安全关键）：
   - `"https://h/p?access_token=abc123def&x=1"` → 含 `abc123def`、不含 `"1"`；
   - `"https://user:pa55w0rd@h/p"` → 含 `pa55w0rd`；
   - 键名大小写与后缀均匹配（**示例必须是完整 URL**——裸 query 串无 scheme，按上面的判定
     不算 URL，会返回空元组）：`"https://h/p?ACCESS_TOKEN=abcdefgh"` 与
     `"https://h/p?my_token=abcdefgh"` 都能提取出 `abcdefgh`；
   - **裸 query 串不是 URL**：`extract_url_secrets("?ACCESS_TOKEN=abcdefgh")` → `()`
     （且不抛异常）；
   - 长度不足：`"?token=abc"` → 空元组（`min_length=6` 生效）；
   - 非 URL 输入（`"not a url"`、`""`）→ 空元组，不抛异常。
6. **脱敏（安全关键，必须逐项断言）**：
   - `redact_url("https://oapi.dingtalk.com/robot/send?access_token=abc123def&x=1")` 的输出
     不含 `abc123def`，且含 `access_token=***`。
   - `redact_url("https://user:pa55w0rd@h/p", secrets=["pa55w0rd"])` 输出不含 `pa55w0rd`。
   - `redact_text("auth failed for p@ss", ["p@ss"])` 不含 `p@ss`。
   - `redact_mapping({"password": "s3cr3t", "nested": [{"token": "t0k"}]})` 中两处均为 `MASK`。
   - `redact_exception(ValueError("bad p@ss"), ["p@ss"])` 不含 `p@ss` 且含 `ValueError`。
7. **日志端到端脱敏**：`logger = setup_logging("INFO", secrets=["TOPSECRET"]); logger.error("url=%s", "https://h/?token=TOPSECRET")`，
   用 `caplog` 断言格式化后的记录中不含 `TOPSECRET`。（对应 tasks 1.2 / 9.6 的服务端一半。）
8. `setup_logging` 幂等：连续调用两次后 `len(logging.getLogger().handlers)` 不翻倍。

**至少 2 个异常场景**：(a) `config.yaml` 内容为 `- 这不是映射`（YAML 合法但结构错）→
`ConfigurationError`；(b) `channels` 中某渠道的 `credentials` 声明了 3 个环境变量而全部未设置 →
`credentials_complete is False` 且凭据映射的值为 `None`（不抛错）。

**完成后必须成立**：上述命令全绿；且 `grep -r "TOPSECRET" tests/` 只出现在断言中，不出现在
被断言为「输出」的字符串构造里。

---

### 模块 M2：数据模型与持久化

**文件边界**
- 实现路径：`src/notify_hub/db.py`、`src/notify_hub/models.py`
- 测试路径：`tests/test_models.py`

#### 1. 功能

定义四张表与数据库访问入口。**不负责**：业务查询（M6 services）、投递记录的内容语义（M4）、
时间语义（由 `clock.as_utc()` 统一）。**不做** Alembic 迁移链——首版只需要幂等的建表。

#### 2. 接口

```python
# db.py
class Database:
    def __init__(self, db_path: str | Path, *, echo: bool = False) -> None:
        """SQLite 引擎；必须带 connect_args={"check_same_thread": False}（后台线程要访问）。"""
    @property
    def engine(self) -> Engine: ...
    def init_schema(self) -> None:
        """幂等建表 + 建索引。对同一路径重复调用不报错、不丢数据。"""
    @contextmanager
    def session(self) -> Iterator[Session]:
        """yield 一个 `sqlmodel.Session`；正常退出时 commit，异常时 rollback 并原样抛出；总是 close。"""
    def dispose(self) -> None: ...
```

**`expire_on_commit=False` 是硬要求（跨模块关键，勿省）**：`session()` 必须在
`sessionmaker(..., expire_on_commit=False)` 之上创建 Session。理由：`MessageService.create()`
与 `TodoService.ensure_for_message()` 都在 `with db.session()` 内部创建行、**退出后再由调用方
读取属性**。若沿用 SQLAlchemy 的默认 `expire_on_commit=True`，commit 会让所有实例过期，
调用方一读属性就抛 `DetachedInstanceError`——这会同时打断 M6 与 M7。
（该坑已在 M2 的测试编写过程中实际踩到并确认。）

**验证段的排序键（冻结）**：第 7 条「按关联查询」中，
`DeliveryRecord` 按 `attempted_at` **升序**、`TodoEvent` 按 `occurred_at` **升序**。

**模型缺省值是契约的一部分**：`Todo.status` 缺省 `"pending"`、`Todo.reminder_count` 缺省 `0`、
`Message.level` 缺省 `Level.INFO` 的 value。实现必须给出这些默认，使调用方不传也能插入成功。

**部分唯一索引的判定以 DDL 为准**：仅靠「`dedup_key=None` 的两条都能插入」**无法**区分普通唯一
索引与部分唯一索引（两者在该用例下行为相同）。因此测试必须直接读 `sqlite_master`，断言该唯一
索引的 DDL **含** `WHERE dedup_key IS NOT NULL AND status='pending'`。

```python
# models.py  —— 全部使用 SQLModel，table=True；所有时间列为 DateTime(timezone=True)
class Message(SQLModel, table=True):
    __tablename__ = "messages"
    id: int | None                 # PK
    source: str                    # index
    title: str
    body: str | None
    level: str                     # Level 的 value
    need_ack_declared: bool        # 调用方原始声明
    dedup_key: str | None
    occurred_at: datetime          # 来源侧发生时间（缺省=接收时间）
    received_at: datetime          # 服务接收时间
    meta: dict[str, Any]          # JSON，列名 meta_json
    rule_id: str | None            # 命中规则 id；None=套用 defaults
    category: str | None
    labels: list[str]             # JSON，列名 labels_json
    needs_ack: bool | None         # 分类判定结果
    ack_reason: str | None         # AckReason 的 value
    preferred_channel: str | None

class Todo(SQLModel, table=True):
    __tablename__ = "todos"
    id: int | None                 # PK
    message_id: int                # FK messages.id，唯一
    source: str
    dedup_key: str | None
    category: str | None
    title: str
    status: str                    # TodoStatus；默认 "pending"
    ack_reason: str                # AckReason
    preferred_channel: str | None  # 提醒使用的渠道（「该待办对应的通知渠道」）
    created_at: datetime
    first_notified_at: datetime
    last_notified_at: datetime
    reminder_count: int            # 默认 0
    completed_at: datetime | None

class DeliveryRecord(SQLModel, table=True):
    __tablename__ = "deliveries"
    id: int | None                 # PK
    message_id: int | None         # FK，可空
    todo_id: int | None            # FK，可空
    channel_id: str | None         # None = 没有任何可用渠道
    attempted_at: datetime
    ok: bool
    error_reason: str | None       # 必须已脱敏
    receipt: str | None
    is_preferred: bool
    is_fallback: bool
    fallback_reason: str | None    # 必须已脱敏
    event: str                     # DeliveryEvent 的 value

class TodoEvent(SQLModel, table=True):
    __tablename__ = "todo_events"
    id: int | None                 # PK
    todo_id: int                   # FK todos.id
    kind: str                      # TodoEventKind 的 value
    occurred_at: datetime
    detail: dict[str, Any] | None  # JSON，列名 detail_json
    channel_id: str | None
    delivery_ok: bool | None
```

**不变量**
- `Todo` 上存在**部分唯一索引**：`UNIQUE (source, dedup_key) WHERE dedup_key IS NOT NULL AND status = 'pending'`
  （SQLite 用 `sqlite_where`，Postgres 用 `postgresql_where` 声明同一条件）。
  含义：同一 `source` + 非空 `dedup_key` 在**待完成**状态下至多一条待办；`dedup_key` 为 NULL 的行
  永不受约束；待办完成后允许新的同名待办。
- `Todo.message_id` 在 `todos` 内唯一（一条消息至多一条待办）。
- 读取出的时间列必须经 `clock.as_utc()` 归一化后才能参与计算；`models.py` 不提供该归一化，
  调用方负责（见 4.3）。

**错误契约**
- 违反唯一约束时向上抛出 `sqlalchemy.exc.IntegrityError`（**不包装、不吞掉**）；调用方
  （M6）负责先查后插以给出可读语义。
- `Database.session()` 在异常路径必须 rollback 后再抛，且 Session 一定 close。

#### 3. 内部实现

- 引擎：`create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}, echo=echo)`；
  `db_path` 的父目录不存在时由 `init_schema()` 创建（`mkdir(parents=True, exist_ok=True)`）。
- 建表：`SQLModel.metadata.create_all(engine)`；部分唯一索引若 `create_all` 不生成，则在
  `init_schema()` 中显式 `Index(...).create(engine, checkfirst=True)`。
- SQLite 需开启外键约束：在 `connect` 事件里执行 `PRAGMA foreign_keys=ON`。
- JSON 列：SQLModel/SQLAlchemy 的 `Column(JSON)`；用 `Field(sa_column=Column(JSON))` 显式声明，
  列名与字段名不一致时用 `sa_column=Column("meta_json", JSON)`。
- 依赖：`sqlmodel`、`sqlalchemy`、`src/notify_hub/clock.py`。
  **禁止**：导入 M1/M3/M4/M6/M7 的模块；引入 Alembic；在 `models.py` 中写业务方法。

#### 4. 验证方法

测试文件：`tests/test_models.py`；命令：`.venv/bin/python -m pytest tests/test_models.py`。

必须覆盖的可观察结果：
1. `Database(tmp_path/"x.db").init_schema()` → `inspect(engine).get_table_names()` 包含
   `messages`、`todos`、`deliveries`、`todo_events` 四张表。
2. **幂等**：连续调用 `init_schema()` 两次不抛错；插入一行后再次调用，该行仍可查（tasks 2.4）。
3. 消息往返：写入一条含 `meta={"k": [1, 2]}`、`labels=["a", "b"]` 的 `Message`，读回后
   `meta`/`labels` 结构相等；`category`/`rule_id`/`needs_ack`/`ack_reason` 逐字段相等（tasks 2.1）。
4. **时间往返（关键）**：写入 `occurred_at=datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)`，
   读回后 `as_utc(row.occurred_at) == 写入值`。（SQLite 会丢时区，这条测试保护 1.2 节的约定。）
5. **部分唯一索引（tasks 2.2）**：
   - 插入两条 `source="s"`、`dedup_key="k"`、`status="pending"` → 第二次抛 `IntegrityError`；
   - 插入两条 `dedup_key=None` → 均可插入；
   - 把第一条置为 `status="done"` 后再插入同 `(s, k)` 的 pending → 成功；
   - 插入 `source="s2"`、`dedup_key="k"` → 成功（source 参与唯一性）。
6. `Todo.message_id` 唯一：为同一 `message_id` 插入第二条待办 → `IntegrityError`。
7. 按关联查询（tasks 2.3）：为同一 `message_id` 写 2 条 `DeliveryRecord`、为同一 `todo_id` 写
   2 条 `DeliveryRecord`、为同一 `todo_id` 写 3 条 `TodoEvent`，分别按 `message_id` / `todo_id`
   过滤，断言返回条数与时间顺序。
8. `Database.session()` 异常语义：在 `with db.session() as s:` 中插入后抛 `RuntimeError` →
   断言该行未落库，且异常原样传出。

**至少 2 个异常场景**：(a) 上面第 5 条的 `IntegrityError`；(b) `init_schema()` 在
`db_path` 的父目录不存在时自动创建目录并成功建表（而非 `OperationalError`）。

**完成后必须成立**：命令全绿；`sqlite3 <tmp>/x.db ".schema todos"` 中出现带 `WHERE` 的唯一索引。

---

### 模块 M3：分类器

**文件边界**
- 实现路径：`src/notify_hub/classifier/__init__.py`、`rules.py`、`engine.py`、`loader.py`、`rules.example.yaml`
- 测试路径：`tests/test_classifier.py`

#### 1. 功能

把原始消息映射为 `ClassificationVerdict`，并支持规则文件热加载与坏配置降级。
**不负责**：持久化分类结果（M6）、决定实际投递渠道（M4）、消息校验（M7）。

#### 2. 接口

```python
# rules.py
@dataclass(frozen=True)
class MatchCondition:
    field: str            # "source" | "level" | "title" | "body"
    mode: str             # "equals" | "contains"
    values: tuple[str, ...]   # 多个候选值之间是「或」

@dataclass(frozen=True)
class Rule:
    id: str
    match: tuple[MatchCondition, ...]   # 多个条件之间是「与」；空元组表示匹配一切
    category: str
    labels: tuple[str, ...] = ()
    need_ack: bool = False
    channel: str | None = None

@dataclass(frozen=True)
class RuleDefaults:
    category: str = "uncategorized"
    labels: tuple[str, ...] = ()
    need_ack: bool = False
    channel: str | None = None

@dataclass(frozen=True)
class RuleSet:
    defaults: RuleDefaults
    rules: tuple[Rule, ...]
    case_sensitive: bool
    source_path: Path
    loaded_at: datetime

DEFAULT_RULESET: RuleSet   # 内置兜底：defaults=RuleDefaults(), rules=(), case_sensitive=False,
                           # source_path=Path("<builtin>")

def parse_ruleset(data: Mapping[str, Any], *, source_path: Path, loaded_at: datetime) -> RuleSet:
    """把已解析的 YAML 映射转成 RuleSet。抛 RuleFileError，消息定位到规则下标/规则 id/缺失键。"""

# engine.py
class RuleEngine:
    def __init__(self, ruleset: RuleSet) -> None: ...
    def classify(self, *, source: str, level: Level, title: str, body: str | None,
                 declared_need_ack: bool) -> ClassificationVerdict: ...

# loader.py
class RuleLoader:
    def __init__(self, path: str | Path, *, poll_interval: float = 5.0,
                 clock: Clock | None = None, logger: logging.Logger | None = None) -> None: ...
    @property
    def ruleset(self) -> RuleSet: ...      # 始终可用；初始为 DEFAULT_RULESET
    @property
    def last_error(self) -> str | None: ...
    @property
    def loaded_at(self) -> datetime: ...
    def load_initial(self) -> bool:
        """首次加载。成功返回 True 并把 loaded_at 设为 clock.now()；
        失败返回 False、记 last_error、**不抛**，且 ruleset 保持原值（首次加载时即 DEFAULT_RULESET）。
        **若调用时已持有可用规则集，失败时 MUST NOT 把它降级为 DEFAULT_RULESET。**"""
    def reload(self) -> bool:
        """强制重载。成功替换 ruleset 并返回 True；失败保留旧集合、记 last_error、返回 False、不抛。"""
    def poll_once(self) -> bool:
        """mtime 未变返回 False；变了则等价于 reload()。
        **返回值语义 = 规则集是否真的被替换**：检测到变更但重载失败时返回 False。"""

# classifier/__init__.py
class RuleClassifier:
    def __init__(self, loader: RuleLoader, *, logger: logging.Logger | None = None) -> None:
        """构造时调用 loader.load_initial()。"""
    @property
    def ruleset(self) -> RuleSet:
        """**实时委派给 loader**（不是构造时快照）——热加载后必须立刻看到新规则。"""
    @property
    def last_error(self) -> str | None:
        """同样实时委派给 loader。"""
    def classify(self, *, source: str, level: Level, title: str, body: str | None = None,
                 declared_need_ack: bool = False) -> ClassificationVerdict: ...
    def start(self) -> None: ...   # 启动守护线程，每 poll_interval 秒 poll_once()
    def stop(self, timeout: float = 5.0) -> None: ...
```

**导入路径（冻结，`context.py` 依赖它们，不得改动）**：
| 符号 | 必须可从这里导入 |
|---|---|
| `RuleClassifier` | `from notify_hub.classifier import RuleClassifier` |
| `RuleLoader` | `from notify_hub.classifier.loader import RuleLoader` |
| `RuleEngine` | `from notify_hub.classifier.engine import RuleEngine` |
| `RuleSet`、`Rule`、`RuleDefaults`、`MatchCondition`、`DEFAULT_RULESET`、`parse_ruleset` | `from notify_hub.classifier.rules import ...` |

顶层 `notify_hub/classifier/__init__.py` **至少**必须导出 `RuleClassifier`；是否顺带再导出
`rules.py` 的符号不作要求，但**不得**把 `RuleClassifier` 定义在别处。

**其余语义裁定（补齐规格空白，勿自行发挥）**：
- `case_sensitive` 键**缺失时缺省为 `False`**（大小写不敏感）。`rules.example.yaml` 仍必须显式写出该键。
- 消息 `body is None` 而规则含 `body_contains` 条件 → 该条件**不匹配**，且**不得抛异常**。
- `RuleSet.loaded_at` **MUST** 取注入 clock 的 `now()`（见 4.3 节「ManualClock 是测试中唯一被
  允许的时间来源」）。`RuleLoader` 未注入 clock 时使用 `SystemClock`。
- 规则 `match` 中出现不支持的字段名 → `RuleFileError`（含该字段名与规则 id/下标）。

**YAML 形状**（`rules.example.yaml` 必须与之一致，并被 M9 文档引用）：
```yaml
case_sensitive: false
defaults: { category: uncategorized, labels: [], need_ack: false, channel: webhook }
rules:
  - id: backup-failure
    match:
      source: [db-backup, file-backup]        # equals，或关系
      level: [error]                           # equals
      title_contains: ["失败", "failed"]       # contains
      body_contains: ["exit code"]             # contains
    category: backup-failure
    labels: [infra, backup]
    need_ack: true
    channel: email
```
`match` 的键 → `MatchCondition`：`source`/`level` → `equals`；`title`/`body` → `equals`；
`title_contains`/`body_contains` → `contains`。标量值按单元素列表处理。

**语义（必须逐条实现并被测试固定）**
- 规则按**声明顺序首个命中生效**（first-match-wins）。
- 同一规则内多个条件之间是**与**；一个条件内多个候选值之间是**或**。
- `equals`：字符串全等（`level` 与 `Level.value` 比较）。
- `contains`：子串包含。
- `case_sensitive=false` 时，`equals` 与 `contains` 均在比较前对两侧 `casefold()`；
  `true` 时区分大小写。**不得依赖实现的偶然行为**——必须有显式配置项与对应测试。
- 无规则命中 → 套用 `defaults`，`ClassificationVerdict.rule_id = None`。
- **入待办判定**：`need_ack = declared_need_ack or rule.need_ack`；
  `ack_reason = CALLER_DECLARED if declared_need_ack else (RULE if rule.need_ack else NONE)`。
  即规则**不得**把调用方声明要确认的消息降级为纯通知。

**错误契约**：`parse_ruleset` 抛 `RuleFileError`，消息含文件名 + 规则 id 或下标 + 问题描述。
`load_initial`/`reload`/`poll_once` **MUST NOT 抛异常**给调用方（服务不可因坏配置而不可用）。

#### 3. 内部实现

- 依赖：`pyyaml`、`src/notify_hub/domain.py`、`src/notify_hub/errors.py`、`src/notify_hub/clock.py`。
  **禁止**导入/被 M1 的 `Settings` 依赖——`RuleLoader` 只接收 `path` 与数值参数。
- 热加载：`RuleLoader` 记录 `st_mtime_ns`；`poll_once()` 比较后决定是否 `reload()`。
  `RuleClassifier.start()` 起**守护线程**，循环 `self._stop.wait(self._loader._poll_interval)`，
  异常在循环内被捕获并记录（线程不得因异常退出）。
- 解析失败时的告警文案必须能定位问题：`规则文件解析失败，继续使用上一份可用规则集: <path>: <原因>`。
- `classify` 是**纯函数**（无 IO、无状态突变），便于在请求路径上高频调用。
- `RuleEngine` 可缓存（`RuleClassifier` 在 `ruleset` 变化时重建 `RuleEngine`），但**不得**缓存
  分类结果。

#### 4. 验证方法

测试文件：`tests/test_classifier.py`；命令：`.venv/bin/python -m pytest tests/test_classifier.py`。

必须覆盖的可观察结果：
1. **加载**（tasks 4.1）：把 `rules.example.yaml` 复制到 `tmp_path` 后加载，断言
   `len(ruleset.rules) == 1`、`rules[0].id == "backup-failure"`、
   `rules[0].category == "backup-failure"`、`rules[0].need_ack is True`、
   `rules[0].channel == "email"`、`rules[0].labels == ("infra", "backup")`。
2. **与语义**（tasks 4.2）：规则条件 `source in [web-01, web-02]` 且 `level=error`；
   `source=db-01, level=error` → 不命中该规则（继续尝试后续规则）；断言最终 `rule_id` 为后续
   规则的 id 或 `None`。
3. **或语义**（tasks 4.2）：同一规则下 `source=web-02, level=error` → 命中，`rule_id` 正确。
4. **first-match-wins**（tasks 4.2）：两条规则都能匹配同一消息，断言 `rule_id` 是**先声明**的那条；
   把两条规则在文件里交换顺序后重新加载，断言 `rule_id` 随之改变。
5. **关键词大小写策略**（tasks 4.2）：同一份规则在 `case_sensitive: false` / `true` 两种配置下，
   分别用「失败」与「FAILED」构造消息，断言两种配置下 `rule_id` 的差异符合语义（共 4 个断言）。
6. **兜底**（tasks 4.4）：一条不匹配任何规则的消息 → `rule_id is None` 且
   `category == ruleset.defaults.category`。
7. **入待办判定三分支**（tasks 4.3）：
   - 声明 `need_ack=True` 且命中规则 `need_ack=False` → `verdict.need_ack is True` 且
     `ack_reason is AckReason.CALLER_DECLARED`（冲突以「入待办」为准）；
   - 声明 `need_ack=False` 且命中规则 `need_ack=True` → `need_ack is True` 且
     `ack_reason is AckReason.RULE`；
   - 声明 `need_ack=False` 且规则 `need_ack=False` → `need_ack is False` 且
     `ack_reason is AckReason.NONE`。
8. **热加载**（tasks 4.5）：`RuleLoader(tmp_path/"rules.yaml", clock=ManualClock())` 加载 v1 后
   改写文件为 v2（新增一条规则）、把 mtime 前移或等待其变化，调用 `poll_once()` → 返回 `True`，
   随后 `classify()` 命中新规则的 id。
9. **坏配置降级**（tasks 4.5）：把规则文件写成 `rules: [{id: x}` （非法 YAML）后 `reload()` →
   返回 `False`、不抛异常、`ruleset` 仍是上一份、`last_error` 非空且包含文件路径；
   随后 `classify()` 仍能返回一个合法 `ClassificationVerdict`。
10. **启动时坏配置**：`RuleLoader` 指向一个非法 YAML 文件 → `load_initial()` 返回 `False`，
    `ruleset is DEFAULT_RULESET`（等价比较），`classify()` 返回 `category="uncategorized"`、
    `need_ack` 等于调用方声明值。
11. **线程生命周期**：`RuleClassifier.start()` 后 `stop()` 在 5 秒内返回且线程不存活；
    对同一个 classifier 重复 `stop()` 不抛异常。

**至少 2 个异常场景**：(a) 规则缺少 `category` → `parse_ruleset` 抛 `RuleFileError` 且消息含
规则 id 与 `category`；(b) 规则 `match` 中出现不支持的字段名（如 `foo`）→ `RuleFileError`。

**完成后必须成立**：命令全绿；测试中**不出现真实 `time.sleep` 等待文件变更的轮询**（用
`ManualClock` 或显式修改 mtime），避免 flaky。

---

### 模块 M4：通知投递层

**文件边界**
- 实现路径：`src/notify_hub/notifiers/__init__.py`、`base.py`、`registry.py`、`webhook.py`、`email.py`、`src/notify_hub/delivery.py`
- 测试路径：`tests/test_notifiers.py`、`tests/test_delivery.py`

#### 1. 功能

本设计的核心资产：把「通知到我」抽象成可替换的适配器契约，并负责渠道选择、降级与投递记录。
**不负责**：决定某条消息该不该通知（M7/M6）、待办状态（M6）、提醒节奏（M6）。

#### 2. 接口

`base.py` 的 `ChannelCapabilities` / `DeliveryResult` / `NotificationMessage` / `Notifier`
已在 **4.6 节冻结**，此处不重复。

```python
# registry.py
NOTIFIER_FACTORIES: dict[str, Callable[[ChannelSpec, Sequence[str]], Notifier]]
    # key = ChannelSpec.type；由 webhook.py / email.py 在 notifiers/__init__.py 中注册

class NotifierRegistry:
    def __init__(self) -> None: ...
    def register(self, notifier: Notifier) -> None:
        """按 notifier.channel_id 注册；重复 id 覆盖并记录一条 warning 日志。"""
    def get(self, channel_id: str) -> Notifier | None: ...
    def ids(self) -> tuple[str, ...]: ...          # 注册顺序
    def __contains__(self, channel_id: str) -> bool: ...
    def build_from_specs(self, specs: Sequence[ChannelSpec], *, secrets: Sequence[str] = ()) -> None:
        """逐个 spec：
             enabled=False                -> 跳过，unavailable_reasons[id] = "渠道未启用"
             type 不在 NOTIFIER_FACTORIES -> 跳过，原因 "未知的适配器类型: <type>"
             credentials_complete=False   -> 跳过，原因 "凭据缺失: <逻辑凭据名列表>"
                                             （如 "凭据缺失: url"；**不是**环境变量名——
                                              ChannelSpec.credentials 的键是逻辑名，解析后
                                              环境变量名已不存在，只有 params.url_env 这类
                                              纯文档字段还留着它）
             工厂抛异常                    -> 跳过，原因 "适配器构造失败: <脱敏后异常>"
           其余 -> register(工厂(spec, secrets))"""
    def unavailable_reasons(self) -> Mapping[str, str]: ...
    def available_ids(self) -> tuple[str, ...]: ...

# webhook.py
class WebhookNotifier:
    def __init__(self, channel_id: str, url: str, *, headers: Mapping[str, str] | None = None,
                 field_map: Mapping[str, str] | None = None, timeout: float = 10.0,
                 error_path: str | None = "code", success_codes: Sequence[Any] = (0, 200),
                 transport: httpx.BaseTransport | None = None,
                 secrets: Sequence[str] = ()) -> None: ...
    channel_id: str
    def capabilities(self) -> ChannelCapabilities: ...
    def send(self, msg: NotificationMessage) -> DeliveryResult: ...

# email.py
class EmailNotifier:
    def __init__(self, channel_id: str, *, host: str, port: int = 25, use_tls: bool = False,
                 username: str | None = None, password: str | None = None,
                 sender: str, recipients: Sequence[str], timeout: float = 10.0,
                 smtp_factory: Callable[[], smtplib.SMTP] | None = None,
                 secrets: Sequence[str] = ()) -> None: ...
    channel_id: str
    def capabilities(self) -> ChannelCapabilities: ...
    def send(self, msg: NotificationMessage) -> DeliveryResult: ...

# delivery.py
@dataclass(frozen=True)
class DeliveryOutcome:
    ok: bool
    channel_id: str | None
    is_preferred: bool
    is_fallback: bool
    fallback_reason: str | None
    error_reason: str | None
    receipt: str | None
    attempted_channels: tuple[str, ...]

class DeliveryService:
    def __init__(self, db: Database, registry: NotifierRegistry, *, default_channel: str | None,
                 channel_order: Sequence[str], clock: Clock, logger: logging.Logger,
                 secrets: Sequence[str] = ()) -> None: ...
    def deliver(self, msg: NotificationMessage, *, preferred_channel: str | None = None,
                message_id: int | None = None, todo_id: int | None = None) -> DeliveryOutcome: ...
```

**候选渠道顺序（冻结）**：`[preferred_channel]`（若非空）→ `[default_channel]`（若非空）
→ `channel_order` 中其余渠道。去重保序，只保留 `registry.available_ids()` 中的项。

**`is_preferred` / `is_fallback` 的唯一判定规则（全项目以此为准，M7 的 `DeliveryOut` 同此）**：
```
is_preferred = preferred_channel is not None and used_channel == preferred_channel
is_fallback  = preferred_channel is not None and used_channel != preferred_channel
```
即 **`preferred_channel is None` 时两者恒为 `False`**（没有表达偏好，就谈不上「首选」或「降级」）。

**降级原因文案**：
- 首选未注册/未启用/凭据缺失（**未尝试**，不写投递记录）：
  `fallback_reason = f"首选渠道 {preferred} 不可用: {registry.unavailable_reasons().get(preferred, '未在配置中声明的渠道')}"`
  —— **必须用 `.get(...)`**：首选渠道可能根本没出现在配置里，此时
  `unavailable_reasons()` 没有它的条目，直接下标会抛 `KeyError`。
- 首选已尝试但失败：先为它写一条 `ok=False` 记录，再
  `fallback_reason = f"首选渠道 {preferred} 投递失败: {error_reason}"`

**`unavailable_reasons()` 的覆盖面**：只包含**配置里声明过**的渠道（`enabled=False`、未知 type、
凭据缺失、工厂抛异常四种）。完全未在配置中出现的渠道 id **不在**其中。

**投递记录规则**：每一次**实际调用适配器**都写一条 `DeliveryRecord`（成功或失败）。
`DeliveryRecord.event` 直接取 `msg.kind.value`（`first_notice` / `reminder`），**无其它映射**。
**零候选渠道**时写且只写一条 `channel_id=None`、`ok=False`、
`error_reason="没有可用的通知渠道"` 的记录，并返回 `ok=False` 的 `DeliveryOutcome`。

**`DeliveryOutcome` 各字段在「候选全部失败」时的取值（冻结）**：
- 有候选渠道但全部失败：`ok=False`、`channel_id` = **最后一个尝试过的**渠道 id、
  `error_reason` = 该次失败的原因、`is_preferred`/`is_fallback` 按上面的唯一判定规则计算、
  `attempted_channels` 按尝试顺序列出全部渠道 id。
- 零候选渠道：`ok=False`、`channel_id=None`、`error_reason="没有可用的通知渠道"`、
  `is_preferred=False`、`is_fallback=False`、`fallback_reason=None`（**没有发生降级**，
  只是无渠道可用）、`attempted_channels=()`。
- 有渠道成功：`ok=True`、`channel_id` = 成功的那个、`error_reason=None`。

**`spec → 适配器` 的参数映射（冻结：M1 的 `config.example.yaml` 必须与此一致，M9 文档照此写）**

`webhook` 工厂（`type: "webhook"`）：
| 来源 | 目标参数 | 说明 |
|---|---|---|
| `credentials["url"]` | `url` | **必填**；缺失 → 渠道不可用 |
| `params.headers` | `headers` | mapping，缺省 `{}` |
| `params.field_map` | `field_map` | mapping，缺省 `{}` |
| `params.error_path` | `error_path` | 缺省 `"code"` |
| `params.success_codes` | `success_codes` | list，缺省 `[0, 200]` |
| `params.timeout` | `timeout` | float，缺省 `10.0` |
| `params.url_env` | —— | **工厂不读**，纯文档字段（作者可读地指明环境变量名） |

`email` 工厂（`type: "email"`）：
| 来源 | 目标参数 | 说明 |
|---|---|---|
| `params.host` | `host` | **必填** |
| `params.port` | `port` | 缺省 `25` |
| `params.use_tls` | `use_tls` | 缺省 `False` |
| `params.sender` | `sender` | **必填** |
| `params.recipients` | `recipients` | **必填非空 list** |
| `credentials["username"]` | `username` | 可选；未声明即 `None` |
| `credentials["password"]` | `password` | 可选；未声明即 `None` |
| `params.timeout` | `timeout` | float，缺省 `10.0` |

必填项缺失 → 工厂抛异常 → 该渠道按「适配器构造失败」记入 `unavailable_reasons`（**不**让启动失败）。

**Webhook 载荷（冻结默认键名）**：`title`、`body`、`level`、`source`、`occurred_at`（ISO 8601）、
`category`、`todo_id`、`overdue_seconds`、`kind`。`field_map` 的键是**默认键名**、值是**目标键名**
（例如 `{"body": "text"}` 表示用 `text` 承载正文），未列出的键沿用默认键名。
请求头固定带 `Content-Type: application/json`，再并入配置的自定义 `headers`。
判定失败：HTTP 非 2xx → 失败；HTTP 2xx 且响应体是 JSON 且 `error_path` 可取到值时，该值不在
`success_codes` 中 → 失败，`error_reason` 取响应体中 `msg`/`message`/`error` 的首个非空字符串
（无则用 `"平台返回业务错误: <code>"`）。响应体非 JSON → 只看 HTTP 状态。

**Webhook 成功时的 `receipt`（冻结）**：取响应体 JSON 中 `msg`/`message`/`id` 的**首个非空值**
并转为字符串；都取不到（含响应体非 JSON）时用 `f"HTTP {status_code}"`。`receipt` 永不为空。

**Email 主题与正文（冻结）**：主题 `f"[{msg.level.value.upper()}] {msg.title}"`；
正文纯文本，依次含 `来源: {source}`、`时间: {occurred_at ISO}`、可选 `已超时: {format 时长}`、
空行、`msg.body`。

**不变量**
- `Notifier.send()` 与 `DeliveryService.deliver()` **MUST NOT 抛出异常**：网络错误、超时、
  认证失败、DNS 失败、JSON 解析失败全部转成失败结果。
- 写入 `DeliveryRecord` 的 `error_reason` / `fallback_reason` **MUST** 已经过 `redact_*`。
- 首次通知失败**不重试、不排队**（`design.md` 决策 2）；本模块不含任何重试逻辑。

#### 3. 内部实现

- `WebhookNotifier` 每次 `send()` 用 `httpx.Client(transport=transport, timeout=timeout)`；
  `transport` 是唯一的测试接缝（`httpx.MockTransport`）。
- `EmailNotifier` 用 `smtplib.SMTP`；`use_tls=True` 时 `starttls()`；`smtp_factory` 是唯一的
  测试接缝（错误注入用）；正常路径用真实的本机 `aiosmtpd`。
- 捕获 `Exception` 的边界：`send()` 顶层 `except Exception as exc:` →
  `DeliveryResult.failure(redact_exception(exc, self._secrets))`。**必须**记录一行日志。
- `DeliveryService` 通过 `db.session()` 写记录；每条记录一个独立 session，避免长事务。
- 依赖：`httpx`、标准库 `smtplib`/`email.message`、M1（`ChannelSpec`、`redact_*`）、
  M2（`Database`、`DeliveryRecord`）、阶段 0（`domain`、`clock`、`errors`）。
  **禁止**导入 M3/M5/M6/M7/M8。

#### 4. 验证方法

测试文件：`tests/test_notifiers.py`、`tests/test_delivery.py`；
命令：`.venv/bin/python -m pytest tests/test_notifiers.py tests/test_delivery.py`。

必须覆盖的可观察结果：
1. **契约自检**（tasks 5.1）：`isinstance(WebhookNotifier(...), Notifier)` 对 `runtime_checkable`
   的 Protocol 成立；`NotifierRegistry.register` 后 `ids()` 与 `get()` 一致；重复注册同一
   `channel_id` 时后注册的生效且 `caplog` 出现 warning。
2. **注册表按 spec 构造**（tasks 5.2）：3 个 spec（`enabled=False`、未知 type、
   凭据缺失）→ `available_ids() == ()`，`unavailable_reasons()` 含 3 条且文案分别匹配
   「未启用」「未知的适配器类型」「凭据缺失」。
3. **Webhook 三种响应**（tasks 5.3，用 `httpx.MockTransport`）：
   - 200 + `{"code": 0}` → `ok is True`，`receipt` 非空；
   - 200 + `{"code": 40001, "msg": "invalid sign"}` → `ok is False`，
     `error_reason` 含 `invalid sign`；
   - 500 + 任意体 → `ok is False`，`error_reason` 含状态码 `500`。
4. **字段映射**（tasks 5.3）：`field_map={"body": "text", "title": "head"}` 时，捕获实际请求体，
   断言键为 `head`/`text`，且其余默认键仍存在；`overdue_seconds` 在首次通知时为 `null`。
5. **自定义请求头**：`headers={"X-Sign": "abc"}` → 捕获的请求头含 `X-Sign: abc`。
6. **网络异常不外抛**：`MockTransport` 抛 `httpx.ConnectError` → `send()` 返回失败结果而非抛异常。
7. **邮件成功**（tasks 5.4）：启动本机 `aiosmtpd`，用真实 `EmailNotifier` 发送 → 收到的邮件
   主题等于 `[ERROR] <标题>`、`To` 包含全部收件人、正文含来源与 ISO 时间。
   **注意（已实测）**：`Controller(port=0)` **不会**绑定临时端口，连接必被拒绝；
   测试必须先自行取一个空闲端口再传给 `Controller`：
   ```python
   def free_port() -> int:
       with socket.socket() as s:
           s.bind(("127.0.0.1", 0)); return s.getsockname()[1]
   ```
   `Controller(hostname="127.0.0.1", port=free_port())` + `ctl.start()` / `ctl.stop()` 于 `finally`。
8. **邮件认证失败**（tasks 5.4）：注入一个 `smtp_factory` 返回的假 SMTP 对象，其 `login()` 抛
   `smtplib.SMTPAuthenticationError` → `ok is False`，且 `error_reason` **不含**密码明文
   （断言 `password_value not in error_reason`）。
9. **DeliveryService 成功与记录**（tasks 5.5）：注册一个 `RecordingNotifier`（`channel_id="rec"`），
   以 **`deliver(msg, preferred_channel="rec", message_id=<已存在的消息 id>)`** 调用 →
   `outcome.ok is True`、`channel_id == "rec"`、`is_preferred is True`、`is_fallback is False`，
   且数据库中该 `message_id` 下恰有 1 条 `ok=True`、`is_preferred=True`、`is_fallback=False` 的记录。
   **必须显式传 `preferred_channel`**：按上面的唯一判定规则，不传时 `is_preferred` 恒为 `False`。
10. **降级**（tasks 5.2）：首选渠道凭据缺失（`preferred_channel="missing"`，默认渠道可用）→
    `outcome.ok is True`、`channel_id == 默认渠道`、`is_fallback is True`、
    `fallback_reason` 含 `missing`；且**不为首选渠道**写投递记录。
11. **首选已尝试但失败后降级**：首选可用但返回失败、默认渠道成功 → 数据库中有 2 条记录
    （第 1 条 `ok=False`、`is_preferred=True`；第 2 条 `ok=True`、`is_fallback=True`）。
12. **全部渠道不可用**（tasks 5.6）：注册表为空、`preferred_channel=None`、`default_channel=None`
    → `outcome.ok is False`，且存在**恰好 1 条** `channel_id is None`、`ok=False` 的记录。
13. **脱敏（安全关键，tasks 9.6）—— 两个都必须覆盖**：
    a. HTTP 非 2xx：webhook URL 为 `https://h/p?access_token=SECRETTOKEN`，`transport` 返回 500
       → 数据库中该记录的 `error_reason` 不含 `SECRETTOKEN`；`caplog` 中同样不含。
    b. **平台回显裸 token（已确认 P1 的真实形态，必须固定）**：`transport` 返回
       **HTTP 200 + `{"code": 40001, "msg": "invalid access_token SECRETTOKEN"}`**
       （即业务失败并在 msg 里回显**裸 token**），且 `secrets` **必须按生产装配的形态构造**——
       即经 `credential_values(settings)` 得到（settings 由 `load_settings` +
       `env={"NOTIFY_WEBHOOK_URL": "https://h/p?access_token=SECRETTOKEN"}` 构造），
       **不得**手工写 `secrets=("SECRETTOKEN",)`。
       → 断言 `error_reason` 不含 `SECRETTOKEN`、`caplog.text` 不含 `SECRETTOKEN`。
       **这一条是本轮审查发现的 P1 的回归闸门**：原实现下 `secrets` 只含完整 URL，
       `redact_text` 的子串匹配对裸 token 失效，token 会明文入库并经查询接口外泄。
14. **首次通知不重试**：渠道失败后，断言该 `message_id` 的投递记录数在再次调用 `deliver()` 前
    不增加；`DeliveryService` 无任何延时/重试循环（断言 `deliver()` 的调用次数 == 候选数）。

**至少 2 个异常场景**：(a) `httpx.ConnectError`（上面的第 6 条）；(b)
`SMTPAuthenticationError` 且密码不泄漏（第 8 条）。

**完成后必须成立**：命令全绿；测试**不访问真实外网**（`MockTransport` + 本机 `aiosmtpd`），
且 `grep -rn "http://" tests/test_notifiers.py` 中的地址只用于 `MockTransport`。

---

### 模块 M5：命令行客户端

**文件边界**
- 实现路径：`src/notify_hub/cli.py`
- 测试路径：`tests/test_cli.py`

#### 1. 功能

给 shell 脚本 / cron / 运维终端提供一行命令的投递与待办处理入口。**必须走 HTTP 接口**，
不得绕过接入层校验，也不得导入 M1–M4/M6–M8 的任何模块（它是独立客户端）。

#### 2. 接口

```python
# cli.py
def build_client(endpoint: str) -> httpx.Client:
    """唯一的测试接缝：测试 monkeypatch 本函数返回挂 MockTransport 的 Client。"""

app: typer.Typer        # 根命令

def main() -> None:     # console script 入口，见 pyproject [project.scripts]
```

**命令面（冻结）**
```
notify [--source S] [--title T] [--body B | --body-stdin] [--level info|warning|error]
       [--need-ack] [--dedup-key K] [--endpoint URL]
notify todo list [--all] [--endpoint URL]
notify todo done TODO_ID [--endpoint URL]
```
- `--endpoint` 解析顺序：命令行参数 > 环境变量 `NOTIFY_HUB_ENDPOINT` > `http://127.0.0.1:8000`。
- `--body-stdin` 与 `--body` 同时给出 → 退出码 `2`（usage error，由 typer/click 处理）。

**退出码（冻结，避免与 typer 的用法错误码 2 冲突）**
| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 服务端拒绝（HTTP 4xx/5xx，含 404 未找到） |
| 2 | 用法错误（缺必填选项等，typer 默认） |
| 3 | 无法连接服务端（`httpx.ConnectError`/`TimeoutException`） |

**输出契约**
- `notify ...` 成功：stdout 打印 `message_id`（纯整数或 `message_id=<n>`，见下），退出码 0。
  冻结为 **stdout 打印 `message_id=<n>` 一行**（tasks 8.1 断言 stdout 含 `message_id`）。
- `notify todo list`：每行 `<todo_id>\t<超时时长>\t<来源>\t<标题>`，按超时时长降序；
  `--all` 时包含已完成并在行首加状态列。
- `notify todo done <id>` 成功：stdout 打印 `done <id>`。
- 所有错误写 stderr，形如 `错误: <可读原因>`（服务端拒绝时附服务端返回的字段级原因）。

#### 3. 内部实现

- 依赖：`typer`、`httpx`、标准库。**禁止**导入本项目其它模块（`notify_hub.domain` 等一律不用，
  级别取值用字面量校验交给服务端——但可用 `Enum` 在点击层限制取值）。
- `main()` 捕获 `typer.Exit` 与自身定义的 `CliError(code, message)`；写 stderr 后 `sys.exit(code)`。
- 响应解析：成功投递读 `resp.json()["message_id"]`；`todo list` 读
  `resp.json()["todos"]`（字段见 M7 的 `TodoOut`）。字段缺失时按「服务端返回格式无法识别」
  归为退出码 1，不抛 `KeyError` 崩溃。
- 超时时长列用 `todo["overdue_seconds"]` 自行格式化为 `Xd Yh Zm`（不依赖 M6 的格式化函数，
  保持客户端独立）。

#### 4. 验证方法

测试文件：`tests/test_cli.py`；命令：`.venv/bin/python -m pytest tests/test_cli.py`。

用 `typer.testing.CliRunner` + monkeypatch `notify_hub.cli.build_client`（返回挂
`httpx.MockTransport` 的 `Client`）。

必须覆盖的可观察结果：
1. **成功投递**（tasks 8.1）：stub 返回 `202 {"message_id": 42}` →
   `result.exit_code == 0`，`"message_id=42" in result.stdout`；并断言 stub 收到的请求
   `method == "POST"`、路径为 `/api/v1/messages`、JSON 体含 `source`/`title`/`level`/`need_ack`。
2. **stdin 读正文**（tasks 8.1）：`--body-stdin` + 输入 `多行\n正文` → 请求体的 `body`
   等于 `"多行\n正文"`。
3. **服务端拒绝**（tasks 8.2）：stub 返回 `422 {"detail": [{"loc": ["body", "source"], ...}]}`
   → `exit_code == 1`，stderr 含 `source`。
4. **服务不可达**（tasks 8.2）：`build_client` 返回的 Client 其 transport 抛
   `httpx.ConnectError` → `exit_code == 3`，stderr 含「无法连接」。
5. **用法错误**：`notify --title x`（缺 `--source`）→ `exit_code == 2`。
6. **todo list**（tasks 8.3）：stub 返回 3 条 `todos`（`overdue_seconds` 分别 3600 / 18000 / 1200，
   **服务端已排序**）→ `exit_code == 0`，stdout 的 3 行顺序为 18000 / 3600 / 1200 对应的 id。
7. **todo done**（tasks 8.3）：stub 返回 `200 {"status": "completed"}` → `exit_code == 0`，
   stdout 含 `done`。
8. **todo done 不存在**（tasks 8.3）：stub 返回 `404` → `exit_code == 1`，stderr 含可读原因。
9. **endpoint 优先级**：不传 `--endpoint`、环境变量设为 `http://x:9` → 断言请求发往 `http://x:9`
   （通过 stub 捕获的 URL）；传 `--endpoint http://y:8` 时覆盖环境变量。
10. **响应格式异常不崩溃**：stub 返回 `202 {}`（缺 `message_id`）→ `exit_code == 1`，
    stderr 可读，且**不出现 traceback**（断言 `"Traceback" not in result.output`）。

**至少 2 个异常场景**：(a) `httpx.ConnectError`（第 4 条）；(b) `202 {}` 缺字段（第 10 条）。

**完成后必须成立**：命令全绿；测试不启动真实服务器、不访问网络。

---

### 模块 M6：消息/待办领域服务 + 提醒调度

**文件边界**
- 实现路径：`src/notify_hub/services/__init__.py`、`messages.py`、`todos.py`、`notifications.py`、`scheduler.py`
- 测试路径：`tests/test_services.py`、`tests/test_todos.py`

#### 1. 功能

消息与待办的持久化业务逻辑，以及「超时提醒」的周期扫描。**不负责**：HTTP/页面入口（M7/M8）、
分类规则（M3）、渠道选择与投递（M4）、受理编排（M7 的 `IngestPipeline`）。

#### 2. 接口

```python
# services/messages.py
@dataclass(frozen=True)
class MessageDraft:
    source: str
    title: str
    body: str | None = None
    level: Level = Level.INFO
    need_ack: bool = False
    dedup_key: str | None = None
    occurred_at: datetime | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)

class MessageService:
    def __init__(self, db: Database, clock: Clock) -> None: ...
    def create(self, draft: MessageDraft, verdict: ClassificationVerdict) -> Message:
        """写入 messages 行：received_at=clock.now()；occurred_at=draft.occurred_at or received_at；
        分类结论来自 verdict（rule_id/category/labels/needs_ack/ack_reason/preferred_channel）；
        need_ack_declared=draft.need_ack。返回带 id 的行。"""
    def get(self, message_id: int) -> Message | None: ...
    def list(self, *, limit: int = 50, offset: int = 0, source: str | None = None) -> list[Message]:
        """按 received_at 降序。"""
    def count(self, *, source: str | None = None) -> int: ...
    def deliveries(self, message_id: int) -> list[DeliveryRecord]:   # 按 attempted_at 升序
        ...
    def todo_for(self, message_id: int) -> Todo | None: ...

# services/todos.py
@dataclass(frozen=True)
class TodoView:
    id: int
    source: str
    category: str | None
    title: str
    status: TodoStatus
    ack_reason: AckReason
    preferred_channel: str | None
    created_at: datetime
    first_notified_at: datetime
    last_notified_at: datetime
    reminder_count: int
    completed_at: datetime | None
    overdue_seconds: float          # 未完成: now - first_notified_at；已完成: completed_at - first_notified_at

@dataclass(frozen=True)
class TodoDetail:
    todo: TodoView
    message: Message
    deliveries: tuple[DeliveryRecord, ...]   # 该 todo 的投递记录，按时间升序
    events: tuple[TodoEvent, ...]            # 按 occurred_at 升序

@dataclass(frozen=True)
class CompleteOutcome:
    status: str            # "completed" | "already_completed" | "not_found"
    todo_id: int
    completed_at: datetime | None

class TodoService:
    def __init__(self, db: Database, clock: Clock, *, logger: logging.Logger | None = None) -> None: ...
    def ensure_for_message(self, message: Message) -> Todo | None:
        """message.needs_ack 为假 -> None。
        否则：若 message.dedup_key 非空且存在同 (source, dedup_key) 的 pending 待办 -> 返回该条，
        不新建、不写事件；否则新建 pending 待办，first_notified_at=last_notified_at=clock.now()，
        reminder_count=0，preferred_channel=message.preferred_channel，
        并写一条 TodoEvent(kind=CREATED)。并发下 IntegrityError 视为已存在，回查返回。"""
    def get(self, todo_id: int) -> Todo | None: ...
    def complete(self, todo_id: int) -> CompleteOutcome: ...
    def list(self, *, status: TodoStatus | None = TodoStatus.PENDING,
             limit: int = 100, offset: int = 0) -> list[TodoView]:
        """默认仅待完成；按 overdue_seconds 降序（即 first_notified_at 升序）。"""
    def detail(self, todo_id: int) -> TodoDetail | None: ...
    def due_for_reminder(self, *, settings: ReminderSettings) -> list[Todo]:
        """reminder_count == 0 时门槛为 first_reminder_after_seconds（自 first_notified_at 起），
        否则为 reminder_interval_seconds（自 last_notified_at 起）；仅 pending。"""
    def record_reminder(self, todo_id: int, *, delivered: bool, channel_id: str | None) -> None:
        """last_notified_at = clock.now()（**无论成败**）、reminder_count += 1、
        写 TodoEvent(kind=REMINDER, channel_id=..., delivery_ok=delivered)。"""
    def message_for(self, message_id: int) -> Message | None:
        """**架构师批准的附加方法**（原规格未列，实现时补入）。
        用途：`ReminderScheduler` 的构造签名按规格不含 `MessageService`，但它必须取到
        原始消息才能构造提醒文案，因此经 `TodoService` 取。
        属**纯只读**辅助，不得在其中做任何写入或状态变更。"""

# services/notifications.py
def format_duration(seconds: float) -> str:
    """人类可读时长，如 '2 小时 5 分钟'、'45 秒'、'1 天 3 小时'。用于提醒文案。"""

def notification_for_message(message: Message, *, kind: DeliveryEvent, now: datetime,
                             todo: Todo | None = None) -> NotificationMessage: ...

# services/scheduler.py
class ReminderScheduler:
    def __init__(self, *, todos: TodoService, delivery: DeliveryService, clock: Clock,
                 settings: ReminderSettings, logger: logging.Logger) -> None: ...
    @property
    def running(self) -> bool: ...
    def start(self) -> None: ...            # 守护线程；每 scan_interval_seconds 调一次 run_once()
    def stop(self, timeout: float = 5.0) -> None: ...
    def run_once(self) -> int:              # 执行一轮；返回实际发出提醒的待办条数
        ...
```

**首次通知文案（冻结）**：`notification_for_message(..., kind=FIRST_NOTICE, todo=None)` 返回的
`NotificationMessage`：
- `overdue_seconds = None`；`todo_id = None`
- `title = message.title`（**不加前缀**——前缀是「超时」语义，只属于提醒）
- `body` 依次含 `来源: {source}`、`分类: {category}`、`时间: {occurred_at ISO}`、空行、原始正文
- `level`、`source`、`occurred_at`、`kind`、`category` 取自消息行

**提醒文案（冻结）**：`notification_for_message(..., kind=REMINDER, todo=t)` 返回的
`NotificationMessage`：
- `overdue_seconds = (now - as_utc(todo.first_notified_at)).total_seconds()`
- `title = f"[待办超时 {format_duration(overdue_seconds)}] {todo.title}"`（**含标题与超时时长**）
- `body` 依次含 `来源: {source}`、`分类: {category}`、`已超时: {format_duration(...)}`、
  `待办 id: {todo.id}`、空行、原始消息正文
- `meta` 含 `{"todo_id": t.id, "message_id": t.message_id, "reminder_count": t.reminder_count}`

**`run_once()` 行为（冻结）**：对每个 `due_for_reminder` 的待办：
1. `todo = todos.get(id)` 重新读取（防并发完成）
2. 若 `todo.status != PENDING` 则跳过
3. `msg = notification_for_message(message, kind=REMINDER, now=clock.now(), todo=todo)`
4. `outcome = delivery.deliver(msg, preferred_channel=todo.preferred_channel,
   message_id=todo.message_id, todo_id=todo.id)`
5. `todos.record_reminder(todo.id, delivered=outcome.ok, channel_id=outcome.channel_id)`
6. 该条计一次「已提醒」

单条待办的异常**不得**中断整轮：捕获、记日志、继续下一条。

**不变量**
- 一条待办在**同一轮**内至多被提醒一次（tasks 6.5「不出现同一周期内重复发送」）。
- `last_notified_at` 每次提醒后更新，**与投递成败无关**；因此两次提醒之间至少间隔
  `reminder_interval_seconds`（spec「提醒节奏保持稳定」）。
- 已完成的待办**永不出现在** `due_for_reminder` 结果中，且 `complete()` 后
  `status` 不可再回到 `pending`。
- `complete()` 幂等：对已完成待办再次调用返回 `already_completed`，`completed_at` **不变**。

**M6 语义裁定（补齐规格空白，勿自行发挥）**
- `complete()` 返回 `not_found` 时，`CompleteOutcome.todo_id` 回显传入的 id、
  `completed_at = None`、`todo = None`。
- `TodoService.list(status=None)` 表示**不过滤状态**（返回全部）；缺省参数才是
  `TodoStatus.PENDING`（只返回待完成）。
- `run_once()` 的返回值 = **本轮为该待办发起了一次提醒并完成记账的条数**
  （即调用了 `record_reminder` 的条数；**投递成功或失败都计入**，因为失败也要按间隔重试）。
  只有两种不计入：投递调用**抛异常被隔离**的那条，以及状态在检查间隙已变为已完成而被跳过的那条。
- `ReminderScheduler.running`：`start()` 前为 `False`，`start()` 后为 `True`，`stop()` 后为
  `False`；`stop()` 可重复调用且不抛异常。该线程生命周期**不在 M6 模块级测试内断言**
  （需要真实时序等待，与本模块「禁止真实等待」冲突），改由**阶段 D 集成测试**在真实线程下覆盖。

#### 3. 内部实现

- 归属说明：`TodoService.ensure_for_message` 需要 `message.needs_ack`/`dedup_key`/`source`，
  这些字段由 M7 在 `MessageService.create(draft, verdict)` 时写入。
- **`first_notified_at` 的语义（本文档的解释，需登记为偏差 D-2）**：待办在**受理时刻**创建，
  `first_notified_at = 受理时刻`，代表「随受理发出的首次通知」；背景派发是该次通知。
  这样即使进程在派发前崩溃，超时提醒仍会按时触发（安全侧）。
- 所有从 ORM 读出的时间在参与算术前经 `clock.as_utc()`。
- `TodoView.overdue_seconds` 用 `max(0.0, ...)` 兜底，避免时钟回拨产生负数。
- 依赖：M2、M4（`DeliveryService`/`NotificationMessage`）、M1（`ReminderSettings`）、阶段 0。
  `TodoService` **不得**依赖 `DeliveryService`（只有 `ReminderScheduler` 依赖），避免环。
- **禁止**在 services 中写 HTTP 语义（状态码、JSON 模型）。

#### 4. 验证方法

测试文件：`tests/test_services.py`、`tests/test_todos.py`；
命令：`.venv/bin/python -m pytest tests/test_services.py tests/test_todos.py`。
**必须用 `ManualClock`，禁止真实等待。**

必须覆盖的可观察结果：
1. **消息落库**：`MessageService.create` 后 `get(id)` 返回的行，其 `category`/`labels`/`rule_id`/
   `needs_ack`/`ack_reason` 与传入 verdict 逐字段相等；`received_at == clock.now()`；
   `occurred_at` 在 draft 未给时等于 `received_at`、给了则等于给定值。
2. **待办生成**（tasks 6.1）：`needs_ack=True` 的消息 → `ensure_for_message` 返回待办，
   `status == PENDING`、`first_notified_at == last_notified_at == clock.now()`、
   `reminder_count == 0`；且 `todos` 表中该消息只有 1 条待办，`todo_events` 有 1 条 `CREATED`。
3. **不需要待办**：`needs_ack=False` → 返回 `None`，`todos` 表无新增。
4. **去重**（tasks 6.2）：同一 `(source, dedup_key)` 的两条消息分别 `ensure_for_message` →
   第二次返回的 `todo.id` 与第一次相同，`todos` 表总数为 1，`todo_events` 仍只有 1 条 `CREATED`。
5. **dedup_key 为空不去重**：两条 `dedup_key=None` 的消息 → 产生 2 条待办。
6. **完成后可再建**：把第 4 条的待办 `complete()` 后，再 `ensure_for_message` 一条同
   `(source, dedup_key)` 的消息 → 产生**新的**待办，总数为 2。
7. **完成幂等**（tasks 6.3）：`complete(id)` 首次 → `status == "completed"`、
   `completed_at == clock.now()`；`clock.advance(600)` 后再次 `complete(id)` →
   `status == "already_completed"` 且 `completed_at` **仍等于首次的值**；`complete(99999)` →
   `status == "not_found"`。三条各写一个断言。
8. **列表排序与过滤**（tasks 7.1 的 service 一半）：3 条待办，`first_notified_at` 分别为
   `now-1h`、`now-5h`、`now-20m` → `list()` 的 id 顺序为 5h、1h、20m；
   `list(status=DONE)` 不包含任何 pending；`overdue_seconds` 依次约等于 18000/3600/1200
   （`abs(x - 期望) < 1`）。
9. **详情**（tasks 7.3 的 service 一半）：构造「创建 → 2 次提醒 → 完成」→ `detail()` 的
   `events` 长度为 4、`kind` 顺序为 `created, reminder, reminder, completed`、
   `occurred_at` 非降序；`deliveries` 含 2 条记录。
10. **超时判定**（tasks 6.4）：`ReminderSettings(scan_interval_seconds=1,
    first_reminder_after_seconds=60, reminder_interval_seconds=120)`；待办创建后
    `clock.advance(59)` → `due_for_reminder()` 为空；`advance(2)`（累计 61）→ 含该待办。
11. **提醒与节奏**（tasks 6.4/6.5）：`run_once()` 返回 1，数据库新增 1 条 `REMINDER` 事件、
    `last_notified_at == clock.now()`、`reminder_count == 1`（提醒记录数为 1）；
    `clock.advance(119)` → `run_once()` 返回 0（**未到间隔不打扰**）；
    `clock.advance(2)` → `run_once()` 返回 1。
12. **完成后不再提醒**（tasks 6.5）：待办在门槛到达前 `complete()`，再 `advance(10_000)` 并
    `run_once()` → 返回 0，`todo_events` 中没有新的 `REMINDER`。
13. **提醒失败仍重试**（tasks 6.5）：用一个 `send()` 恒失败的 notifier，`run_once()` 后
    投递记录 `ok=False` 且 `last_notified_at` **已更新**；`clock.advance(interval)` 后
    `run_once()` 再次尝试 → 该待办的提醒事件数为 2，待办仍为 `pending`。
14. **提醒文案**（tasks 6.4）：断言 reminder 的 `NotificationMessage.title` 同时含待办标题与
    形如 `分钟`/`小时` 的时长字样；`overdue_seconds` 等于 `now - first_notified_at` 的秒数。
15. **`format_duration` 边界**：0 → 含 `0 秒`；59 → `59 秒`；60 → `1 分钟`；3661 → 含
    `1 小时` 与 `1 分钟`；90000 → 含 `1 天`。
16. **一轮一次**：把 `scan_interval_seconds` 设为 1、`advance(3600)` 后只调用一次 `run_once()`
    → 断言该待办本轮只新增 1 条 `REMINDER` 事件（不因积压时间而成批补发）。
17. **单条异常不中断整轮**：构造 2 条到期待办，**在 `delivery.deliver` 边界注入一次异常**
    （例如替身/monkeypatch，使第一条待办的 `deliver` 调用抛异常）→ `run_once()` **不抛异常**，
    第二条仍被提醒，且返回值只计**实际成功发出提醒的条数**。
    **注意**：不得通过「让 `Notifier.send()` 抛异常」来构造本用例——4.6 节明确规定
    `Notifier.send()` MUST NOT 抛异常，那样会用一个违反契约的替身去测上层。要测的是
    「投递层即便因 bug 抛出异常，调度器也不能被带崩」，所以在 `deliver` 边界注入才正确。

**至少 2 个异常场景**：(a) `complete()` 不存在的 id（第 7 条）；(b) 第 17 条的投递异常隔离。

**完成后必须成立**：命令全绿；`grep -n "time.sleep" tests/test_todos.py tests/test_services.py`
无输出。

---

### 模块 M7：HTTP 接入层与受理编排

**文件边界**
- 实现路径：`src/notify_hub/api/__init__.py`、`schemas.py`、`messages.py`、`todos.py`、`health.py`、`src/notify_hub/pipeline.py`
- 测试路径：`tests/test_ingest.py`、`tests/test_api_todos.py`

#### 1. 功能

对外 HTTP 契约与「受理 → 分类 → 落库 → 生成待办 → 后台投递」的编排。**不负责**：业务规则
（M3/M6）、渠道选择（M4）、页面渲染（M8）。**不做**认证（`design.md` 决策 8 的部署边界）。

#### 2. 接口

```python
# schemas.py —— 请求
class MessageIn(BaseModel):
    source: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body: str | None = None
    level: Level = Level.INFO
    need_ack: bool = False
    dedup_key: str | None = None
    occurred_at: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

# schemas.py —— 响应（字段名冻结，M5/M8/integration 依赖）
class MessageAccepted(BaseModel):
    message_id: int
    todo_id: int | None = None

class BatchItemResult(BaseModel):
    index: int
    accepted: bool
    message_id: int | None = None
    todo_id: int | None = None
    error: str | None = None
    error_fields: list[str] = Field(default_factory=list)

class BatchAccepted(BaseModel):
    results: list[BatchItemResult]
    accepted_count: int
    rejected_count: int

class MessageOut(BaseModel):
    id: int; source: str; title: str; body: str | None; level: Level
    need_ack: bool; dedup_key: str | None; occurred_at: datetime; received_at: datetime
    meta: dict[str, Any]; rule_id: str | None; category: str | None; labels: list[str]
    needs_ack: bool | None; ack_reason: str | None; preferred_channel: str | None
    todo_id: int | None
    deliveries: list[DeliveryOut]

class DeliveryOut(BaseModel):
    channel_id: str | None; attempted_at: datetime; ok: bool; error_reason: str | None
    receipt: str | None; is_preferred: bool; is_fallback: bool
    fallback_reason: str | None; event: str

class TodoOut(BaseModel):
    id: int; source: str; category: str | None; title: str; status: TodoStatus
    ack_reason: str; preferred_channel: str | None
    created_at: datetime; first_notified_at: datetime; last_notified_at: datetime
    reminder_count: int; completed_at: datetime | None; overdue_seconds: float

class TodoListOut(BaseModel):
    todos: list[TodoOut]
    total: int

class TodoDoneOut(BaseModel):
    todo_id: int; status: str; completed_at: datetime | None

class HealthOut(BaseModel):
    status: str; time: datetime; version: str

# api/__init__.py
def create_api_router(ctx: AppContext) -> APIRouter:
    """返回**包含 `/healthz` 在内的全部 API 端点**的路由；`ctx` 闭包进各路由。"""
def create_api_app(ctx: AppContext) -> FastAPI:
    """= FastAPI() + app.state.ctx = ctx + include_router(create_api_router(ctx))。
    供模块级测试使用；不启动 classifier/pipeline/scheduler 的后台线程。"""

# pipeline.py
@dataclass(frozen=True)
class AcceptOutcome:
    message_id: int
    todo_id: int | None
    verdict: ClassificationVerdict

class IngestPipeline:
    def __init__(self, *, classifier: RuleClassifier, messages: MessageService,
                 todos: TodoService, delivery: DeliveryService, clock: Clock,
                 logger: logging.Logger, run_inline: bool = False) -> None: ...
    def accept(self, draft: MessageDraft) -> AcceptOutcome:
        """1) verdict = classifier.classify(...)  2) msg = messages.create(draft, verdict)
           3) todo = todos.ensure_for_message(msg)  4) 若非 run_inline: 入队；否则直接 dispatch
           单条消息的任何异常都不得让 accept 抛给路由层以外——路由层负责转 5xx。"""
    def dispatch(self, message_id: int) -> DeliveryOutcome | None:
        """读消息 -> notification_for_message(kind=FIRST_NOTICE) ->
           delivery.deliver(..., preferred_channel=message.preferred_channel,
                            message_id=..., todo_id=...)。消息不存在则记日志返回 None。"""
    def start(self) -> None: ...    # 启动单个守护工作线程消费队列
    def stop(self, timeout: float = 5.0) -> None: ...
    def drain(self, timeout: float = 5.0) -> bool:
        """等待队列排空且当前任务完成；超时返回 False。测试专用。
        `run_inline=True` 时队列从不被使用 -> 立即返回 True。
        `run_inline=False` 且**未调用过 `start()`** 时无人消费 -> 必然超时返回 False。"""
    @property
    def pending(self) -> int: ...
```

**M7 语义裁定（补齐规格空白，勿自行发挥）**：
- **测试必须自行管理后台线程**：`create_api_app(ctx)` 与 `build_test_context()` 都**不**启动
  `classifier`/`pipeline`/`scheduler` 的线程（那是 `create_app()` 的 lifespan 职责）。因此凡是用
  `run_inline=False` 的用例，**必须自己调用 `pipeline.start()`**，否则 `drain()` 永远不会返回 True。
  用 `run_inline=True`（即 `ctx` fixture）时 `drain()` 立即返回 True，无需 `start()`。
- **`TodoListOut.total` = 匹配 `?status=` 过滤条件的总条数**，**不受 `limit`/`offset` 影响**
  （即分页前的命中数），不是库内总数。
- **`?status=all` 的排序**：与其它取值一致，按 `overdue_seconds` **降序**混排。已完成待办的
  `overdue_seconds` 按 M6 的定义取 `completed_at - first_notified_at`。
- **`DeliveryOut.is_preferred`**：仅当**请求过** `preferred_channel` 且它与实际使用的渠道相同时
  才为 `true`；`preferred_channel` 为 `None`（如 conftest 的最小规则集）时恒为 `false`。
  同理 `is_fallback` 在 `preferred_channel` 为 `None` 时恒为 `false`。
- **`BatchItemResult.error`**：非空的人类可读字符串，且**必须点出第一个出错字段名**
  （`error_fields` 给出全部字段名）。具体句式不作要求，测试只断言非空 + 字段名出现在
  `error_fields` 中。

**HTTP 端点（冻结）**
| 方法 | 路径 | 成功 | 说明 |
|---|---|---|---|
| POST | `/api/v1/messages` | 202 `MessageAccepted` | 校验失败 422，**不写库** |
| POST | `/api/v1/messages/batch` | 207 `BatchAccepted` | 请求体是**裸 JSON 数组**；逐条独立处理 |
| GET | `/api/v1/messages/{message_id}` | 200 `MessageOut` | 不存在 404 |
| GET | `/api/v1/todos` | 200 `TodoListOut` | `?status=pending\|done\|all`，默认 `pending`；按 `overdue_seconds` 降序 |
| POST | `/api/v1/todos/{todo_id}/done` | 200 `TodoDoneOut` | 不存在 404；重复完成 200 `already_completed` |
| GET | `/healthz` | 200 `HealthOut` | **不探测任何渠道**，不访问网络 |

**批量语义（tasks 3.3）**：body 必须是 JSON 数组；逐条用 `MessageIn` 校验，失败的一条记
`accepted=False` + `error_fields`（来自 pydantic 的 `loc`，只取字段名）+ `index`，
合法的一条正常走 `pipeline.accept`。整体返回 **207**，`accepted_count`/`rejected_count` 为汇总。
单条非法**不得**影响其它条目。数组本身不是数组（如传对象）→ 422。

**422 语义（tasks 3.2）**：使用 FastAPI 默认的 `RequestValidationError` 处理器形状
（`{"detail": [{"loc": [...], "msg": ..., "type": ...}]}`）；字段名出现在 `loc` 中。
因为凭据从不进入请求体，不会泄漏。**MUST NOT 创建任何消息记录**（校验在路由函数体之前完成）。
**禁止**为空字段名之外的理由改写这个形状。

#### 3. 内部实现

- 路由依赖注入：`def _ctx(request: Request) -> AppContext: return request.app.state.ctx`，
  各路由用 `ctx: AppContext = Depends(_ctx)`。
- `create_api_router(ctx)` 把 `ctx` 闭包进路由，**并包含 `/healthz`**；
  `create_api_app(ctx)` = `FastAPI()` + `app.state.ctx = ctx` + `include_router(create_api_router(ctx))`，
  它**不得**启动任何后台线程（那是 `create_app` 的 lifespan 的职责）。
- `/healthz` 返回 `HealthOut(status="ok", time=ctx.clock.now(), version=notify_hub.__version__)`；
  **禁止**在其中调用 `delivery` 或 `registry`。
- `IngestPipeline` 的队列：`queue.Queue[int]`；工作线程
  `while not stop.wait(0.05): try: mid = q.get(timeout=0.05) ... finally: q.task_done()`；
  `drain()` 用 `q.join()` 加超时实现（`join` 无超时 → 用 `Event` + 轮询实现）。
  线程内异常必须捕获并记日志，**线程不得退出**。
- `accept()` 中 `classifier.classify` 是同步纯函数，不涉及 IO。
- 依赖：M1–M4、M6、阶段 0。**禁止**导入 M8（web）；`create_app()`（阶段 0）会挂载 web 路由，
  但本模块的 `create_api_app` **不得**依赖 M8 存在。

#### 4. 验证方法

测试文件：`tests/test_ingest.py`、`tests/test_api_todos.py`；
命令：`.venv/bin/python -m pytest tests/test_ingest.py tests/test_api_todos.py`。
用 `TestClient(create_api_app(ctx))`，`ctx` 来自 `conftest.py` 的 `ctx` fixture（`run_inline=True`）。

必须覆盖的可观察结果：
1. **成功投递**（tasks 3.1）：`POST /api/v1/messages` 带
   `{"source": "s", "title": "t", "level": "error", "need_ack": true}` → 状态码 202，
   响应含整数 `message_id`；用 `ctx.messages.get(id)` 断言该行已入库、`level == "error"`。
2. **进入待办**（tasks 3.1）：上一条 `need_ack=true` → 响应 `todo_id` 非空，`ctx.todos` 中该条
   `status == PENDING`，且其 `ack_reason == "caller_declared"`。
3. **缺必填字段**（tasks 3.2）：`{"title": "t"}` → 422，响应 JSON 的 `detail[*].loc` 中出现
   `source`，且 `ctx.messages.count() == 0`。
4. **非法 level**（tasks 3.2）：`level="critical"` → 422，`detail` 中出现 `level`，
   `count() == 0`。
5. **类型错误**（tasks 3.2）：`{"source": 1, "title": []}` → 422，`count() == 0`。
6. **默认语义**（tasks 3.1）：只给 `source`/`title` → 202；该行 `level == "info"`、
   `need_ack_declared is False`、`occurred_at == received_at`、`todo_id is None`、
   `ctx.todos.list()` 为空。
7. **批量部分失败**（tasks 3.3）：数组 3 条，第 2 条缺 `source` → 207；
   `results[0].accepted is True` 且含 `message_id`、`results[1].accepted is False` 且
   `index == 1` 且 `"source" in results[1].error_fields`、`results[2].accepted is True`；
   `accepted_count == 2`、`rejected_count == 1`；`ctx.messages.count() == 2`。
8. **批量整体类型错**：body 传 `{"messages": []}` → 422。
9. **不阻塞响应**（tasks 3.4）：用 `ctx` 但把 `pipeline` 换成 `run_inline=False` 且注册一个
   `send()` 内部 `time.sleep(2)` 的 notifier → `client.post(...)` 的耗时 **< 0.5 秒**；
   随后 `pipeline.drain(timeout=5)` 返回 `True`，且该消息最终有一条投递记录。
10. **消息详情**（tasks 3.5）：投递一条带 `meta={"a": 1, "b": [1, 2]}` 的消息 →
    `GET /api/v1/messages/{id}` 200，`meta` 原样返回、`category` 非空、`rule_id` 与规则一致、
    `deliveries` 是长度 ≥ 1 的数组且含 `channel_id`/`ok`/`is_preferred`；不存在 id → 404。
11. **待办列表**（tasks 7.1 接口一半）：构造 3 条不同 `first_notified_at` 的待办 →
    `GET /api/v1/todos` 的 `todos[*].id` 顺序为最久→最新；`?status=done` 只返回已完成；
    `?status=all` 两者都含。
12. **待办完成**（tasks 7.2 接口一半）：`POST /api/v1/todos/{id}/done` → 200
    `status == "completed"`；再次调用 → 200 `status == "already_completed"` 且
    `completed_at` 与第一次相同；不存在 id → 404；完成后 `GET /api/v1/todos` 不再含该条。
13. **健康检查**（tasks 1.3）：`GET /healthz` → 200，`status == "ok"`，`time` 可解析为 datetime；
    **把注册表清空（无可用渠道）后再请求仍为 200**（spec「渠道故障不影响探活」）。
14. **凭据不泄漏**（spec message-ingest「凭据隔离」）：注册一个 URL 含 `SECRETTOKEN` 但恒失败的
    webhook 渠道后发一条会触发 422 的请求 → 断言响应文本与 `caplog` 文本均不含 `SECRETTOKEN`。

**至少 2 个异常场景**：(a) 缺必填字段 422 且不写库（第 3 条）；(b) 详情查询不存在的 id 404（第 10 条）。

**完成后必须成立**：命令全绿；`grep -n "time.sleep" tests/test_ingest.py` 只出现在第 9 条的
故意慢 notifier 中。

---

### 模块 M8：Web 待办界面

**文件边界**
- 实现路径：`src/notify_hub/web/__init__.py`、`src/notify_hub/web/routes.py`、`src/notify_hub/web/templates/*.html`
- 测试路径：`tests/test_web.py`

#### 1. 功能

手机上也能点「完成」的服务端渲染页面。**不负责**：任何业务逻辑（复用 M6 的 service 层，
`design.md` 决策 7）、认证、前端框架。

#### 2. 接口

```python
# web/__init__.py
def create_web_router(ctx: AppContext) -> APIRouter
# web/routes.py
def create_web_app(ctx: AppContext) -> FastAPI:
    """仅挂载 web 路由，并把 ctx 写入 app.state.ctx。供模块级测试使用。"""
TEMPLATES_DIR: Path    # = Path(__file__).parent / "templates"
```

**页面（路径与行为冻结）**
| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/` | 303 重定向到 `/todos` |
| GET | `/todos` | 列表页；`?status=pending\|done\|all`，默认 `pending`；**按超时时长降序**；每行展示来源、分类、标题、首次通知时间、超时时长，并有进入详情与「完成」表单 |
| POST | `/todos/{todo_id}/done` | 调 `ctx.todos.complete()`，然后 **303 重定向**到 `/todos` |
| GET | `/todos/{todo_id}` | 详情页：关联消息的标题/正文/来源/级别/分类 + 历次通知的渠道、时间、结果 + 状态变更时间序列（按时间升序） |
| GET | `/messages` | 消息列表（`?limit=`，默认 50），按接收时间降序 |
| GET | `/messages/{message_id}` | 消息详情：分类结果（rule_id/category/labels/是否入待办及原因）+ 投递记录 |
| GET | `/todos/{todo_id}`（不存在） | 404 |
| POST | `/todos/{todo_id}/done`（不存在） | 404 |

**不变量**
- 所有渲染必须经过 Jinja2 自动转义（**禁止** `|safe`、**禁止** `Markup`）；消息正文可能含
  HTML，必须被转义（安全关键）。
- 页面不触发任何通知投递，也不修改除「完成」以外的状态。
- 时间在页面上以本地可读形式展示；超时时长与列表页一致地由 `TodoView.overdue_seconds` 格式化。

#### 3. 内部实现

- `Jinja2Templates(directory=str(TEMPLATES_DIR))`；模板文件：
  `base.html`、`todos_list.html`、`todo_detail.html`、`messages_list.html`、`message_detail.html`。
- 「完成」用 HTML `<form method="post" action="/todos/{id}/done">` + `<button>`；
  路由用 `Form`/无体 POST 均可（`python-multipart` 已在依赖中）。
- 超时时长格式化：本模块内实现一个小函数，**不得**导入 M6 的 `services.notifications`（避免
  web → services 的全量依赖；只用 `TodoView` 的数据）。
  说明：这条限制是为了让 M8 与 M7 在同一批次内不互相阻塞；只依赖 `AppContext` 与 `TodoView`。
- 依赖：M6 的 `TodoService`/`MessageService`（经 `ctx`）、阶段 0。**禁止**导入 M7 的 `api`/`pipeline`。
- 模板目录必须可通过 `importlib.resources`/`__file__` 定位，不依赖 cwd。

#### 4. 验证方法

测试文件：`tests/test_web.py`；命令：`.venv/bin/python -m pytest tests/test_web.py`。
用 `TestClient(create_web_app(ctx))`。

必须覆盖的可观察结果：
1. **列表排序**（tasks 7.1）：3 条待办 `first_notified_at` = `now-1h` / `now-5h` / `now-20m`
   → `GET /todos` 的 HTML 中三个标题出现的先后顺序为 5h、1h、20m（用 `html.index(title)` 比较）。
2. **列表字段**：HTML 中含来源、分类、标题、首次通知时间与超时时长字样（断言三个来源字符串都出现）。
3. **只看待完成**：一条 `pending` + 一条 `done` → 默认 `GET /todos` 不含已完成那条的标题；
   `?status=all` 含两者；`?status=done` 只含已完成那条。
4. **完成操作**（tasks 7.2）：`POST /todos/{id}/done` → 状态码 303 且 `Location` 指向 `/todos`；
   随后 `GET /todos` 不含该标题；再次 `GET /todos/{id}` 仍显示已完成（断言 `ctx.todos.get(id).status`
   为 `done`，且页面含「已完成」字样）。
5. **详情时间序列**（tasks 7.3）：构造「创建 → 2 次提醒 → 完成」（经 `ctx` 直接调 service 与
   `clock.advance`）→ `GET /todos/{id}` 中 4 个事件的文本按时间顺序出现（断言
   `created` 文案的 index < 第 1 次提醒 < 第 2 次提醒 < 完成），且页面含渠道 id 与投递结果。
6. **详情含原始消息**：`GET /todos/{id}` 含该待办关联消息的标题、正文、来源、级别、分类。
7. **消息页**（tasks 7.4）：`GET /messages` 列出消息；`GET /messages/{id}` 含分类结果
   （`rule_id`、`category`、标签）与实际使用的渠道 id。
8. **XSS 转义（安全关键）**：投递一条 `title="<script>alert(1)</script>"` 的消息并使其入待办 →
   `GET /todos` 与 `GET /todos/{id}` 的 HTML 中**不含** `<script>alert(1)</script>`，而含
   `&lt;script&gt;`。
9. **404**：`GET /todos/99999` → 404；`POST /todos/99999/done` → 404。
10. **根路径**：`GET /` → 303 且 `Location` 以 `/todos` 结尾。

**至少 2 个异常场景**：(a) 第 8 条的 XSS 转义；(b) 第 9 条的不存在 id。

**完成后必须成立**：命令全绿；`grep -rn "|safe" src/notify_hub/web/` 无输出。

---

### 模块 M9：文档与部署

**文件边界**
- 实现路径：`README.md`、`docs/configuration.md`、`docs/rule-authoring.md`、`docs/adapter-guide.md`、`docs/deployment.md`
- 测试路径：`tests/test_docs_contract.py`

#### 1. 功能

让使用者能照文档跑通首次投递与完成流程，让新渠道作者能只实现契约就接入。
**不负责**：任何运行时代码。

#### 2. 接口

无 Python 接口。**文档契约（冻结）**：
- `README.md`：项目简介、安装（`.venv` + `pip install -e ".[dev]"`）、配置入口、启动
  （`python -m notify_hub`）、HTTP 与 CLI 各一个可复制示例、完成流程示例、文档索引。
- `docs/configuration.md`：`config.example.yaml` 每个键的含义、环境变量与凭据的引用方式
  （**只写环境变量名**）、提醒参数三者关系与非法组合的报错。
- `docs/rule-authoring.md`：匹配语义（与/或）、`case_sensitive`、first-match-wins 的顺序影响、
  新增规则的步骤、一个**可被直接加载**的完整 YAML 示例。
- `docs/adapter-guide.md`：`Notifier` 契约、`DeliveryResult` 失败语义（**不得抛异常**）、
  `ChannelCapabilities`、注册方式、一个完整的 dummy 适配器示例。
- `docs/deployment.md`：systemd 单元示例、数据文件位置、**默认只绑定回环地址的强制安全边界**、
  跨机器使用需自加反向代理与鉴权（后续变更，不在本次范围）。

#### 3. 内部实现

- 文档中的 YAML 示例必须与 M1/M3 的解析器真实一致：**从 `config.example.yaml` /
  `rules.example.yaml` 复制而来，不得凭空编写**。
- 文档中的命令必须可复制执行；不得出现占位符以外的虚构路径。
- **禁止**在文档中写入任何真实凭据、真实域名或 token。
- 不得修改 `config.example.yaml` / `rules.example.yaml`（属 M1/M3）；发现不一致时在报告里写
  `BLOCKERS`。

#### 4. 验证方法

测试文件：`tests/test_docs_contract.py`；命令：`.venv/bin/python -m pytest tests/test_docs_contract.py`。
这是一个**文档契约测试**：把文档里的代码块当成可执行断言。

必须覆盖的可观察结果：
1. **规则示例可加载**（tasks 10.2）：从 `docs/rule-authoring.md` 中提取标记为
   ` ```yaml rules-example ` 的代码块，写入 `tmp_path/rules.yaml`，用 M3 的 `RuleLoader` 加载
   → `last_error is None` 且 `len(ruleset.rules) >= 1`；再用文档中描述的一条示例消息调用
   `RuleClassifier.classify`，断言得到的 `category` 与文档文字描述一致。
2. **配置键完整**（tasks 10.1）：解析 `config.example.yaml` 的顶层键集合，断言
   `docs/configuration.md` 中每个 `##`/`###` 小节标题里出现的键名都属于该集合（反之亦然：
   对 `config.example.yaml` 的每个顶层键，文档中必须出现其名字）。
3. **适配器示例可运行**（tasks 10.3）：从 `docs/adapter-guide.md` 中提取标记为
   ` ```python dummy-adapter ` 的代码块，`exec` 到一个命名空间，断言其中定义的类
   `isinstance(cls(...), Notifier)`（`runtime_checkable` Protocol）成立，且
   `cls(...).send(msg)` 返回 `DeliveryResult`；再把它 `register()` 进 `NotifierRegistry`，
   断言 `"dummy" in registry.ids()`——**且全程未修改 api/classifier 的任何文件**。
4. **README 命令面**（tasks 10.1）：提取 `README.md` 中所有 `notify ...` 行，断言其中出现的
   每个长选项名（`--source`、`--title`、`--body`、`--body-stdin`、`--level`、`--need-ack`、
   `--dedup-key`、`--endpoint`）都出现在 `typer` 的
   `notify_hub.cli.app` 的已注册参数名集合中（通过 `CliRunner().invoke(app, ["--help"])` 的输出断言）。
5. **部署安全边界**（tasks 10.4）：断言 `docs/deployment.md` 同时含「回环」或 `127.0.0.1`
   字样、`[Unit]`/`[Service]` 段、以及「不要暴露到公网」的显式警示句；断言
   `docs/deployment.md` 中的 `ExecStart` 行包含 `notify_hub`。
6. **无凭据泄漏**：断言五份文档的全文均不匹配
   `re.compile(r"(?i)(token|password|secret)\s*[:=]\s*[A-Za-z0-9_\-]{12,}")`。
7. **文档索引可达**：`README.md` 中出现的每个 `docs/*.md` 相对链接指向的文件都存在。

**至少 2 个异常场景**：(a) 从文档中提取的规则 YAML 若加载失败必须让测试失败（即该测试本身是
异常门禁）；(b) 第 6 条的凭据正则扫描。

**完成后必须成立**：命令全绿；`README.md` 与 `docs/` 下四份文件均存在且非空。

---

## 7. 阶段 D 集成测试（`subagent_verify` 作用域 2）

**文件边界**：`tests/test_integration.py`、`tests/test_e2e.py`（不属于任何模块）。

**必须真实跨越的接缝**（不许打桩在模块内部边界上）：

| 接缝 | 测什么 |
|---|---|
| `create_app` → api/web/classifier/scheduler | 组合根真实装配：`create_app(settings)` 能构造、`/healthz` 与 `/todos` 同时可达 |
| HTTP 接入 → 分类器 → 落库 → 待办 → 派发 → 真实 webhook 适配器 | 端到端：起一个本机 `http.server` 桩，POST 一条 `need_ack=true` 的消息，断言桩收到 JSON、DB 有消息与待办 |
| `ReminderScheduler` → `DeliveryService` → webhook | 缩短间隔后提醒真的发到桩上，且文案含标题与超时时长 |
| CLI → 真实 uvicorn → app | 用 `uvicorn.Server` 起在临时端口，`notify --endpoint http://127.0.0.1:<port> ...` 退出码 0 且桩渠道收到通知 |
| Web 页面 → service → DB | 页面上点完成（POST）后，`ctx.todos.get()` 状态为 done，且后续不再提醒 |
| 规则热加载 | 运行中改写规则文件 → 新消息按新规则分类 |

**任务 9.5 的端到端场景（必须实现为 `tests/test_e2e.py::test_full_lifecycle`）**：
投递 `need_ack=true` 消息 → 缩短提醒间隔 → `ManualClock.advance` + `scheduler.run_once()`
收到提醒 → 通过 Web 页面标记完成 → 再 `run_once()` 不再提醒。
断言点：待办状态、`todo_events` 的 4 个事件、桩渠道收到的 2 条通知（首次 + 1 次提醒）、
完成后无新通知。

**运行命令**：`.venv/bin/python -m pytest tests/test_integration.py tests/test_e2e.py`。

---

## 8. tasks.md → 模块映射（防遗漏）

| tasks.md | 归属 |
|---|---|
| 1.1 | 阶段 0（架构师）+ M1 |
| 1.2 | M1 |
| 1.3 | M7（`/healthz`）+ 阶段 0（`create_app`） |
| 1.4 | M1 |
| 2.1 – 2.4 | M2 |
| 3.1 – 3.5 | M7 |
| 4.1 – 4.5 | M3 |
| 5.1 – 5.6 | M4 |
| 6.1 – 6.5 | M6 |
| 6.6 | M1（校验在 `load_settings`） |
| 7.1 – 7.4 | M8（页面）+ M6（service 排序/详情） |
| 8.1 – 8.3 | M5 |
| 9.1 | M7 的 `tests/test_ingest.py` |
| 9.2 | M3 的 `tests/test_classifier.py` |
| 9.3 | M4 的 `tests/test_notifiers.py` |
| 9.4 | M6 的 `tests/test_todos.py` |
| 9.5 | 阶段 D 的 `tests/test_e2e.py` |
| 9.6 | M1（日志脱敏）+ M4（记录脱敏）+ M7（响应脱敏） |
| 10.1 – 10.4 | M9 |

---

## 9. 与设计文档的偏差记录（审查者必看）

| # | 偏差 | 理由 | 设计文档依据 |
|---|---|---|---|
| **D-1** | 首次通知的异步化用**进程内 `queue.Queue` + 守护工作线程**（M7 `IngestPipeline`），而非 `design.md` 决策 4 提到的 `BackgroundTasks` 起步 | `BackgroundTasks` 在 Starlette 的 `TestClient` 下会**阻塞响应直到任务结束**，导致 tasks 3.4「慢渠道下响应 < 500ms」无法被测试固定；线程+队列真正兑现「投递不阻塞响应」的意图，且不引入外部中间件（仍符合 non-goal） | 决策 4 的意图，非其字面实现 |
| **D-2** | 待办的 `first_notified_at` 在**受理时刻**写入（而非等首次投递尝试完成后回填） | spec 要求「首次通知时间」用于计算超时时长。若等背景派发回填，进程在派发前崩溃会导致该待办永不计入超时、永不提醒——违背 todo-tracking 的核心承诺。受理时刻与首次派发在实践上同一瞬间 | `specs/todo-tracking/spec.md` 待办生成 |
| **D-3** | `last_notified_at` 在**每次提醒后无条件更新**（与投递成败无关） | 同时满足 spec「提醒节奏保持稳定（每次至少间隔一个间隔）」与「失败后下一个提醒周期再次尝试」；扫描式实现只比较时间戳，不引入重试状态机 | 决策 2 与决策 6 |
| **D-4** | 凭据缺失**不导致启动失败**，而是把该渠道标记为不可用并可降级 | spec notification-delivery 明确把「凭据缺失」列为渠道不可用的原因之一 | 决策 1/决策 5 |
| **D-5** | 消息与待办都持久化 `preferred_channel` | spec 要求提醒「通过该待办对应的渠道」发送；待办行必须自洽，不能依赖运行期重新分类 | 决策 5 |
| **D-6** | 分类器不接收 `Settings`，只接收 `rules_path` 与数值参数 | 解耦 M3 与 M1，使二者可在同一批次并行；组合根负责注入 | — |
| **D-7** | 首版不含 Alembic 迁移链，`init_schema()` 用幂等 `create_all` | 全新项目无历史版本；`design.md` 迁移计划即「无数据迁移」 | 决策 5 / Migration Plan |
| **D-8** | CLI 退出码冻结为 0/1/2/3（1=服务端拒绝、2=用法错误、3=不可达） | spec 只要求「非零」；但测试必须能区分三种失败，且避免与 click 的用法错误码 2 冲突 | — |
| **D-9** | **`httpx` logger 被压到 `WARNING`**（当前实现位置：`src/notify_hub/notifiers/webhook.py` 导入时） | **安全必要**：实测 `httpx` 在 INFO 级别记录完整请求 URL，其中含 `access_token` 等凭据，会直接违反 spec notification-delivery 的「凭据管理」——凭据 MUST NOT 出现在日志中。M1 的 `SecretFilter` 装在**根 logger 的 handler** 上，而 `httpx` 的记录经 propagation 直达 handler、且 pytest 的 `caplog` 自带独立 handler，因此 filter 拦不住它。压制该 logger 是当前唯一简洁有效的止血方式 | spec notification-delivery「凭据管理」；`design.md` 决策 8 |

### 9.1 阶段 A 的过程记录（审查者需要核实的事项）

测试先行阶段中，各模块测试作者回传的 `SPEC-GAPS` 已由架构师逐条补进本规格（不是让测试作者修改断言）。
以下过程事实需要在阶段 E 核实：

- **M2 的测试作者在 `/tmp` 下写过一次性参考实现**（以 `PYTHONPATH` 影子包方式，**未触碰 `src/`**），
  用来排除「测试自身写错」造成的假红，并做了变异检验（把部分唯一索引退化为普通唯一索引 → 2 个
  用例转红；去掉父目录 mkdir → 对应用例转红）。它声明该临时实现已删除。
  **审查者需核实**：`tests/` 与 `src/` 中不存在对 `/tmp/m2shim` 等影子包的残留引用
  （`grep -rn "m2shim\|m2mut\|PYTHONPATH" tests/ src/`）。
- 该做法**不违反**测试先行的独立性：测试仍然是从规格推导的，参考实现在测试之后、且从未进入仓库；
  变异检验反而证明了断言确实有判别力。
- **阶段 D（集成测试）的作者同样在 `/tmp` 影子副本上做过变异检验**（`stop()` 不复位 `running` →
  2 个用例转红；webhook 载荷键改名 → socket 载荷用例转红），同样未触碰仓库 `src/`。
- 各模块测试作者均声明未修改 `src/`、`tests/conftest.py`、`pyproject.toml`、`openspec/`。
  **审查者需用 git 核实**这一点，而不是采信自报。
- **一次已裁决的 test-conflict**（M7）：`tests/test_ingest.py` 以位置参数调用 `make_recording_notifier`，
  而该 fixture 当时只收关键字参数。裁定为**架构师的文件规格不足**（该 fixture 未写入本文档
  3.2 节，测试作者只能按 `RecordingNotifier.__init__` 的自然形状推断），因此修的是 `tests/conftest.py`
  （架构师所有）而非测试；测试断言强度未变。3.2 节已补齐全部 fixture 的签名。

### 9.2 P1 凭据泄漏：发现与修复（阶段 E 审查产出）

**发现**：独立审查（阶段 E）复现了一条 P1——webhook 平台在业务错误 `msg` 中回显**裸 token** 时，
token 以明文写入 `DeliveryRecord.error_reason`，并经 `GET /api/v1/messages/{id}` 与日志外泄。
架构师已用独立脚本复核确认（`LEAKED: True`）。

**根因（两层，第二层是规格缺陷）**：
1. 生产装配下 `credential_values()` 返回的是 webhook 的**完整 URL**，而泄漏出来的是 **URL 中的
   裸 token**；`redact_text` 做子串匹配，两者对不上。
2. **规格缺陷**：M4 原第 13 条测试用的是「transport 返回 500」，而 500 的 `error_reason` 是
   `HTTP 500`、**根本不回显响应体**；且规格未规定 `secrets` 的构造形态。于是测试作者用了
   「裸 token 作 secrets」——一个生产中不存在的形态。166 个测试全绿，缺陷零覆盖。

**修复（用户裁定方案 A：修在源头）**：
- 新增 `redact.py::extract_url_secrets(url, *, min_length=6)`，提取 URL 内嵌凭据成分。
- `credential_values()` 对 URL 形态的凭据值做**展开**，把内嵌成分作为独立密钥加入。
- 补两条回归闸门：M1 第 4 段第 5/5b 条，M4 第 4 段第 **13b** 条（**必须经
  `credential_values()` 构造 secrets，禁止手工写裸 token**）。

**为什么加 `min_length=6`**：`SENSITIVE_KEYS` 含通用键名 `key`，若把 `?key=1` 的 `"1"` 收进全局
密钥集合，`redact_text` 会把日志里所有 `1` 都打成 `***`，日志不可读。短成分不进入全局集合，
但仍由 `redact_url` 在 URL 内部按键名掩码，不会因此暴露。

**教训（对后续模块）**：安全类断言必须用**生产装配的真实数据形态**构造输入
（走 `load_config → credential_values`），不得手工拼一个「看起来对」的形态。

---

## 10. 文件边界互斥性检查

逐对检查结果（模块实现路径 × 其它模块测试路径、模块测试路径 × 其它模块实现路径）：

- M1–M9 的实现路径两两不相交，且都在 `src/notify_hub/` 的不同子路径下。
- M1–M9 的测试路径两两不相交，且都以 `tests/test_<模块>.py` 命名。
- 没有任何模块的测试路径落在另一模块的实现路径内，反之亦然。
- 阶段 0 的 9 个文件不在任何模块边界内，任何模块都不得修改。
- `tests/conftest.py` 只由架构师维护；模块新增 fixture 必须放在自己的测试文件里。
