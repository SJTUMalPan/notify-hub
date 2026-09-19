## 1. 配置（M1）

- [ ] 1.1 `ReminderSettings` 改为 `{ at: str, timezone: str, scan_interval_seconds: float }`，
      移除 `first_reminder_after_seconds` 与 `reminder_interval_seconds`；默认值
      `at="21:00"`、`timezone="Asia/Shanghai"`、`scan_interval_seconds=60.0`
- [ ] 1.2 校验：`at` 必须匹配 `HH:MM` 且为合法时刻；`timezone` 必须是 `zoneinfo.ZoneInfo` 能加载的
      IANA 名称；`scan_interval_seconds` 必须 > 0 且 ≤ 3600。错误消息指明键名与非法值
- [ ] 1.3 旧键 `first_reminder_after_seconds` / `reminder_interval_seconds` 出现即报错（不静默忽略），
      错误消息指明「该键已被移除，请改用 at / timezone」
- [ ] 1.4 新增 `ReminderSettings.trigger_time()` 之类的纯函数：把 `at` 解析为 `datetime.time`，
      并暴露 `ZoneInfo`，供调度器换算本地时刻

## 2. 数据模型（M2）

- [ ] 2.1 新增 `DigestRun` 表（`digest_runs`）：`id` 主键、
      `local_date`（本地日期，唯一索引）、`checked_at`、`fired_at`（可空）、
      `todo_count`（整数，默认 0）、`delivered`（布尔，默认 false）、`attempts`（整数，默认 0）、
      `last_error`（可空字符串，需已脱敏）
- [ ] 2.2 `init_schema()` 幂等建出该表；重复执行不报错、旧数据不受影响

## 3. 汇总调度（M6）

- [ ] 3.1 重写 `ReminderScheduler.run_once()`：计算本地日期与本地时刻；
      若本地时刻 < 触发时刻 → 返回 0；
      若 `digest_runs` 中该日期已有记录 → 返回 0；
      否则进入汇总流程
- [ ] 3.2 汇总流程：取全部未完成待办 → 为空则写一条 `todo_count=0, delivered=true` 的记录并返回 0
      → 非空则构造汇总消息并投递 → 成功则写 `delivered=true, fired_at=now` 并逐条记账（见 4.x）
      → 失败则写 `delivered=false, last_error=脱敏原因`，返回 0，由下一轮扫描重试
- [ ] 3.3 重试不得跨天：若本地日期已变，则当天的记录不再重试（次日按新记录正常触发）
- [ ] 3.4 单条待办处理异常不得中断整轮；汇总构造/投递异常一律捕获并记日志，线程不得退出
- [ ] 3.5 移除 `TodoService.due_for_reminder()` 及其相关代码路径

## 4. 汇总文案与记账（M6）

- [ ] 4.1 新增 `notification_for_digest(todos, *, now) -> NotificationMessage`：
      标题含未完成数与「待办汇总」字样；正文逐条列出「序号. 标题（已超时 X / 来源 / 分类）」，
      按已超时时长降序；`kind=REMINDER`、`level=WARNING`、`source="notify-hub"`、`occurred_at=now`
- [ ] 4.2 汇总走 `DeliveryService.deliver(..., message_id=None, todo_id=None)`
- [ ] 4.3 投递成功后：为每条被覆盖的待办写一条 `todo_events(reminder)`、
      更新 `last_notified_at=now`、`reminder_count += 1`
- [ ] 4.4 汇总自身的投递记录为**一条**，`message_id` 与 `todo_id` 均为 None

## 5. 文档

- [ ] 5.1 `config.example.yaml`：`reminders` 段改为 `at` / `timezone` / `scan_interval_seconds`
- [ ] 5.2 `docs/configuration.md`：改写提醒参数一节，说明触发语义（越过时刻的第一轮检查定案、
      空则当日不发、失败当日重试、跨天不补发）与旧键已移除
- [ ] 5.3 `docs/architecture-overview.md` 与 `docs/architecture-map.html`：改写「超时提醒」那条链路
      的步骤与涉及文件

## 6. 测试

- [ ] 6.1 配置测试：新默认值、`at`/`timezone`/`scan_interval` 的合法与非法用例、
      旧键出现即报错的用例
- [ ] 6.2 模型测试：`digest_runs` 建表、`local_date` 唯一、`init_schema` 幂等
- [ ] 6.3 调度测试（用 `ManualClock` 推进，禁止真实等待）：未到时刻不发、到达时刻发一条、
      同一天不重复、空待办不发且定案、当天稍后新建待办不触发、次日正常触发、
      失败后当日重试直到成功、跨天不补发、重启（新建调度器实例）不重复发送
- [ ] 6.4 文案与记账测试：明细按超时时长降序、含标题与已超时时长、每条待办各一条提醒事件、
      汇总只产生一条投递记录
- [ ] 6.5 端到端：投递两条 need_ack 消息 → 推进时钟越过触发时刻 → 收到**一条**含两条明细的汇总 →
      标记其中一条完成 → 次日汇总只含剩下那条
