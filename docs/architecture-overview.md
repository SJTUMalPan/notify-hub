# notify-hub 代码走读

> 面向第一次读这个代码库的人。目标：读完你能知道**每一条消息从进来到送达，经过了哪些文件、哪些函数**。
>
> 配套的可视化页面：**[`architecture-map.html`](./architecture-map.html)**（纯前端单文件，双击即可打开，无需服务器）。
> 契约级规格（接口签名、不变量、偏差记录）在 `../openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`。

---

## 1. 它是什么

一台常驻服务，做四件事：

```
进程投递消息  →  按配置规则分类  →  需要人处理的变成待办  →  通过渠道通知你，直到你点完成
```

它**不做**消息队列消费者、不做投递可靠性保证（不追求 at-least-once）、不做多用户与权限体系。

**技术栈**：Python 3.10 · FastAPI + Uvicorn · SQLModel/SQLite · Jinja2 · httpx · Typer · PyYAML

---

## 2. 目录结构

```
message/
├── pyproject.toml              依赖、打包、pytest 配置、notify 命令行入口
├── config.example.yaml         配置示例（复制成 config.yaml 使用）
├── rules.example.yaml          分类规则示例
├── README.md                   安装/配置/启动/示例
├── docs/                       使用文档（本文件所在目录）
├── openspec/                   需求与规格（提案、设计、模块规格）
├── src/notify_hub/             全部实现
└── tests/                      267 个测试
```

### 2.1 `src/notify_hub/` 根目录 —— 共享契约与组合根

这些文件**不属于任何单个功能模块**，它们被多个模块共同依赖，改动会影响全局。

| 文件 | 职责 | 关键符号 |
|---|---|---|
| `domain.py` | **跨模块共享的领域类型**。所有枚举与「分类结论」都在这里，不导入任何其它子模块（避免环） | `Level` `TodoStatus` `AckReason` `DeliveryEvent` `ClassificationVerdict` |
| `errors.py` | 共享异常类型 | `NotifyHubError` `ConfigurationError` `RuleFileError` `TodoNotFound` |
| `clock.py` | **时间来源与归一化**。`ManualClock` 让测试能冻结/推进时间，不必真实等待 | `Clock` `SystemClock` `ManualClock` `as_utc()` |
| `config.py` | 加载并校验 `config.yaml`；解析凭据环境变量；提供脱敏用的密钥集合 | `Settings` `ChannelSpec` `ReminderSettings` `load_settings()` `credential_values()` |
| `redact.py` | **脱敏工具链**（安全关键）。掩码日志/记录里的 URL 与密钥 | `redact_text` `redact_url` `extract_url_secrets` `redact_exception` |
| `logging_setup.py` | 日志初始化，把 `SecretFilter` 装到各 handler 上 | `setup_logging` `SecretFilter` |
| `context.py` | **组合根**。按固定顺序装配全部对象，产出 `AppContext` | `AppContext` `build_context()` `build_test_context()` |
| `app.py` | FastAPI 应用工厂：挂载路由 + lifespan 启停后台线程 | `create_app()` |
| `main.py` / `__main__.py` | 进程入口 `python -m notify_hub` | `main()` |

> **为什么组合根要独立成文件**：如果每个模块各自去 `import` 并 new 别的模块，就没人能一眼看清
> 「谁依赖谁、启动顺序是什么」。把这些集中在 `context.py`，模块本身退化成「构造签名已固定的库」。

### 2.2 `db.py` / `models.py` —— 持久化

| 文件 | 职责 | 关键符号 |
|---|---|---|
| `db.py` | 引擎、会话上下文、幂等建表 | `Database`（`session()` 用 `expire_on_commit=False`） |
| `models.py` | 五张表的 ORM 定义 | `Message` `Todo` `DeliveryRecord` `TodoEvent` `DigestRun` |

**五张表**：

