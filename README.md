# notify-hub

统一的消息汇聚、分类、待办跟踪与多渠道路由通知服务。

把散落在各处的脚本、CI、监控告警统一投递到一个 HTTP 接口，服务按规则给消息打上
分类、标签与「是否需要确认」，把需要跟进的消息落成**待办**，并按渠道（webhook / email /
feishu）路由投递；超时未完成的待办会被反复提醒，直到有人在 Web 页面点「完成」。

## 安装

需要 Python 3.10+。在仓库根目录创建虚拟环境并安装：

```bash
python3 -m venv .venv
.venv/bin/pip install --no-build-isolation -e ".[dev]"
```

（`--no-build-isolation` 使用 venv 内已有的 setuptools/wheel，避免隔离构建挂起；
常规环境也可用 `.venv/bin/pip install -e ".[dev]"`。）

## 配置

从示例复制出实际配置与规则文件：

```bash
cp config.example.yaml config.yaml
cp rules.example.yaml rules.yaml
```

- 配置文件默认 `./config.yaml`，也可用环境变量 `NOTIFY_HUB_CONFIG` 指向别处。
- `channels[].credentials` 里写的**只是环境变量名**，凭据本身放在运行环境里，
  例如 `NOTIFY_WEBHOOK_URL`、`NOTIFY_SMTP_PASSWORD`、`NOTIFY_FEISHU_WEBHOOK_URL`；
  变量为空则该渠道不可用，服务照常启动。
- 内置渠道类型为 `webhook`、`email` 与 `feishu`（飞书自定义机器人；启用加签时再声明
  `secret`，不用加签时不要声明该键）。每个配置键的含义见
  [docs/configuration.md](docs/configuration.md)。

## 启动

```bash
.venv/bin/python -m notify_hub
```

服务默认只绑定回环地址 `127.0.0.1:8000`。健康检查：

```bash
curl -sS http://127.0.0.1:8000/healthz
```

## 投递一条消息

HTTP 示例（`POST /api/v1/messages`，成功返回 `202` 与 `message_id`）：

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{"source":"db-backup","title":"备份失败","body":"exit code 1","level":"error","need_ack":true}'
```

CLI 示例（默认连接 `http://127.0.0.1:8000`，可用 `--endpoint` 覆盖）：

```bash
notify --source db-backup --title "备份失败" --body "exit code 1" --level error --need-ack --dedup-key backup-2024 --endpoint http://127.0.0.1:8000
```

CLI 的 `--body-stdin` 可从标准输入读正文；退出码约定为 `0` 成功、`1` 服务端拒绝、
`2` 用法错误、`3` 无法连接服务端。

## 完成流程

消息命中需要确认的规则后会生成待办。列表与详情在 Web 页面：

- `/`：跳转到待办列表。
- `/todos`：待办列表，支持 `?status=pending|done|all`。
- `/todos/{id}`：待办详情（含消息与投递记录）。
- `/todos/{id}/done`：`POST` 标记完成（页面上的「完成」按钮）。

也可以直接用 HTTP 完成一条待办：

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/todos/1/done
```

未在提醒间隔内完成的待办会按 `reminders` 配置重复提醒；消息历史可浏览 `/messages` 与
`/messages/{id}`。

## DSH 插件（`dsh-plugin/`）

仓库里还带一个 DSH 宿主插件：模型用 `ask_user_question` 提问后，若超过阈值（默认 60 分钟）
无人回答，就往本服务的 `POST /api/v1/messages` 投一条 `need_ack` 消息，落成待办并推送。

它**只是本服务的一个普通消息生产者**，不改本服务任何代码、不需要额外规则：

```bash
# 装进 DSH 的 web profile（换机器时用 git 形式，见插件 README）
dsh plugin --profile web add "git+https://github.com/SJTUMalPan/notify-hub#<sha>&path:/dsh-plugin"
```

配置只需两个环境变量：`DSH_NOTIFY_HUB_ENDPOINT`（默认 `http://127.0.0.1:8000`）与
`DSH_NOTIFY_HUB_TOKEN`（本服务 `server.auth_token` 的值）。完整说明见
[dsh-plugin/README.md](dsh-plugin/README.md)。

## 文档索引

- [配置说明](docs/configuration.md)：每个配置键、环境变量与凭据引用方式、提醒参数关系。
- [分类规则编写指南](docs/rule-authoring.md)：匹配语义、`case_sensitive`、
  first-match-wins 与可直接加载的规则示例。
- [适配器开发指南](docs/adapter-guide.md)：`Notifier` 契约、`DeliveryResult` 失败语义、
  能力声明、平台专属嵌套载荷（范例 `notify_hub.notifiers.feishu`）与完整的 dummy 适配器示例。
- [部署说明](docs/deployment.md)：systemd 单元、数据文件位置与回环地址安全边界。
- [代码走读](docs/architecture-overview.md)：目录结构与逐文件职责、启动链、一条消息从接入到
  送达的完整数据流（每步标注确切的文件与函数）、四条跨模块不变量与建议的阅读顺序。
- [架构可视化](docs/architecture-map.html)：纯前端单文件，双击即可打开——数据流步进器、
  可过滤的模块地图、不变量与后台线程。
