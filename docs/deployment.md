# 部署说明

notify-hub 是单进程服务：一个 `python -m notify_hub` 进程同时提供 HTTP 接入、投递工作
线程、提醒调度与 Web 待办页面。部署只需要 Python 与一个可写的 SQLite 目录。

## 安全边界（强制）

- **默认只绑定回环地址 `127.0.0.1`**（由 `server.host` 决定，缺省即回环）。
- **不要暴露到公网**：本次范围**没有**内置鉴权与 TLS，任何能连到端口的人都能投递消息、
  读取待办与消息详情、执行「完成」操作。
- 跨机器使用需自加**反向代理**与**鉴权**（例如在 Nginx/Caddy 上终止 TLS、加认证中间件），
  反向代理只把请求转发给本机回环端口。反向代理与鉴权是后续变更的内容，不在本次范围。
- 如果确需监听非回环地址，务必先确认已有反向代理与鉴权在入口处生效。

## 数据文件位置

| 文件 | 默认位置 | 说明 |
|---|---|---|
| 配置文件 | `./config.yaml`（或 `NOTIFY_HUB_CONFIG` 指向的路径） | 从 `config.example.yaml` 复制 |
| 规则文件 | `./rules.yaml`（配置键 `rules.path`） | 从 `rules.example.yaml` 复制 |
| SQLite 数据库 | `./data/notify.db`（配置键 `storage.db_path`） | 目录需存在且可写 |
| 环境变量文件 | `/etc/notify-hub/notify-hub.env`（推荐） | 只放**环境变量名**对应的值，权限 0600 |

相对路径按配置文件所在目录解析，所以 systemd 单元里用 `WorkingDirectory` 固定工作目录
最稳妥。数据库、规则文件与配置都属于运行用户所有，不要让服务以 root 写共享目录。

## systemd 单元示例

把下面的单元保存为 `/etc/systemd/system/notify-hub.service`，按环境调整路径后执行
`systemctl daemon-reload && systemctl enable --now notify-hub`：

```ini
[Unit]
Description=notify-hub 消息汇聚与通知服务
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=notify-hub
Group=notify-hub
WorkingDirectory=/opt/notify-hub
Environment=NOTIFY_HUB_CONFIG=/opt/notify-hub/config.yaml
EnvironmentFile=-/etc/notify-hub/notify-hub.env
ExecStart=/opt/notify-hub/.venv/bin/python -m notify_hub
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

`EnvironmentFile` 里只写环境变量名对应的值（例如 webhook URL 与 SMTP 密码），不要把它们
写进单元文件或配置文件的 `credentials` 段——`credentials` 段里只写变量名本身。

## 启动与自检

```bash
systemctl status notify-hub
journalctl -u notify-hub -n 50 --no-pager
curl -sS http://127.0.0.1:8000/healthz
```

`/healthz` 返回 `{"status":"ok","time":"...","version":"..."}` 表示进程可用；它不会探测
任何通知渠道——渠道故障与进程故障是两件事。部署后还应确认页面可访问：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 跳转到 `/todos` |
| GET | `/todos` | 待办列表，`?status=pending\|done\|all`，默认 `pending` |
| GET | `/todos/{id}` | 待办详情 |
| POST | `/todos/{id}/done` | 标记完成（表单提交） |
| GET | `/messages` | 消息列表 |
| GET | `/messages/{id}` | 消息详情 |

## 备份与升级

- 备份：停服后复制 `storage.db_path` 指向的 SQLite 文件与规则文件即可；SQLite 用 WAL
  模式时连同 `-wal`/`-shm` 文件一起复制，或直接使用 `sqlite3 ... ".backup"`。
- 升级：替换代码后 `systemctl restart notify-hub`；数据库结构由服务在启动时初始化。
- 配置与规则的热加载：规则文件按 `rules.poll_interval_seconds` 轮询生效；`server.*`
  等启动期配置改动需要重启。