| 表 | 一行代表什么 |
|---|---|
| `messages` | 一条接入的消息 + 它的分类结论（命中哪条规则、分类名、标签、是否入待办及原因） |
| `todos` | 一条需要你处理的待办（状态、首次/上次通知时间、提醒次数、完成时间） |
| `deliveries` | **每一次投递尝试**（渠道、时间、成功与否、失败原因、是否首选/是否降级） |
| `todo_events` | 待办的时间序列（创建 / 每次提醒 / 完成），详情页靠它回溯 |
| `digest_runs` | 某个**本地自然日**的汇总状态（是否已定案/已送达、覆盖待办数、尝试次数、失败原因） |

`todos` 上有一条**部分唯一索引**：`(source, dedup_key) WHERE dedup_key IS NOT NULL AND status='pending'`
——保证重复投递不产生重复待办，但待办完成后同样的 key 可以再建。

### 2.3 `classifier/` —— 配置驱动的分类

| 文件 | 职责 | 关键符号 |
|---|---|---|
| `rules.py` | 规则文件的 schema 与解析（YAML → 数据类） | `Rule` `MatchCondition` `RuleSet` `parse_ruleset()` |
| `engine.py` | 匹配引擎：同规则内多条件「与」、候选值「或」、**首个命中生效** | `RuleEngine.classify()` |
| `loader.py` | 规则文件加载与 **mtime 轮询热加载**；坏配置保留上一份可用规则集 | `RuleLoader` |
| `__init__.py` | 对外门面：给上层用的分类器 + 后台重载线程 | `RuleClassifier`（`classify()` `start()` `stop()`） |

### 2.4 `notifiers/` + `delivery.py` —— 通知投递

| 文件 | 职责 | 关键符号 |
|---|---|---|
| `base.py` | **适配器契约**：统一消息结构、能力声明、成败值对象 | `Notifier`(Protocol) `NotificationMessage` `DeliveryResult` `ChannelCapabilities` |
| `registry.py` | 按配置构造渠道实例；记录不可用原因 | `NotifierRegistry` `NOTIFIER_FACTORIES` |
| `webhook.py` | 通用 Webhook 适配器（平铺 JSON、可配字段映射与请求头） | `WebhookNotifier` |
| `email.py` | SMTP 邮件适配器 | `EmailNotifier` |
| `feishu.py` | **飞书专属适配器**（嵌套载荷 + 加签） | `FeishuNotifier` `sign_feishu()` |
| `delivery.py` | **渠道选择与降级**：首选 → 默认 → 其余；每次尝试写投递记录 | `DeliveryService` `DeliveryOutcome` |

> **为什么要飞书专属适配器**：通用 `webhook` 发的是平铺 JSON，而飞书要求
> `{"msg_type":"text","content":{"text":"..."}}` 这种**嵌套**结构，`field_map` 只做顶层键改名、
> 表达不了嵌套。这正是「适配器契约」要兑现的价值：新增渠道不必动接入层、分类器与待办逻辑。

### 2.5 `services/` —— 领域服务与提醒调度

| 文件 | 职责 | 关键符号 |
|---|---|---|
| `messages.py` | 消息读写 | `MessageService` `MessageDraft` |
| `todos.py` | 待办生成/去重/完成/列表/详情，逐条提醒记账 | `TodoService` `TodoView` `TodoDetail` |
| `notifications.py` | 把消息行渲染成与渠道无关的通知文案（首次通知与**每日汇总**） | `notification_for_message()` `notification_for_digest()` `format_duration()` |
| `digest.py` | **每日汇总的状态服务**：按本地日期记录「已检查/已送达/失败重试」 | `DigestService` `DigestState` |
| `scheduler.py` | **每日汇总调度**（守护线程，周期扫描本地时刻） | `ReminderScheduler` |

### 2.6 `api/` + `pipeline.py` —— HTTP 接入与受理编排

| 文件 | 职责 | 关键符号 |
|---|---|---|
| `schemas.py` | 请求/响应的 Pydantic 模型（**字段名即对外契约**） | `MessageIn` `MessageOut` `TodoOut` … |
| `messages.py` | 投递、批量投递、消息详情 | `router`（前缀 `/api/v1/messages`） |
| `todos.py` | 待办列表与完成 | `router`（前缀 `/api/v1/todos`） |
| `health.py` | `/healthz`（**不探测任何渠道**） | `healthz()` |
| `__init__.py` | 组装 api 路由 / 供模块测试的应用 | `create_api_router()` `create_api_app()` |
| `pipeline.py` | **受理编排**：分类 → 落库 → 生成待办 → 后台派发 | `IngestPipeline` `AcceptOutcome` |

