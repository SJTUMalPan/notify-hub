# 分类规则编写指南

分类器读取一份 YAML 规则文件（配置键 `rules.path`，示例见仓库根的 `rules.example.yaml`），
按规则顺序逐条求值，把每条消息映射为一个分类结果 `category`、一组 `labels`、是否需要确认
`need_ack` 与首选渠道 `channel`。文件变更后由 `rules.poll_interval_seconds` 轮询热加载。

## 规则文件结构

```yaml
case_sensitive: false

defaults:
  category: uncategorized
  labels: []
  need_ack: false
  channel: null

rules:
  - id: 规则的唯一标识
    match:
      source: [某个来源]
      level: [error]
      title_contains: ["关键字"]
      body_contains: ["关键字"]
    category: 命中后写入的分类
    labels: [标签]
    need_ack: true
    channel: 渠道 id
```

- `rules` 是规则列表，按书写顺序求值；每一项必须有 `id` 与 `category`。
- `defaults` 是没有任何规则命中时套用的默认动作，缺省 `category: uncategorized`。
- `case_sensitive` 缺省 `false`，即所有比较都不区分大小写。

## 匹配语义

`match` 是一个映射：**不同字段之间是「与」，同一字段的候选值之间是「或」**。

- 字段之间「与」：一条规则的 `match` 里所有字段都必须命中，规则才命中。
- 候选值之间「或」：`source: [db-backup, file-backup]` 表示来源等于其中任意一个即可。
- `match` 为空（或省略）表示匹配一切，通常放在规则列表末尾作为兜底。
- `title` / `body` 的 equals 比较在 `body is None` 时视为不匹配，不会抛异常。

支持的 `match` 字段（写错字段名会让规则文件加载失败，并保留上一份可用规则集）：

| 字段 | 匹配模式 | 含义 |
|---|---|---|
| `source` | equals | 消息来源等于候选值之一 |
| `level` | equals | 级别等于候选值之一（`info`/`warning`/`error`） |
| `title` | equals | 标题恰好等于候选值之一 |
| `body` | equals | 正文恰好等于候选值之一 |
| `title_contains` | contains | 标题包含候选子串之一 |
| `body_contains` | contains | 正文包含候选子串之一 |

`case_sensitive: false`（默认）下，equals 与 contains 都先把两侧折叠为小写再比较；
`case_sensitive: true` 时逐字符精确比较。`contains` 的空候选子串永远不命中，不会匹配一切。

## 顺序影响：first-match-wins

规则**自上而下**求值，**第一条命中的规则胜出**，其后的规则不再参与判定。因此：

- 越具体的规则越要写在前面，越宽泛的兜底规则越要写在后面。
- 调整两条规则的前后顺序可能改变分类结果，这是设计而非缺陷。
- 未命中任何规则时才套用 `defaults`（示例中 `category` 为 `uncategorized`）。

## 一个可直接加载的完整示例

下面的代码块是一份**真实可用**的规则文件，可以整体复制为 `rules.yaml` 后直接启动服务：

```yaml rules-example
case_sensitive: false

defaults:
  category: uncategorized
  labels: []
  need_ack: false
  channel: webhook

rules:
  - id: backup-failure
    match:
      source: [db-backup, file-backup]
      level: [error]
      title_contains: ["失败", "failed"]
      body_contains: ["exit code"]
    category: backup-failure
    labels: [infra, backup]
    need_ack: true
    channel: email

  - id: deploy-warning
    match:
      source: [ci]
      level: [warning]
      title_contains: ["deploy", "部署"]
    category: deploy-warning
    labels: [release]
    need_ack: false
    channel: webhook
```

按上面示例，第一条规则 `backup-failure` 命中一类消息：来源是 `db-backup`（或
`file-backup`）、级别是 `error`、标题含「失败」或 `failed`、正文含 `exit code`；它的
`category` 是 `backup-failure`，标签是 `infra` 与 `backup`，需要确认，首选渠道是 `email`。
第二条规则 `deploy-warning` 处理来源为 `ci` 的告警，`category` 为 `deploy-warning`。
两者都不命中时落到 `defaults`，`category` 为 `uncategorized`。

## 新增一条规则

1. 打开规则文件，在 `rules` 列表的**合适位置**插入一条新规则：越具体越靠前。
2. 填写 `id`（唯一、可读）与 `category`（分类结果名）。
3. 在 `match` 里选择字段与候选值；不同字段之间「与」，候选值之间「或」。
4. 按需设置 `labels`、`need_ack`、`channel`；省略则沿用该规则类型的默认（字段默认空）。
5. 保存文件。热加载轮询最多等待 `rules.poll_interval_seconds` 秒后生效；若写错字段名或
   缺少 `id`/`category`，加载失败会**保留上一份可用规则集**并记录一条告警，服务不中断。
6. 用一条真实消息验证：`notify --source <来源> --title <标题> --body <正文> --level <级别>`
   投递后查看待办详情里的 `category` 与规则 id。
