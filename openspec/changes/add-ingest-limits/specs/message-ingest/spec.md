## ADDED Requirements

### Requirement: 接入请求体上限

系统 SHALL 对消息接入端点的请求体大小与批量条数设上限，缺省分别为 `1 MiB`
（`server.max_body_bytes`）与 `500` 条（`server.max_batch_items`）。上限 SHALL 可配置，
且配置值 MUST 是正整数：`0`、负数与非整数一律在加载配置时报错，服务 MUST NOT 启动。

请求体超过字节上限、或批量数组条数超过条数上限时，系统 SHALL 返回 HTTP 413，
并 MUST NOT 写入任何消息记录、MUST NOT 触发任何投递。计字节 MUST 以**实际读取到的
请求体字节**为准，因此不携带 `Content-Length` 的分块请求同样受限。

#### Scenario: 单条消息请求体超限
- **WHEN** `POST /api/v1/messages` 的请求体字节数超过 `server.max_body_bytes`
- **THEN** 系统返回 HTTP 413
- **AND** 不产生任何消息记录与投递

#### Scenario: 分块传输的请求体超限
- **WHEN** 请求体以分块方式发送、不带 `Content-Length`，且实际字节数超过上限
- **THEN** 系统返回 HTTP 413
- **AND** 不产生任何消息记录

#### Scenario: 请求体恰好等于上限
- **WHEN** 请求体字节数恰好等于 `server.max_body_bytes` 且内容合法
- **THEN** 系统按正常路径受理，返回 HTTP 202

#### Scenario: 批量条数超限
- **WHEN** `POST /api/v1/messages/batch` 的数组长度超过 `server.max_batch_items`
- **THEN** 系统返回 HTTP 413 并在说明中点出上限与本次条数
- **AND** 该批次中**没有任何一条**消息被受理（不返回 207、不部分受理）

#### Scenario: 批量条数恰好等于上限
- **WHEN** 数组长度恰好等于 `server.max_batch_items` 且每条都合法
- **THEN** 系统返回 HTTP 207 且全部标记为已接受

#### Scenario: 上限配置非法
- **WHEN** `server.max_body_bytes` 或 `server.max_batch_items` 为 `0`、负数或非整数
- **THEN** 加载配置时抛出配置错误，服务不启动
