# 架构与模块规格：add-public-access

> 本文件是**冻结接口**的唯一来源。子代理任务书只引用本文件的章节号，不重述内容。
> 设计基线见同目录 `design.md`；被本变更取代的旧决策见 `design.md` D9。

## 1. 交付范围与规模

| 项 | 值 |
|---|---|
| 模块数 | **1** |
| 预计派发次数 | **4**（模块测试 1 + 实现 1 + 集成测试 1 + 审查 1） |
| 阈值 | 未超过 8 个模块，无需分批 |

阶段 0 的共享文件由架构师在 fan-out 前完成，不计入派发。

## 2. 阶段 0：共享文件处置

这类文件不属于任何模块，两个执行者同时改会互相覆盖。**全部由架构师在 fan-out 之前处置完**，
模块**不得**修改其中任何一个；需要改就停下来报告。

| 文件 | 处置 | 内容 |
|---|---|---|
| `src/notify_hub/config.py` | 架构师，阶段 0 | `Settings` 增 `auth_token: str | None = None`；`credential_values` 把非空令牌并入返回值（字面量级脱敏） |
| `src/notify_hub/config.py` 的 `_parse_auth_token` | 架构师，阶段 0 | `server.auth_token` 的**冻结语义**：键缺失或值为 YAML `null` → `None`（不启用鉴权）；值必须是字符串，`strip()` 后仍非空，否则抛 `ConfigurationError`（非字符串与纯空白/空串都算）；返回值是 **`strip()` 之后**的字符串。测试作者按此写断言，不要自行推断是否 strip |
| `src/notify_hub/main.py` | 架构师，阶段 0 | 抽出纯函数 `build_uvicorn_kwargs(settings) -> dict`，返回 `{"host","port","log_config": None}`；`main()` 用它调 `uvicorn.run` |
| `src/notify_hub/app.py` | 架构师，**阶段 B 末尾**（此时 M-A 已存在） | `create_app` 装载 `AuthGuard` 中间件 |
| `tests/conftest.py` | **不改** | 造带令牌的 `Settings` 用 `dataclasses.replace(tmp_settings, auth_token="...")` 即可（`Settings` 是 frozen dataclass） |
| `docs/deployment.md` | 架构师 | 公网暴露与令牌轮换章节 |
| `~/notify-hub-run/` 启动脚本 | 架构师（运维） | 转发进程 + 未配置令牌时拒绝启动转发 |
| `openspec/**` | 架构师 | 本变更全部产物 |

**`app.py` 为什么延期到阶段 B 末尾**：它要 `from notify_hub.auth import AuthGuard`，而该模块
在阶段 A 时尚不存在。提前应用会让 4 个测试文件在收集期就 ImportError——`add-notify-hub`
的 D 系列已经踩过这个坑（见其 architecture §2.1）。装配行在阶段 B 末尾补，属预期，不是遗漏。

## 3. 模块 M-A：访问认证（`auth`）

**文件边界**

- **实现路径**（`subagent_dev`）：`src/notify_hub/auth.py`
- **测试路径**（`subagent_verify`）：`tests/test_auth.py`

两组不得重叠。`src/notify_hub/app.py`、`config.py`、`main.py` 归架构师，**两者都不得修改**。

### 3.1 功能

为整个 ASGI 应用提供统一的凭据校验：把「URL 查询串里的令牌」或「会话 Cookie」判定为
一次已认证请求，未通过者一律 `401`。

**做**：令牌校验、Cookie 换取、`/healthz` 例外、未配置令牌时透传、`401` 响应体按 `Accept` 分型。

**不做**：不做限流、不做登录页、不做用户体系、不做权限分级（单用户单令牌）、
不做 TLS、**不写日志**（凭据系统里没有任何日志调用，避免自身成为泄漏源）、
不导入 `notify_hub.web` / `notify_hub.api`（不得与业务路由耦合）。

### 3.2 接口

模块级常量：

```python
COOKIE_NAME = "nh_session"                          # str
SESSION_MESSAGE = b"notify-hub-session-v1"          # bytes，HMAC 的固定消息
HEALTH_PATH = "/healthz"                            # str，唯一免鉴权路径
```

