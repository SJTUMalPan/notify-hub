# 配置说明

notify-hub 的配置是一个 YAML 文件，默认路径为 `./config.yaml`，也可以用环境变量
`NOTIFY_HUB_CONFIG` 指向任意路径。仓库根的 `config.example.yaml` 是可运行的完整示例；
复制后用环境变量名替换凭据相关取值即可启动。

加载失败时服务直接拒绝启动，并打印一条**不含凭据值**的 `ConfigurationError`（文件名 +
键名 + 原因）。凭据缺失不是启动错误：对应渠道被标记为不可用，其余渠道照常工作。

## 配置概览

配置文件顶层是一个映射，只有下面这些键：

| 顶层键 | 是否必填 | 作用 |
|---|---|---|
| `server` | 否 | HTTP 服务监听地址、端口与日志级别 |
| `storage` | 是 | SQLite 数据库文件位置 |
| `rules` | 是 | 分类规则文件位置与热加载轮询间隔 |
| `reminders` | 否 | 超时提醒的扫描与提醒间隔 |
| `default_channel` | 否 | 规则未指定渠道时的默认渠道 id |
| `channels` | 否 | 通知渠道实例列表 |

完整示例（与 `config.example.yaml` 一致）：

```yaml
server:
  host: 127.0.0.1
  port: 8000
  log_level: INFO
storage:
  db_path: ./data/notify.db
rules:
  path: ./rules.yaml
  poll_interval_seconds: 5
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
  - id: feishu
    type: feishu
    enabled: true
    # 形态一：不用加签——只声明 url。
    # 启用加签时再补上 secret: NOTIFY_FEISHU_SECRET（url 与 secret 两个环境变量都必须设置）。
    # 不要写 `secret: null`：声明了 secret 却解析不出值，整个渠道会被判为不可用。
    params: { timeout: 10 }
    credentials: { url: NOTIFY_FEISHU_WEBHOOK_URL }
```

### server

HTTP 服务的监听配置。缺省时 `host` 为 `127.0.0.1`、`port` 为 `8000`、`log_level` 为
`INFO`。

### server.host

监听地址。**默认且推荐 `127.0.0.1`（仅回环）**；跨机器访问请在服务前加反向代理与鉴权，
不要把服务直接暴露到公网。详见 [deployment.md](deployment.md)。

### server.port

监听端口，必须是整数，缺省 `8000`。非整数会报
`server.port 必须是整数，实际为 ...`。

### server.log_level

日志级别字符串，缺省 `INFO`，例如 `DEBUG` / `INFO` / `WARNING`。

### storage

持久化配置段。

### storage.db_path

SQLite 数据库文件路径。**必填**；相对路径按配置文件所在目录解析。缺失时报
`缺少必填配置键: storage.db_path`。默认示例为 `./data/notify.db`。

### rules

分类规则文件配置段。

### rules.path

规则 YAML 文件路径。**必填**；相对路径按配置文件所在目录解析。缺失时报
`缺少必填配置键: rules.path`。规则文件的写法见 [rule-authoring.md](rule-authoring.md)。

### rules.poll_interval_seconds

规则文件 mtime 轮询间隔（秒），缺省 `5`。必须是数字。文件变更后最多等待该时长即热加载；
解析失败时保留上一份可用规则集。

### reminders

超时提醒参数段。三个参数共同决定「多久扫一次、首次提醒多晚、之后多久提醒一次」。

### reminders.scan_interval_seconds

提醒调度线程的扫描间隔（秒），缺省 `60`。必须是数字。

### reminders.first_reminder_after_seconds

待办创建后首次提醒的延迟（秒），缺省 `1800`。必须是数字。

### reminders.reminder_interval_seconds

首次提醒之后每次提醒的间隔（秒），缺省 `3600`。必须是数字。

**三者关系与非法组合**：`scan_interval_seconds` 必须不大于
`first_reminder_after_seconds`，也必须不大于 `reminder_interval_seconds`；
相等是合法的。违反时报错并拒绝启动：

- `reminders.reminder_interval_seconds(...) 小于 reminders.scan_interval_seconds(...)`
- `reminders.first_reminder_after_seconds(...) 小于 reminders.scan_interval_seconds(...)`

### default_channel

规则与渠道都没有指定时使用的渠道 `id`。缺省为 `null`（没有默认渠道）。若取值不在
`channels` 列表的 `id` 中，报
`default_channel='...' 不在 channels 列表中: [...]`。