### 2.7 `web/` —— 服务端渲染的待办界面

| 文件 | 职责 |
|---|---|
| `routes.py` | 页面路由 + 模板渲染；`create_web_router()` / `create_web_app()` |
| `templates/base.html` | 布局骨架 |
| `templates/todos_list.html` | 待办列表（按超时时长降序，含「完成」表单） |
| `templates/todo_detail.html` | 待办详情（原始消息 + 历次投递 + 时间序列） |
| `templates/messages_list.html` | 消息列表 |
| `templates/message_detail.html` | 消息详情（分类结果 + 投递记录） |

### 2.8 `cli.py` —— 命令行客户端

给 shell 脚本 / cron 用的独立 HTTP 客户端：`notify`（投递）、`notify todo list`、`notify todo done <id>`。
**不导入本项目任何其它模块**，只走 HTTP，因此不会绕过接入层的校验与记录逻辑。
退出码：`0` 成功 / `1` 服务端拒绝 / `2` 用法错误 / `3` 服务不可达。

---

## 3. 启动时发生了什么

```
python -m notify_hub
  └─ main.py::main()
       ├─ config.py::load_settings()          读 config.yaml、解析环境变量里的凭据
       └─ app.py::create_app(settings)
            └─ context.py::build_context()     ← 组合根，固定顺序装配：
                 1. credential_values(settings)   算出所有密钥字面量（供脱敏）
                 2. setup_logging(..., secrets)   装 SecretFilter，此后日志自动脱敏
                 3. Database(db_path).init_schema()   幂等建表
                 4. SystemClock()                 生产时钟
                 5. RuleLoader + RuleClassifier   构造时加载一次规则（坏了也不拒服务）
                 6. NotifierRegistry.build_from_specs()   按配置造渠道；不可用的记原因
                 7. DeliveryService               渠道路由与降级
                 8. MessageService / TodoService  领域服务
                 9. IngestPipeline(run_inline=False)  受理 + 后台工作线程
                10. ReminderScheduler              每日汇总调度（注入 DigestService）
            ├─ app.include_router(create_api_router(ctx))   API + /healthz
            └─ app.include_router(create_web_router(ctx))   Web 页面
```

**lifespan（服务起停时的动作）**：

```
启动：classifier.start() → pipeline.start() → scheduler.start()
        ↑能分类了          ↑能收了           ↑最后才开始提醒
关闭：scheduler.stop() → pipeline.stop() → classifier.stop() → db.dispose()
```

三条后台线程的名字可以直接在日志/调试器里认出来：

| 线程名 | 来源 | 干什么 |
|---|---|---|
| `notify-hub-rule-reloader` | `classifier/__init__.py` | 轮询规则文件 mtime，变更即热加载 |
| `notify-hub-ingest-worker` | `pipeline.py` | 从队列取 message_id，做首次通知 |
| `notify-hub-reminder-scheduler` | `services/scheduler.py` | 周期扫描本地时刻，每天到点发一条汇总 |

---

## 4. 数据流：一条消息的完整旅程

下面每一步都给出**确切的文件与函数**，可以照着断点调试。

### 主链路（HTTP 投递一条 `need_ack=true` 的消息）

