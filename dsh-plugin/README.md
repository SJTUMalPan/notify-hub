# dsh-ask-timeout-notify

**DSH 宿主插件**：模型通过 `ask_user_question` 提问后，如果超过阈值（默认 **60 分钟**）
仍无人回答，就往 [notify-hub](../README.md) 投递一条 `need_ack` 消息，由 notify-hub
落成**待办**并按渠道推送（飞书 / webhook / email）。

典型场景：模型问了个选择题就去干别的了，你在开会/出门，DSH 的会话就这么挂着。
本插件让你在飞书里收到一条"DSH 在等你回答"的待办，而不是事后才发现它等了两小时。

## 它做什么、不做什么

| 做 | 不做 |
|---|---|
| 在 `user-questions/request` 这条 waterfall 链首起一个定时器 | 不替用户作答，也不改变 DSH "一直等"的语义 |
| 到点往 notify-hub 投一条待办（带 `dedup_key` 去重） | 不改 notify-hub 一行代码、不需要给它加规则 |
| 用户答了/请求取消/出错了就销毁定时器 | 不影响问答主流程（投递失败只记日志） |

**为什么不用改 notify-hub**：`POST /api/v1/messages` 本来就接受调用方声明
`need_ack: true`——分类器按 `declared_need_ack or rule.need_ack` 裁决并记
`ack_reason=caller_declared`，待办因此自动产生；`dedup_key` 让同一问题重复超时不会刷出
多条（服务端按 `(source, dedup_key)` 复用未完成的待办）。所以本插件只是一个普通的消息
生产者，跟 CI 脚本、监控告警走同一个入口。

## 什么时候会触发、什么时候不会

这一点是在真实 DSH 进程里实测确认的（不是推测），值得先讲清楚：

| 场景 | 链上的表现 | 本插件的行为 |
|---|---|---|
| 前端开着、你在忙，一直没点 | 回答者接手，`next()` **一直挂着** | 到阈值投一条待办 ✅ |
| 前端关掉 / 没有回答者 | 链上**立刻**以 `NO_PROVIDER` 拒绝 | `finally` 销毁定时器，**不发**（这条问答已经失败了，不是「在等你」）❌ |
| 前端开着，但你在阈值内答了 | `next()` 正常返回 | 定时器销毁，**不发** ✅ |
| 会话被取消 / 处理中断 | `next()` 抛错 | 定时器销毁，**不发** ✅ |

也就是说：**它管的是「人还没回」，不是「人不在」**。浏览器关掉导致无人接手时，DSH 会
立刻判定这条问答失败，那种情况不会有待办——本插件的价值恰好在前端还开着、会话真的挂着
等你的那段时间。

## 前置条件

1. DSH 已安装并能跑 `dsh web`（profile 名为 `web`）。
2. notify-hub 在跑，且你知道它的地址与访问令牌：
   - 地址：默认 `http://127.0.0.1:8000`；
   - 令牌：`config.yaml` 里 `server.auth_token` 的值（未配置令牌 = 服务不鉴权，此时
     本插件也要给一个非空令牌值，随便填即可——见下面「配置」）。

## 安装

### 本机开发（符号链接，改完即生效，不用重装）

```bash
dsh plugin --profile web add "$PWD/dsh-plugin"   # 在仓库根目录执行
```

> 裸路径与 `file:` 是 **pnpm link 语义（符号链接）**：源码改了不用重装，但**换机器就断**，
> 因为目标机器上这个路径不存在。跨机器请用下面的 git 形式。

### 别的机器 / 稳定安装（推荐，锁 commit）

```bash
dsh plugin --profile web add "git+https://github.com/SJTUMalPan/notify-hub#<sha>&path:/dsh-plugin"
```

把 `<sha>` 换成 `git -C <notify-hub 仓库> rev-parse HEAD` 的输出（或任意分支/标签名）。
**不锁就会装到最新代码**，插件与 notify-hub 一起演进时容易错配。想跟着分支走：

```bash
dsh plugin --profile web add "git+https://github.com/SJTUMalPan/notify-hub#main&path:/dsh-plugin"
```

### 安装之后

`dsh plugin add` 会做两件事：把包装进 `~/.dsh/profiles/web/`，并把包名追加进该 profile
`package.json` 的 `dsh.profile.bundles`（因为本包声明了 `dsh.bundle.patch`）。所以
**不需要手改 profile 的任何文件**。然后：

```bash
# 重启 dsh web 才生效（profile 的插件组合变更只对新进程生效）
```

卸载：

```bash
dsh plugin --profile web remove dsh-ask-timeout-notify
```

## 配置

两种方式，**环境变量优先**（同一份插件装在多台机器上时，只有环境变量能表达机器差异）。