### channels

渠道实例列表，每个元素对应一个已构造的适配器。每一项的键见下。

### channels[].id

渠道实例 id，**必填且全局唯一**；重复时报 `channels 中存在重复的渠道 id: ...`。它同时是
投递记录、规则 `channel` 字段与 `default_channel` 引用的名字。

### channels[].type

适配器类型，**必填**，对应已注册的工厂名。内置类型为 `webhook`、`email` 与 `feishu`
（飞书自定义机器人，见下）；未知类型只会让该渠道不可用（记入不可用原因），不会让服务
启动失败。如何新增类型见 [adapter-guide.md](adapter-guide.md)。

### channels[].enabled

是否启用该渠道，缺省 `true`。为 `false` 时渠道被跳过并记录原因 `渠道未启用`。

### channels[].params

适配器的非敏感参数映射，缺省空映射。不同 `type` 识别不同键。

### channels[].params.url_env

webhook 渠道使用的参数：**URL 凭据对应的环境变量名**。webhook 适配器实际读取
`credentials.url` 解析出的值，`url_env` 用于让配置自描述。

### channels[].params.field_map

webhook 渠道使用的参数：把消息字段名映射为 webhook 载荷里的字段名，缺省空映射。

### channels[].params.headers

webhook 渠道使用的参数：附加请求头映射，缺省空映射。**不要在这里写凭据字面量**，
只写非敏感头。

### channels[].params.timeout

feishu 渠道使用的参数：单次请求超时秒数，缺省 `10`。飞书的请求体是**嵌套**结构
（`{"msg_type": "text", "content": {"text": "..."}}`），通用 `webhook` 适配器只发平铺
JSON、`field_map` 只做顶层键改名，表达不了它，因此飞书单列一个类型。

### channels[].params.host

email 渠道使用的参数：SMTP 服务器主机名。

### channels[].params.port

email 渠道使用的参数：SMTP 端口，整数。

### channels[].params.use_tls

email 渠道使用的参数：是否使用 STARTTLS，布尔值，缺省 `false`。

### channels[].params.sender

email 渠道使用的参数：发件人地址，**必填**；缺失时报
`email 渠道 <id> 缺少必填参数: sender`。

### channels[].params.recipients

email 渠道使用的参数：收件人地址列表，**必填且非空**；缺失时报
`email 渠道 <id> 缺少非空参数: recipients`。

### channels[].credentials

凭据映射。**这里写的值一律是环境变量名，不是凭据本身**；服务启动时读取对应环境变量的值。
示例：`credentials: { url: NOTIFY_WEBHOOK_URL }`。环境变量未设置或为空时，该渠道的
`credentials_complete` 为 `false`，渠道不可用但服务照常启动。

### channels[].credentials.url

webhook 渠道的凭据键：webhook 完整 URL，从对应环境变量读取。

### channels[].credentials.password

email 渠道的凭据键：SMTP 密码，从对应环境变量读取。

### feishu 渠道

`type: feishu` 对应飞书（Feishu / Lark）自定义机器人适配器。它的凭据映射有两个键，
**值同样一律写环境变量名**：

- `url`：**必填**，飞书机器人 webhook 完整 URL，从 `NOTIFY_FEISHU_WEBHOOK_URL` 这类
  环境变量读取；token 在 URL 路径末段。
- `secret`：**可选**；**只有启用加签时才声明**，从 `NOTIFY_FEISHU_SECRET` 这类环境变量
  读取。提供即启用加签（`timestamp` 与 `sign` 放在 JSON body 顶层，单位是秒）。

**不用加签时不要声明 `secret` 这个键**，更不要写 `secret: null`：只要声明了凭据却解析不出
值，整个渠道的 `credentials_complete` 就是 `false`，渠道被判为不可用（这正是「你本来打算
加签但凭据没配好」应有的语义）。非敏感的 `params.timeout` 见上。

## 环境变量

| 环境变量 | 作用 |
|---|---|
| `NOTIFY_HUB_CONFIG` | 指向配置文件路径；未设置时用 `./config.yaml` |
| `NOTIFY_HUB_ENDPOINT` | CLI 默认服务端地址；未设置时用 `http://127.0.0.1:8000` |
| 由 `channels[].credentials` 引用的变量 | 渠道凭据值本身，仅存在于运行环境 |

任何日志、错误消息与投递记录都会对凭据值做脱敏；配置错误消息中只出现键名，不出现值。