```python
def session_value(token: str) -> str
```
- 返回 `hmac.new(token.encode("utf-8"), SESSION_MESSAGE, hashlib.sha256).hexdigest()`，小写十六进制、定长 64。
- 纯函数：同一 `token` 恒返回同一值；不同 `token` 极大概率不同；**不是**令牌本体的任何可逆编码。
- `token` 为空串时抛 `ValueError`（调用方必须先判空）。

```python
def strip_token_param(query_string: str) -> str
```
- 入参是不带 `?` 的原始查询串（`scope["query_string"]` 解码而来），返回**去掉所有 `key == "token"` 的键值对**后的查询串。
- 用 `urllib.parse.parse_qsl(qs, keep_blank_values=True)` 解析，保留其余键值对的**原顺序**，用 `urlencode` 重新编码（因此编码形式可能规范化，这是允许的）。
- 无 `token` 键时返回原串的等价重编码；输入为空串返回 `""`。

```python
@dataclass(frozen=True)
class AuthGuard:
    app: Any = None
    token: str | None = None
```
- **字段顺序固定为 `app` 在前**：Starlette 的 `add_middleware(cls, **opts)` 会以
  `cls(app=<被包裹应用>, **opts)` 构造，因此 `app.add_middleware(AuthGuard, token=T)`
  能直接工作。不要改动字段顺序，也不要在 `__init__` 里额外要求别的参数。
- `app is None` 仅供纯单元测试使用（只调 `is_authorized` / `expected_cookie`）；
  此时一旦需要透传，**必须抛 `RuntimeError`**，不得静默放行。
- `token` 为 `None` 或去空白后为空 → **守卫关闭**。
- 属性 `enabled -> bool`：`token` 非空即 `True`。
- 不变量：`AuthGuard` 一经构造即不可变；不持有任何全局状态。
- 方法 `expected_cookie() -> str`：返回 `session_value(self.token)`；守卫关闭时抛 `RuntimeError`。
- 方法 `is_authorized(self, *, cookie_value: str | None, query_token: str | None) -> bool`：
  - 守卫关闭 → 恒 `True`。
  - `query_token` 非空且与 `token` 常量时间相等 → `True`。
  - 否则 `cookie_value` 非空且与 `expected_cookie()` 常量时间相等 → `True`。
  - 否则 `False`。
  - **比较前双方都 `.encode("utf-8")` 成 bytes**（`hmac.compare_digest` 对非 ASCII 的 `str` 会抛 `TypeError`）。
- **ASGI 接口** `async def __call__(self, scope, receive, send) -> None`：
  作为纯 ASGI 中间件使用（**不要继承 `BaseHTTPMiddleware`**——它会缓冲响应并干扰
  `BackgroundTasks`/流式响应，本应用有后台线程投递语义）。透传分支一律
  `await self.app(scope, receive, send)`。

`AuthGuard.__call__` 的判定顺序，**必须严格按此顺序，每步都可被单独观测**：

1. `scope["type"] != "http"` → 直接透传（`websocket`/`lifespan` 不受影响）。
2. 守卫关闭 → 透传。
3. `scope["path"] == HEALTH_PATH` → 透传。
4. 解析凭据：
   - `query_token`：对 `scope["query_string"].decode("latin-1")` 用 `parse_qsl(keep_blank_values=True)`，取**第一个** `key == "token"` 的值；值为空串视为**未提供**。
   - `cookie_value`：取请求头 `cookie`（`scope["headers"]` 中 `name` 小写等于 `b"cookie"` 的**全部**值，用 `"; "` 连接），交给 `http.cookies.SimpleCookie`；**解析异常必须捕获并视为未提供**（畸形 Cookie 头不得导致 500）。
5. 已认证 **且** `query_token` 非空 **且** 请求方法为 `GET` **且** `Accept` 请求头（小写 `accept`）含 `"text/html"`：
   → **不调用内层应用**，直接返回 `303`，`Location: <path>` 或 `<path>?<strip_token_param(原查询串)>`（后者仅当去掉令牌后仍有内容），并带
   `Set-Cookie: nh_session=<expected_cookie()>; HttpOnly; Path=/; SameSite=Lax`。
   **不得**带 `Secure` 属性（部署为纯 HTTP，带上会让浏览器不回传，功能直接坏掉）。