| 环境变量 | `cordis.patch.yml` 里的键 | 默认 | 说明 |
|---|---|---|---|
| `DSH_ASK_TIMEOUT_MINUTES` | `timeoutMinutes` | `60` | 等待多少分钟算超时（正整数，1–10080） |
| `DSH_NOTIFY_HUB_ENDPOINT` | `endpoint` | 无（必填） | notify-hub 地址，如 `http://127.0.0.1:8000` |
| `DSH_NOTIFY_HUB_TOKEN` | `token` | 无（必填） | 访问令牌，放查询串 `?token=…`，**不写日志** |
| `DSH_ASK_NOTIFY_ENABLED` | `enabled` | `true` | 设 `false` 可整体关闭 |
| `DSH_ASK_NOTIFY_REQUEST_TIMEOUT_MS` | `requestTimeoutMs` | `5000` | 单次 HTTP 投递超时（毫秒） |
| —（只认 config） | `source` | `dsh` | 消息来源标识，notify-hub 规则匹配用 |

环境变量应该跟 `dsh web` 的启动方式放在一起（例如启动脚本里 `export`），不要写进仓库。

改 `cordis.patch.yml` 里的 config（`~/.dsh/profiles/web/node_modules/dsh-ask-timeout-notify/cordis.patch.yml`
不会生效——它属于包本身；要覆盖请改 profile 自己的 `~/.dsh/profiles/web/cordis.patch.yml`）：

```yaml
# ~/.dsh/profiles/web/cordis.patch.yml
- id: ask-timeout-notify
  config:
    timeoutMinutes: 15
```

## 怎么判断它在工作

- **没配置端点/令牌时**：插件照常装载，但启动日志会有一条 warn 说明原因，超时后不会产生
  待办。这是刻意设计——补上环境变量重启即可生效，而不是静默缺席。
- **投递成功**：DSH 日志里出现
  `ask-timeout-notify: 已投递待办 message_id=… todo_id=… dedup=ask:<会话>:<问题 id>`。
- **投递失败**：日志里 `ask-timeout-notify: 投递失败（…）；不影响等待中的问答`，
  同时 DSH 里的问答**照常继续等**——通知失败永远不影响主流程。

DSH 的 `ctx.logger.info` 不一定落到 stdout（取决于 profile 的日志配置），所以最可靠的
旁证是 notify-hub 侧：`/todos` 页面出现一条 `source=dsh` 的待办。

## 排障

| 现象 | 原因与处理 |
|---|---|
| 待办没出现，日志也没有 `已投递` | 端点或令牌没配。用 `curl -sS "$ENDPOINT/healthz"` 先确认服务活着（`/healthz` 免鉴权） |
| 日志报 `401` | 令牌不对。令牌取自 notify-hub `config.yaml` 的 `server.auth_token` |
| 日志报 `请求超时` | notify-hub 没起或地址不可达；若它挂在公网隧道后面，确认隧道在线 |
| 有 `apply` 但从不触发 | 定时器只在**有提问**时才会起。让模型真的调一次 `ask_user_question` 试试 |
| 收到多条待办 | 不该发生——`dedup_key` 保证同一问题只建一条。若同一条待办被反复提醒，那是 notify-hub 的 `reminders` 在按日提醒，不是本插件 |
| 想立刻验证 | 把 `DSH_ASK_TIMEOUT_MINUTES` 设为 `1`，重启 `dsh web`，让模型问一个问题，然后**不要回答**，一分钟内应收到待办；验证完改回 60 |
| 启动报 `Cannot find module '…/dsh-plugin/index.json'` | 你是在 `--patch` 里写了**目录**路径。loader 对目录路径按 `cordis:include` 目录入口处理（找 `index.json`），不会读 `package.json` 的 `main`。`--patch` 里要写入口文件：`.../dsh-plugin/lib/index.js`；只有通过 `dsh plugin add` 安装（按包名装载）时才用目录/包名 |

## 开发

```bash
cd dsh-plugin
npm test          # = node --test test/*.test.mjs
```

测试覆盖三块：配置解析（含环境变量/ config 优先级与非法值回退）、请求体构造
（`dedup_key` 稳定性、字段只含契约声明的键）、以及**真实 HTTP 投递**——测试里起一个真的
`node:http` 服务端，断言发出去的请求行（含 `?token=` 转义）、请求头与请求体，而不是断言
我们自己的对象。超时、401、连不上、非 JSON 响应都有对应用例。

零依赖、纯 ESM JavaScript，**没有构建步骤**（这是刻意的：git 安装的包若要跑 `prepare`
脚本，pnpm 需要在 `pnpm-workspace.yaml` 的 `allowBuilds` 里逐个放行，一个百行插件不值得
背这个负担）。

## 文件

| 文件 | 作用 |
|---|---|
| `package.json` | 声明 `main` 与 `dsh.bundle.patch`（后者是本包能成为 profile 层级的关键） |
| `cordis.patch.yml` | 两行 `insert`，把本插件挂进 profile 插件树 |
| `lib/index.js` | Cordis 接线：事件监听、定时器、effect 生命周期 |
| `lib/notify.mjs` | 纯函数层：配置解析、请求体构造、HTTP 投递（可独立测试） |
| `test/notify.test.mjs` | 单测 |
