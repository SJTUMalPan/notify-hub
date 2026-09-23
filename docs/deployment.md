# 部署说明

notify-hub 是单进程服务：一个 `python -m notify_hub` 进程同时提供 HTTP 接入、投递工作
线程、提醒调度与 Web 待办页面。部署只需要 Python 与一个可写的 SQLite 目录。

## 安全边界（强制）

- **应用自身默认且应当只绑定回环地址 `127.0.0.1`**（由 `server.host` 决定，缺省即回环）。
  这条边界**不要为了公网访问而改**——按下面「公网访问」一节的做，暴露交给独立进程。
- 服务**内置访问令牌认证**（`server.auth_token`）：配置之后，**除 `/healthz` 外的每一个路由**
  都要求凭据——包括投递入口 `POST /api/v1/messages`，它本身没有独立的鉴权。
- **未配置 `server.auth_token` 时鉴权整体关闭**（刻意的兼容默认，让单机使用无需凭据）。
  因此**暴露之前必须先配令牌**，否则任何能连到端口的人都能投递消息、读取待办与消息详情、
  执行「完成」操作。
- 需要跨机器或公网访问时，见下一节做法；需要 TLS 时在入口处用**反向代理**终止，
  代理只把请求转发给本机回环端口。**本版本不内置 TLS**。

## 公网访问（访问令牌）

### 它怎么工作

1. 浏览器首次访问 `http://<入口>/?token=<令牌>`（`Accept` 含 `text/html` 的 GET）
2. 服务返回 `303`，重定向到**去掉 `token` 参数**的同一地址，同时种下会话 Cookie
   `nh_session`（`HttpOnly`、`SameSite=Lax`、`Path=/`；值是该令牌的 HMAC，**不是令牌本身**）
3. 此后请求只带 Cookie，地址栏与浏览历史里不再出现令牌

命令行与脚本可以每次带 `?token=`，**不会**被重定向——只有 `Accept` 含 `text/html` 的 GET 才走
第 2 步。`/healthz` 始终免鉴权，供探活使用。

### 配置

```yaml
server:
  host: 127.0.0.1        # 保持回环，不要改成 0.0.0.0
  port: 8000
  auth_token: "<你的令牌>"
```

生成令牌，并把配置文件权限设为 `0600`、确保它不进版本库：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

令牌只能写在配置文件里——`server` 段**不支持环境变量占位符**（只有渠道的 `credentials` 支持）。

### 暴露的做法

应用**不改绑定**，由一个独立转发进程把 `0.0.0.0:<外部端口>` 转到 `127.0.0.1:8000`：

```
公网 → 宿主机已发布端口 → 容器内 0.0.0.0:3091 → 127.0.0.1:8000（notify-hub）
```

仓库外提供了 `~/notify-hub-run/start-public.sh`（`start` / `stop` / `status`）。
它在**未配置 `server.auth_token` 时拒绝启动转发**——用机制兜住「忘了配令牌」这个漏子，
而不是靠一句告警提醒人。回滚就是停掉转发进程，应用侧行为完全不变。

### 令牌轮换

改 `server.auth_token` 并重启服务即可；此前签发的会话 Cookie 立即失效。

### 已知限制

- **没有 TLS**：令牌与待办正文**明文过网**；会话 Cookie 因此**不能**设 `Secure`
  （设了浏览器就不会在 HTTP 上回传，功能直接坏掉）。将来上 TLS 时补 `Secure` 即可，
  认证设计本身不用改。
- **外部端口可能与其它服务冲突**：本部署借用的端口原先是给 DSH mobile profile 预留的，
  两者不能同时启用。
- **外部可达性要实测**：从容器内打自己的公网 IP 属于 hairpin，**会绕过云安全组的入方向检查**，
  不能用它证明外网可达。请从外部网络验证。

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
- **首次启用、或把 `retention.days` 调小之前，务必先备份**：回收会**真的删除**已完成的待办、
  无待办引用的旧消息与过期汇总状态，**删掉即无法恢复**。
- **回滚**：把 `retention.days` 设为 `0` 并重启即可停止回收。已被删除的历史找不回来，
  所以第一次上线这个功能时，建议先用一个偏大的 `days` 跑一轮、看看日志删了什么，再收紧。
- **怎么确认它删了什么**：回收真的删了东西时，日志里会出现**一条**含各类计数的 INFO；
  **什么都没删时不会打日志**。所以「日志里没有回收记录」就等于「这一轮什么都没删」。
- **两条硬约束**（不可配置）：未完成的待办永不回收、被任何待办引用的消息永不回收。
  因此存储占用的下界由未完成待办数决定，而不是纯时间。
- 升级：替换代码后 `systemctl restart notify-hub`；数据库结构由服务在启动时初始化。
- 配置与规则的热加载：规则文件按 `rules.poll_interval_seconds` 轮询生效；
  `server.*` 与 `retention.*` 等启动期配置改动需要重启。