6. 已认证（其余任何情形，含 `GET /api/v1/todos?token=...`）→ 透传，**不种 Cookie**。
7. 未认证 → 返回 `401`：
   - `Accept` 含 `text/html` → `Content-Type: text/html; charset=utf-8`，body 说明需要访问令牌。
   - 否则 → `Content-Type: application/json`，body 为 `{"detail": "unauthorized"}`。
   - 两种情况都**必须**写 `Content-Length`，且响应体不得回显请求携带的任何令牌。

### 3.3 内部实现

- 依赖：仅标准库 `hmac`、`hashlib`、`json`、`urllib.parse`、`http.cookies`、`dataclasses`。
  **禁止引入任何新依赖**，禁止 `import fastapi`/`starlette`。
- 响应用最小 ASGI 三元组直接构造（`await send({"type": "http.response.start", ...})`
  + `{"type": "http.response.body", "body": ...}`），不借助框架。
- 查询串解码用 `latin-1`（ASGI 规范规定 `query_string` 是原始字节的 latin-1 视图），
  再用 `parse_qsl` 按 UTF-8 解码百分号转义——不要自行改写这套语义。
- 参考既有脱敏设计：`src/notify_hub/redact.py` 的 `SENSITIVE_KEYS` 已含 `token`；
  本模块**不负责**脱敏，脱敏由 `logging_setup.SecretFilter` 与阶段 0 的
  `credential_values` 承担。
- 守卫关闭时只透传、不做任何解析，保证零开销路径。

### 3.4 验证方法

测试文件：`tests/test_auth.py`。运行命令：

```bash
.venv/bin/python -m pytest tests/test_auth.py -q
```

**单元测试**（直接测 `AuthGuard` / `session_value` / `strip_token_param`）：

| 验收点 | 可观察结果 |
|---|---|
| `session_value` 确定性 | 同输入两次调用结果相同；长度为 64；值不等于输入令牌 |
| `session_value("")` | 抛 `ValueError` |
| `strip_token_param` | `"a=1&token=X&b=2"` → `"a=1&b=2"`；`"token=X"` → `""`；`"a=1"` → `"a=1"` |
| 守卫关闭 | `AuthGuard(None).enabled is False`；`is_authorized(cookie_value=None, query_token=None) is True` |
| 守卫关闭时 `expected_cookie()` | 抛 `RuntimeError` |
| 正确查询令牌 | `is_authorized(cookie_value=None, query_token=T) is True` |
| 正确 Cookie | `is_authorized(cookie_value=AuthGuard(T).expected_cookie(), query_token=None) is True` |
| 错误 Cookie / 错误令牌 | 均为 `False`（两种错法各一条用例） |
| 两者都错 | `False` |

**单元测试（阶段 0 共享文件组）**——这一组覆盖架构师在阶段 0 改的 `config.py` / `main.py`，
语义以 §2 的两行表格为准：

| 验收点 | 可观察结果 |
|---|---|
| `server.auth_token` 键缺失 | `settings.auth_token is None` |
| `server.auth_token` 显式 `null` | `settings.auth_token is None` |
| 非字符串（如 `12345`） | `load_settings` 抛 `ConfigurationError` |
| 纯空白串 / 空串 | `load_settings` 抛 `ConfigurationError` |
| 值含首尾空白（如 `"  T  "`） | 返回 `"T"`（已 strip） |
| `credential_values` | 非空令牌的字面量出现在返回值里 |
| `build_uvicorn_kwargs` | `log_config is None`，且 `host == "127.0.0.1"` |

**这一组若失败，是架构师实现的问题**：不要改期望值去迁就代码，写进 `SPEC-GAPS`/`NOTES` 报告。

**模块级测试**（把 `AuthGuard` 装在**真实路由**前面，用 `fastapi.testclient.TestClient`）。

**装配方式（冻结，照抄，不要自己发明）**：**不要**用 `create_app`——它要到阶段 B 末尾
才装载本中间件，阶段 A 用它根本测不到守卫。自己拼一个只含真实路由的应用：

