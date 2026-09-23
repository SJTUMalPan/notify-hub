## ADDED Requirements

### Requirement: 全路由凭据校验

系统 SHALL 对除 `/healthz` 之外的**每一个** HTTP 路由校验访问凭据，未通过者 SHALL 收到
`401`。有效凭据有两种形式：URL 查询参数 `token`，或会话 Cookie。当未配置访问令牌时，
系统 SHALL 不启用校验。

#### Scenario: 无凭据的页面请求被拒绝
- **WHEN** 未携带任何凭据请求 `/todos`
- **THEN** 返回 `401`，且不返回任何待办数据

#### Scenario: 无鉴权的投递入口同样被拦住
- **WHEN** 未携带任何凭据向 `/api/v1/messages` 发送 `POST`
- **THEN** 返回 `401`，且不创建消息、不创建待办、不触发任何投递

#### Scenario: 探活端点免校验
- **WHEN** 未携带任何凭据请求 `/healthz`
- **THEN** 返回 `200` 与形如 `{"status":"ok",...}` 的响应体

#### Scenario: 未配置令牌时保持原有行为
- **WHEN** 配置中未设置访问令牌，且请求未携带任何凭据
- **THEN** 请求按原有的无鉴权行为被正常处理

### Requirement: 令牌换取会话 Cookie

携带有效 `token` 查询参数的浏览器页面请求 SHALL 被重定向到**去掉该参数**的同一地址，
并 SHALL 同时下发会话 Cookie。该 Cookie MUST 具备 `HttpOnly`、`SameSite=Lax`、`Path=/`；
其值 MUST NOT 等于令牌本身。

#### Scenario: 首次访问后地址栏不再含令牌
- **WHEN** 浏览器以 `Accept: text/html` 请求 `/todos?token=<有效令牌>`
- **THEN** 返回 `303`，`Location` 指向 `/todos` 且不包含 `token` 参数

#### Scenario: 查询串中的其他参数被保留
- **WHEN** 浏览器请求 `/todos?status=pending&token=<有效令牌>`
- **THEN** `Location` 为 `/todos?status=pending`

#### Scenario: 后续请求仅凭 Cookie 即可访问
- **WHEN** 携带首次换取的会话 Cookie 请求 `/todos`，且不带任何查询参数
- **THEN** 返回 `200` 与真实待办列表

#### Scenario: Cookie 值不泄露令牌
- **WHEN** 检查下发的会话 Cookie 值
- **THEN** 该值不等于令牌明文，且无法由它还原出令牌

### Requirement: 未认证响应的形态

未通过校验时，系统 SHALL 返回 `401`；面向浏览器的请求 SHALL 得到一段说明性的 HTML，
其余请求 SHALL 得到 JSON。响应体 MUST NOT 回显请求中携带的任何凭据。

#### Scenario: 浏览器与接口得到不同形态
- **WHEN** 分别以 `Accept: text/html` 与 `Accept: application/json` 发起无凭据请求
- **THEN** 前者响应体为 HTML，后者为 `{"detail":"unauthorized"}` 形态的 JSON

#### Scenario: 错误凭据不被回显
- **WHEN** 携带一个错误但非空的令牌请求任意受保护路由
- **THEN** 返回 `401`，且响应体中出现该错误令牌的次数为零

### Requirement: 凭据不写入日志

系统 MUST NOT 将访问令牌的明文写入任何日志，**包括 HTTP 访问日志**。

#### Scenario: 携带令牌的请求不留明文
- **WHEN** 以 `?token=<有效令牌>` 发起请求，随后检查服务进程的全部日志输出
- **THEN** 其中出现该令牌明文的次数为零

### Requirement: 令牌轮换立即生效

令牌 SHALL 可由配置变更并重启完成轮换；轮换后，此前签发的会话 Cookie MUST 立即失效。

#### Scenario: 轮换后旧 Cookie 失效
- **WHEN** 令牌由 A 改为 B 并重启，随后携带基于 A 的旧会话 Cookie 发起请求
- **THEN** 返回 `401`，且以令牌 B 重新访问可正常通过

### Requirement: 应用自身的监听边界不扩大

`notify-hub` 进程 SHALL 继续绑定回环地址；对外暴露 MUST 由独立于应用的转发进程承担，
使暴露面可以被单独停止与审计。

#### Scenario: 服务仍只监听回环
- **WHEN** 检查服务启动所使用的监听主机配置
- **THEN** 其为 `127.0.0.1`，而非 `0.0.0.0`
