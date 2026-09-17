## Purpose

为服务器上的各个进程提供统一、语言无关的消息投递入口：进程只负责把消息发出来，不必了解分类规则与通知渠道，也不需要在每个业务进程里重复对接通知 App。

## ADDED Requirements

### Requirement: 单条消息投递接口

系统 SHALL 提供 HTTP 接口 `POST /api/v1/messages`，接受 JSON 请求体并返回投递受理结果。请求体 SHALL 支持以下字段：

- `source`（必填，字符串）：消息来源标识，用于区分不同进程，例如 `backup-job`、`web-01`。
- `title`（必填，字符串）：消息标题。
- `body`（可选，字符串）：消息正文。
- `level`（可选，字符串）：消息级别，取值 `info` / `warning` / `error`，缺省为 `info`。
- `need_ack`（可选，布尔）：是否要求由人处理并确认，缺省为 `false`。
- `dedup_key`（可选，字符串）：调用方自定义的去重键，随消息原样保存。
- `occurred_at`（可选，ISO 8601 时间戳）：消息在来源侧的发生时间，缺省取服务接收时间。
- `meta`（可选，对象）：调用方附加的任意结构化信息，原样保存。

投递成功时系统 SHALL 返回 HTTP 202 与消息标识 `message_id`。请求体缺少必填字段、`level` 取值非法或字段类型不匹配时，系统 SHALL 返回 HTTP 422 并指明出错字段，且 MUST NOT 创建消息记录。

#### Scenario: 成功投递一条带确认要求的消息
- **WHEN** 进程向 `POST /api/v1/messages` 发送包含 `source`、`title`、`level=error`、`need_ack=true` 的合法 JSON
- **THEN** 系统返回 HTTP 202 和该消息的 `message_id`
- **AND** 该消息被保存，且因 `need_ack=true` 进入待办跟踪

#### Scenario: 缺少必填字段被拒绝
- **WHEN** 请求体只包含 `title`，缺少 `source`
- **THEN** 系统返回 HTTP 422，响应中指明 `source` 缺失
- **AND** 系统中不产生任何消息记录

#### Scenario: 级别取值非法被拒绝
- **WHEN** 请求体的 `level` 取值为 `critical`
- **THEN** 系统返回 HTTP 422，响应中指明 `level` 的合法取值范围
- **AND** 系统中不产生任何消息记录

#### Scenario: 可选字段缺省时的默认语义
- **WHEN** 请求体只包含 `source` 与 `title`
- **THEN** 系统按 `level=info`、`need_ack=false`、`occurred_at=` 服务接收时间处理该消息
- **AND** 该消息不进入待办跟踪

### Requirement: 批量投递

系统 SHALL 提供 HTTP 接口 `POST /api/v1/messages/batch`，接受一个消息数组并逐条独立处理。响应 SHALL 分别返回每条消息的受理状态：被接受的消息给出其 `message_id`，被拒绝的消息给出其在数组中的下标与出错原因。批量请求中单条消息非法 MUST NOT 导致同批次其它合法消息被丢弃。

#### Scenario: 同批次中部分消息非法
- **WHEN** 一次批量请求包含 3 条消息，其中第 2 条缺少 `source`
- **THEN** 系统返回 HTTP 207，第 1、3 条带各自的 `message_id` 标记为已接受，第 2 条标记为被拒绝并给出下标与原因
- **AND** 第 1、3 条消息正常进入后续分类与投递流程

### Requirement: 命令行投递入口

系统 SHALL 提供命令行工具 `notify`，至少支持以参数方式投递单条消息，并支持 `--source`、`--title`、`--body`、`--level`、`--need-ack` 选项。该工具 MUST 通过 HTTP 接口投递，不得绕过接入层的校验与记录逻辑。被服务端拒绝时，工具 SHALL 以非零退出码结束并把错误原因写入标准错误；投递成功时 SHALL 以退出码 0 结束并把 `message_id` 写入标准输出。

#### Scenario: 脚本内成功投递
- **WHEN** 执行 `notify --source backup --title "备份完成" --level info`
- **THEN** 命令退出码为 0，标准输出包含服务端返回的 `message_id`

#### Scenario: 服务端拒绝时脚本能感知失败
- **WHEN** 执行 `notify --title "无来源"`，缺少 `source`
- **THEN** 命令以非零退出码结束，标准错误包含服务端给出的拒绝原因

#### Scenario: 服务端不可达
- **WHEN** 服务端未在监听，脚本执行 `notify --source backup --title "测试"`
- **THEN** 命令以非零退出码结束，并在标准错误说明无法连接服务端

### Requirement: 服务健康检查

系统 SHALL 提供 HTTP 接口 `GET /healthz`，无需认证即可访问，返回服务可用状态与当前时间。该接口 MUST NOT 依赖数据库以外的外部服务可用性，以确保在通知渠道故障时仍能区分「服务本身挂了」与「渠道发不出去」。

#### Scenario: 服务正常时探活
- **WHEN** 服务已启动并完成初始化，客户端请求 `GET /healthz`
- **THEN** 系统返回 HTTP 200 及可用状态标识

#### Scenario: 通知渠道故障不影响探活
- **WHEN** 所有通知渠道均不可用，客户端请求 `GET /healthz`
- **THEN** 系统仍返回 HTTP 200

### Requirement: 凭据隔离

接入层 MUST NOT 在响应体、错误信息或日志中回显任何通知渠道的凭据（Webhook 地址中的 token、SMTP 密码等）。接入层错误信息 SHALL 只包含请求字段级别的校验结论。

#### Scenario: 校验失败时不泄漏凭据
- **WHEN** 请求体校验失败并触发错误响应，同时服务端配置了含 token 的 Webhook 地址
- **THEN** 错误响应与日志中不出现该 token 的任何片段
