# 让待办网页通过公网访问

## Why

`add-notify-hub` 的 Web 待办界面（M8）只在容器内可用：`notify-hub` 绑定 `127.0.0.1:8000`，
这是 `add-notify-hub/design.md` 决策 8 刻意设置的边界。实际使用中，用户需要**在手机上
随时查看未完成待办并勾选完成**——手机不在容器内网，现有形态用不了。

已经实测确认：本容器有**第二个已发布但闲置的端口** `宿主机 3090 → 容器 3091`
（当初为 mobile profile 预留，当前无进程监听），可直接复用，**无需新增端口映射、
无需重建容器**。

但一旦暴露到公网，就必须同时解决认证与凭据泄漏——`POST /api/v1/messages` 是**完全无鉴权**
的投递入口，暴露后任何人都能向系统灌消息、生成待办并触发飞书投递。

## What Changes

- **新增能力 `access-control`**：URL 令牌认证。除 `/healthz` 外所有路由要求有效凭据；
  浏览器首次带 `?token=` 访问后换取会话 Cookie，后续请求不再暴露令牌。
- **修复凭据泄漏（P1）**：`uvicorn.access` 日志器是 `propagate=False` 且自带 handler，
  根 logger 上的 `SecretFilter` 覆盖不到它。令牌以 `?token=` 出现时会被**明文写入日志**。
  本次变更让 uvicorn 日志回到根 logger，使脱敏生效。
- **部署接线**：容器内以独立转发进程把 `0.0.0.0:3091` 转到 `127.0.0.1:8000`。
  `notify-hub` 自身**保持绑定回环地址不变**——暴露由一个可审计、可一键关闭的独立进程承担。

明确**不做**：新增网页写操作（创建/删除/重开待办）、TLS、DNS、网关改动、限流。

## Capabilities

- `access-control`（新增）：待办网页与 API 的访问认证与凭据保护。

## Impact

- `src/notify_hub/config.py`：`Settings` 新增 `auth_token`；`credential_values` 纳入令牌
- `src/notify_hub/main.py`：uvicorn 不再接管日志配置
- `src/notify_hub/app.py`：装载认证中间件
- `src/notify_hub/auth.py`：新增
- `docs/deployment.md`：新增公网暴露与令牌轮换章节
- 运行时：`~/notify-hub-run/` 下的启动脚本与转发进程（仓库外）
