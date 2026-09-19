# 任务：add-public-access

> 模块规格见 `architecture.md`。派发任务书只引用章节号，不重述规格。
> 阶段 0 由架构师完成，不计入派发。

## 0. 共享文件（架构师，fan-out 前完成）

- [x] 0.1 `config.py`：`Settings` 增 `auth_token: str | None = None`
- [x] 0.2 `config.py`：`load_settings` 解析 `server.auth_token`，缺省 `None`；非字符串或纯空白报 `ConfigurationError`
- [x] 0.3 `config.py`：`credential_values` 把非空令牌并入返回值（正文中的令牌也随之脱敏）
- [x] 0.4 `main.py`：抽出 `build_uvicorn_kwargs(settings)`，返回 `log_config=None`，使 `uvicorn.*` 日志回到根 logger
- [x] 0.5 手工验证 0.1–0.4 不破坏现有 314 个测试

## 1. 认证模块 M-A（规格 §3）

- [x] 1.1 阶段 A：`tests/test_auth.py` 按 §3.4 写全，确认 `RED-CONFIRMED`
- [x] 1.2 阶段 B：`src/notify_hub/auth.py` 实现到测试全绿，不得改测试
- [x] 1.3 阶段 B 末尾（架构师）：`app.py` 装载 `AuthGuard` 中间件
- [x] 1.4 全量 `pytest -q` 仍全绿、无收集错误

## 2. 集成（规格 §4）

- [x] 2.1 阶段 D：`tests/test_integration_public_access.py` 覆盖真实 uvicorn 进程日志无令牌明文
- [x] 2.2 阶段 D：覆盖「认证 → 真实 DB → 真实路由」的写路径（`POST /todos/{id}/done` 真的改库）
- [x] 2.3 阶段 D：覆盖守卫关闭路径（未配置令牌时无凭据可访问）

## 3. 部署接线（架构师，运维）

- [x] 3.1 生成令牌写入 `~/notify-hub-run/config.yaml` 的 `server.auth_token`（仓库外）
- [x] 3.2 重启 `notify-hub`，确认 `/healthz` 正常
- [x] 3.3 启动脚本：未配置令牌时**拒绝**启动转发进程；起 `0.0.0.0:3091 → 127.0.0.1:8000`
- [ ] 3.4 实测 `http://47.109.102.36:3090/?token=<令牌>` 可看列表并勾选完成
      —— **待用户从外部网络（手机 4G）确认**。容器内只能做 hairpin 验证，
      它会绕过云安全组的入方向检查，**不能证明外网可达**；「勾选完成」也尚未实际点过。
- [x] 3.5 `docs/deployment.md` 增补公网暴露、令牌轮换、与 mobile profile 的端口冲突、回滚步骤
- [x] 3.6 确认日志中令牌明文出现次数为 0

## 4. 门禁

- [x] 4.1 阶段 E：`subagent_review` 复跑全部套件 + 对照 `design.md`/`proposal.md` 查设计符合性
- [ ] 4.2 `security-scan`（本轮未执行）
- [x] 4.3 commit（一模块一 commit，集成测试单独一 commit）
- [x] 4.4 push 到 `feat/add-public-access`，不开 PR
