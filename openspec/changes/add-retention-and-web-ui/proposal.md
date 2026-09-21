# 有界存储：数据回收 + 待办网页改版

## Why

两件事，一个共同目标：**长时间使用之后，存储占用不能持续变大。**

**一、存储无界增长。** 现在五张表全部永久保留，没有任何回收机制。实测当前库 40 KB，
其中载荷占比最大的是 `messages`（8 行 / 1399 字节），而 `deliveries` 每次投递尝试都写一行——
**它的增长速度比消息还快**（一条消息可能产生首次通知 + 多次重试 + 汇总投递）。
`digest_runs` 每天一行，虽然小，但同样无界。长期跑下去，「我到底存了多少东西」会变成一个
只有靠手工清库才能回答的问题。

**二、已完成的待办在页面上越堆越多。** 「昨天做过什么」和「上周做过什么」混在一个列表里，
看不出最近完成过哪些；而它们又确实占着空间。

## What Changes

**新增能力 `data-retention`**：按保留期自动回收。

- 新增配置 `retention.days`（默认 30，`0` = 关闭）
- **每日结算点触发一次**，回收「本地日期早于 `今天 - days`」的记录
- **待完成的待办永不回收**——它们还要进每日汇总，删了就是丢用户的事
- 回收顺序受外键约束：先删子表，再删父表

**新增能力 `todo-web-ui`**：待办网页改版（纯前端，零新增依赖）。

- 主列表页下方新增「最近完成」栏目，只显示**今天完成**的待办（本地自然日）
- 新增「清空已完成」按钮：删除完成日期早于今天的已完成待办
- 界面美化：只改 `base.html` 里的内联 CSS，不引入任何外部资源或 JS 库

## Capabilities

- `data-retention`（新增）：自动回收策略、硬约束与可观测性
- `todo-web-ui`（新增）：最近完成栏目、手动清空、页面自包含

## Impact

- `src/notify_hub/services/retention.py`（新增）
- `src/notify_hub/services/scheduler.py`：每日结算点挂回收
- `src/notify_hub/web/routes.py`、`web/templates/*.html`
- `src/notify_hub/config.py`：`retention` 段
- `src/notify_hub/context.py`：装配 `RetentionService`
- `config.example.yaml`、`docs/deployment.md`、`docs/configuration.md`

**明确不做**：不新增数据表、不改任何既有表结构、不改 API 契约、不改投递行为、
不引入任何前端依赖。