```python
from fastapi import FastAPI
from notify_hub.api import create_api_router
from notify_hub.web import create_web_router

def guarded_app(ctx, token):
    app = FastAPI()
    app.state.ctx = ctx                     # web 路由经 request.app.state.ctx 取上下文
    app.include_router(create_api_router(ctx))
    app.include_router(create_web_router(ctx))
    app.add_middleware(AuthGuard, token=token)
    return app
```

该应用**没有 lifespan**，不启动任何后台线程。`ctx` 直接用 conftest 的 `ctx` /
`make_context` 夹具；守卫令牌由 `add_middleware(token=...)` 传入，**与
`ctx.settings.auth_token` 无关**，两者不要混用。生产装配在 `create_app` 里的装载
由**阶段 D 的集成测试**验证（见 §4），不在本模块的测试范围内。

| 验收点 | 可观察结果 |
|---|---|
| 无凭据访问 `/todos` | `401`，`Content-Type` 前缀为 `text/html`（测试用 `Accept: text/html`） |
| 无凭据访问 `/api/v1/todos` | `401`，body 为 `{"detail":"unauthorized"}` |
| 无凭据访问 `POST /api/v1/messages` | `401`——**这是本变更的关键安全断言**：无鉴权的投递入口必须被挡住 |
| 无凭据访问 `/healthz` | 非 `401`（`200`），body 含 `"status"` |
| 浏览器形态首次请求 `GET /todos?token=T` | `303`；`Location` 为 `/todos`（**不含** `token`）；`Set-Cookie` 含 `nh_session`、`HttpOnly`、`Path=/`、`SameSite=Lax`，且**不含** `Secure` |
| 带 `?status=pending&token=T` | `303`；`Location` 保留 `status=pending` 且不含 `token` |
| 仅凭 Cookie 再访问 `/todos` | `200`，且响应 `Set-Cookie` 不再出现 |
| 非 GET 的 `POST /api/v1/messages?token=T` | 不被重定向（非 `303`）——按第 6 步透传 |
| 畸形 Cookie 头 | 不返回 `5xx`（返回 `401`） |

**必须覆盖的异常场景**（至少两条，均已列入上表）：
1. **畸形 `Cookie` 头**（如 `nh_session="`）：期望 `401`，**不得** 500。
2. **非空但错误的令牌**：期望 `401`，且响应体不含该错误令牌的任何回显。

**完成后必须成立的断言**：

```bash
.venv/bin/python -m pytest tests/test_auth.py -q          # 全绿
.venv/bin/python -m pytest -q                             # 全量仍全绿，无收集错误
```

## 4. 集成测试范围（阶段 D）

作用域 2，`tests/test_integration_public_access.py`，**不打桩**：

- **A -> 真实 uvicorn -> 进程日志**：以 `log_config=None` + `build_uvicorn_kwargs` 在子进程/
  线程内真实启动服务，发一次 `GET /todos?token=<T>`，断言该进程 stderr 输出中
  **不含** `<T>` 明文，且包含形如 `token=***` 的脱敏痕迹。
  这是 D8 修复的**唯一真凭据**——模块测试只能在单元层面断言 `uvicorn.access` 会传播到根 logger。
- **认证 -> 真实 DB -> 真实路由**：带 Cookie 访问 `/todos` 时列表内容与 `ctx.todos` 的
  真实数据一致；`POST /todos/{id}/done` 经 Cookie 认证后**真的改库**（完成后 `status` 变 `done`）。
- **凭据 -> 投递记录**：带 `?token=` 的请求若产生投递记录/事件，记录中不出现令牌明文。
- **守卫关闭路径**：不配置 `auth_token` 的真实应用仍可无凭据访问（D4 的契约）。

## 5. 门禁

1. 阶段 A：`tests/test_auth.py` 写完并确认 `RED-CONFIRMED`（现在必然是红的——`auth.py` 不存在）
2. 阶段 B：模块测试全绿；架构师补 `app.py` 装配
3. 阶段 D：集成测试全绿
4. 阶段 E：`subagent_review` 复跑全部套件 + 对照 `design.md`/`proposal.md` 查设计符合性
5. `security-scan` → commit → push