```
① 接入         curl -XPOST /api/v1/messages
                 api/messages.py::post_message()
                 ├─ 校验由 schemas.py::MessageIn 完成（422 时不落库）
                 └─ _draft() 把请求体转成 services/messages.py::MessageDraft
                        │
② 受理编排     pipeline.py::IngestPipeline.accept()
                        │
③ 分类          ├─ classifier/__init__.py::RuleClassifier.classify()
                 │    └─ classifier/engine.py::RuleEngine.classify()
                 │         · 按声明顺序找首个命中的规则（同规则内条件「与」、候选值「或」）
                 │         · 入待办 = 调用方声明 need_ack OR 规则要求（声明优先，规则不得降级）
                 │         └→ domain.py::ClassificationVerdict
                        │
④ 落库          ├─ services/messages.py::MessageService.create(draft, verdict)
                 │    └─ db.py::Database.session() → models.py::Message
                 │
⑤ 生成待办      ├─ services/todos.py::TodoService.ensure_for_message(message)
                 │    · verdict.need_ack 为假 → 返回 None（纯通知）
                 │    · 否则按 (source, dedup_key) 去重后建 todos 行 + todo_events(created)
                 │
⑥ 派发          └─ run_inline ? dispatch() : queue.put(message_id)   ← 立即返回 202
                        │
        ┌───────────────┘  （以下是后台线程 notify-hub-ingest-worker 在做）
        ▼
⑦ 构造通知      pipeline.py::dispatch()
                 └─ services/notifications.py::notification_for_message(kind=FIRST_NOTICE)
                      · body 里已含「来源 / 分类 / 时间 + 空行 + 原文」
                        │
⑧ 选渠道并投递  delivery.py::DeliveryService.deliver(notification, preferred_channel=…)
                 ├─ 候选顺序：分类首选渠道 → 默认渠道 → 其余（去重保序）
                 ├─ notifiers/registry.py::NotifierRegistry.get(channel_id)
                 ├─ notifiers/feishu.py::FeishuNotifier.send()   （或 webhook / email）
                 │    ├─ 加签（若配置了 secret）→ sign_feishu()
                 │    ├─ POST（业务错误码非 0 也算失败）
                 │    └─ 失败路径经 redact.py::redact_exception() 脱敏
                 └─ 每一次尝试都写一行 models.py::DeliveryRecord
```

### 提醒链路（你没点完成时）—— 每天固定本地时间一条汇总

```
services/scheduler.py::ReminderScheduler._loop()      （线程 notify-hub-reminder-scheduler）
  └─ 每 scan_interval_seconds 调一次 run_once()
       ├─ 按 settings.timezone 把 clock.now() 换算成本地时间
       │    · 本地时间尚未到 settings.at（默认 21:00）→ 本轮什么都不做
       ├─ services/digest.py::DigestService.state_for(today)
       │    · 当日已定案（已送达，或已记过「空待办」）→ 本轮什么都不做
       ├─ 本地时间已越过触发时刻 → 第一轮检查就定案
       │    · 没有未完成待办 → 记 todo_count=0 并定案，当天不再发
       ├─ 一次性取全部未完成待办（最多 1000 条，按超时时长降序）
       ├─ services/notifications.py::notification_for_digest(todos, now=…)
       │    · 标题「[待办汇总] N 项未完成」，正文逐条列「标题 + 已超时 … + 来源/分类」
       ├─ DeliveryService.deliver(..., preferred_channel=None)   ← 汇总统一走 default_channel
       │    · 失败 → DigestService 记失败原因，当日下一轮仍会重试（跨天不补发）
       └─ 成功后逐条记账：TodoService.record_reminder()
            · 每条待办 last_notified_at / reminder_count 更新，各写一条 todo_events(reminder)
            · 汇总本身在 deliveries 里只留**一条**记录（message_id / todo_id 均为 None）
```

重试与重启都靠 `digest_runs` 表：同一本地日期只有一行，进程重启不会重复发送，
也不会吞掉当天的汇总。

### 完成链路（在页面上点「完成」）

```
Web  POST /todos/{id}/done            或     CLI  notify todo done <id>
  └─ web/routes.py 的表单处理                └─ HTTP POST /api/v1/todos/{id}/done
                                                 api/todos.py::complete_todo()
                                                       └─ services/todos.py::TodoService.complete()
                                                            · 记录 completed_at，写 todo_events(completed)
                                                            · 幂等：重复完成不改变原完成时间
                                                            · 此后不再出现在每日汇总的未完成待办里
```

### 另外两条入口

