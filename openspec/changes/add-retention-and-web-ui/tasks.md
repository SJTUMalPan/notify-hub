# 任务：add-retention-and-web-ui

> 模块规格见 `architecture.md`。派发任务书只引用章节号，不重述规格。
> 阶段 0 与装配由架构师完成，不计入派发。
> **顺序是串行的**：M-A 完成 → 架构师补 `context.py` → M-B 开始。

## 0. 共享文件（架构师）

- [ ] 0.1 `config.py`：新增 `RetentionSettings`（`days: int = 30`）与 `Settings.retention`
- [ ] 0.2 `config.py`：解析 `retention.days`；缺省 30；非整数或负数报 `ConfigurationError`
- [ ] 0.3 手工验证 0.1–0.2 不破坏现有 397 个测试
- [ ] 0.4 **M-A 落地后**：`context.py` 装配 `RetentionService`，加进 `AppContext`，传给 `ReminderScheduler`
- [ ] 0.5 `tests/conftest.py`：`make_context` 适配调度器新增的构造参数
- [ ] 0.6 `config.example.yaml` 增 `retention` 段并注释
- [ ] 0.7 `docs/configuration.md`：保留期参数；`docs/deployment.md`：两个窗口的区别、备份建议、回滚

## 1. 数据保留与自动回收（M-A，规格 §4）

- [ ] 1.1 阶段 A：`tests/test_retention.py` 按 §4.5 写全（单元 + 行为 + 调度器集成 + 异常场景），确认 `RED-CONFIRMED`
- [ ] 1.2 阶段 B：`src/notify_hub/services/retention.py` 实现到测试全绿，不得改测试
- [ ] 1.3 阶段 B：`services/scheduler.py` 在**结算完成之后**挂回收，异常隔离，不改既有返回语义
- [ ] 1.4 全量 `pytest -q` 仍全绿、无收集错误

## 2. 待办网页改版（M-B，规格 §5）

- [ ] 2.1 阶段 A：`tests/test_web_recent_completed.py` 按 §5.4 写全，确认 `RED-CONFIRMED`
- [ ] 2.2 阶段 B：`web/routes.py` 增 `recent_completed` 上下文与 `POST /todos/purge-completed`
- [ ] 2.3 阶段 B：`todos_list.html` 增「最近完成」栏目与清空表单（含 `confirm`）
- [ ] 2.4 阶段 B：`base.html` 重写内联 CSS（手机优先、零外部资源）
- [ ] 2.5 **既有 `tests/test_web.py`（635 行）必须仍全绿**——主表七列与三个筛选链接不得改动
- [ ] 2.6 全量 `pytest -q` 仍全绿

## 3. 集成（规格 §6）

- [ ] 3.1 阶段 D：调度器 → 真实 DB → 网页，端到端验证回收后页面不再出现该记录
- [ ] 3.2 阶段 D：删待办后消息详情页的「投递记录」**行数不变**（解绑而非删除）
- [ ] 3.3 阶段 D：跨越保留期后 pending 待办与其消息仍在
- [ ] 3.4 阶段 D：重复触发回收的幂等性

## 4. 门禁

- [ ] 4.1 阶段 E：`subagent_review` 复跑全部套件 + 对照 `design.md`/`proposal.md` 查设计符合性
- [ ] 4.2 `security-scan`
- [ ] 4.3 commit（一模块一 commit，集成测试单独一 commit）
- [ ] 4.4 push 到 `feat/add-retention-and-web-ui`，不开 PR