| 入口 | 路径 | 说明 |
|---|---|---|
| 批量投递 | `POST /api/v1/messages/batch` | 请求体是**裸 JSON 数组**，逐条独立处理，返回 **207** 与逐条结果 |
| CLI 投递 | `notify --source … --title …` | `cli.py` 走 HTTP，与 curl 完全同一条链路 |
| 健康检查 | `GET /healthz` | 只返回服务状态与时间，**不探测任何渠道** |

---

## 5. 四条跨模块不变量（读代码时最容易踩的地方）

### 5.1 时间：从库里读出来的时间一定是「裸」的

SQLite 不保留时区，`DateTime(timezone=True)` 存进去的 aware 时间读出来是 **naive** 的。
所以**任何参与比较或算术的时间，必须先经 `clock.py::as_utc()` 归一化**。
漏掉这一步，超时计算会静默错掉（不是报错）。

### 5.2 凭据：适配器从自己的 URL 派生脱敏密钥

脱敏密钥集合（`secrets`）由 `config.py::credential_values()` 产出，它会把 URL 里内嵌的凭据
**展开**成独立密钥——包括 **query 参数**（`?access_token=…`）和**路径片段**（`/hook/<token>`）。

但**更关键的一层**：每个适配器在 `__init__` 里还会自己调
`redact.extract_url_secrets(自己的 URL)` 并合并进去。也就是说：
**即使调用方忘了传 `secrets`，适配器也不会把凭据写进日志或投递记录。**

> 这条不变量是被同一类缺陷咬了三次之后才定下来的（query token → 路径 token → 调用方没传参）。
> 前两次的修法都落在「某个调用点」，而调用点会随适配器数量增长；第三次把责任放回
> **持有凭据的那个对象**。

### 5.3 并发：除 FastAPI 路由外，全是同步代码

`Notifier.send()` 是**同步**的；两个后台组件是**守护线程 + `threading.Event`**，不是 asyncio。
首次通知不阻塞响应：`IngestPipeline` 用 `queue.Queue` + 单个工作线程承接投递
（不是 FastAPI 的 `BackgroundTasks`——那在 `TestClient` 下会阻塞响应，导致「慢渠道不影响响应」无法被测试固定）。

### 5.4 正文归属：`NotificationMessage.body` 已经是成品

`services/notifications.py::notification_for_message()` 产出的 `body` **已经包含**
`来源`/`分类`/`时间` 与空行、原始正文；每日汇总的
`services/notifications.py::notification_for_digest()` 同样把逐条明细原样放进 `body`
（标题含「[待办汇总] N 项未完成」，明细含「已超时 …」）。

**适配器只负责呈现层的标题，正文一律原样使用 `msg.body`。**
曾经的缺陷：飞书与邮件适配器各自又拼了一层表头，导致用户看到 `来源`/`时间` 重复两次。

---

## 6. 建议的阅读顺序

1. `domain.py` + `errors.py` + `clock.py` —— 10 分钟，看懂全项目的类型词汇
2. `config.example.yaml` + `rules.example.yaml` —— 知道「配置长什么样」
3. `context.py::_assemble()` —— 一眼看清有哪些对象、谁依赖谁、启动顺序
4. `pipeline.py::accept()` —— 主链路就这么 20 行
5. `delivery.py::deliver()` —— 渠道选择与降级
6. `services/todos.py` + `services/scheduler.py` —— 待办与提醒（本项目最有业务味的部分）
7. `notifiers/base.py` —— 想新增渠道时，从这里开始
8. `api/schemas.py` —— 对外契约的字段名都在这儿

---

## 7. 可视化页面

打开 **[`architecture-map.html`](./architecture-map.html)**（双击即可，无需服务器）。

页面上有三个视图：

- **数据流步进器** —— 点任一步骤，右侧显示「发生了什么 / 涉及哪些文件与函数 / 产出什么」，
  并在左侧架构图上高亮参与该步的模块。
- **模块地图** —— 按分层列出全部文件与职责，支持关键字过滤。
- **不变量与后台线程** —— 四条跨模块约定与三条常驻线程。
