# 设计：公网访问待办网页

## Context

见 `proposal.md` 的 Why。这里只记录塑造方案的环境事实（全部为实测，非推断）：

| 事实 | 证据 |
|---|---|
| 本容器 `qoder-ai-env` 在 `docker_default` = `172.18.0.2` | `docker inspect` |
| 已发布端口：`3080→3081`（DSH）、`3090→3091`（闲置）、`2222→22` | `docker ps` |
| `3090` 链路已通：转发到 `127.0.0.1:8000` 后，`http://<公网IP>:3090/todos` 返回真实待办页 HTML | 实测（测毕已关闭转发进程） |
| 无端口挂载 `/etc/letsencrypt`；证书在宿主机、7 天有效期 | `docker inspect` + `openssl x509` |
| 宿主机 nginx 网关在另一 docker 网络（`172.19.0.4`），与本文容器**不可直连**；但可经宿主机已发布端口到达 | `gateway → 172.18.0.2` FAIL；`gateway → 172.19.0.1:3080` = 401 |
| 国际出网不可用（`api.cloudflare.com` 超时），隧道类方案不可行 | 实测 |
| `uvicorn.access` 的访问行**已在写 `service.log`** | `service.log` 中 `INFO: 127.0.0.1:... - "GET /todos HTTP/1.1" 200 OK` |

最后一条是本设计的主要动因：它意味着令牌一旦出现在查询串，就会被明文落盘。

## Goals / Non-Goals

**Goals**

- 手机浏览器通过 `http://<公网IP>:3090/` 查看待办并勾选完成
- 除 `/healthz` 外，任何路由都不接受无凭据访问——**包括无鉴权的投递入口**
  `POST /api/v1/messages`
- 令牌在任何日志中都不出现明文（含访问日志）
- `notify-hub` 进程自身的网络边界不放松（仍绑回环）

**Non-Goals**

- 不新增网页写操作；本次只保护已有的「标记完成」
- 不做 TLS、不做 DNS、不改宿主机网关、不做限流（43 字符随机令牌的暴力枚举不可行）
- 不改动渠道凭据、投递、分类、汇总提醒的任何行为

## Decisions

### D1：复用已发布端口 `3090→3091`，不新增映射

不重建容器（用户明确排除），不加 DNS。进程既不是特权端口，也不占用 DSH 的 3080。
**备选**：改天宏网关加 `/todo/` 路径（需改 12 处模板链接支持路径前缀，并与生产业务共用
server 块）；宿主机新增 TLS 小网关容器（不动业务，但要新增容器）。二者都保留为将来上 TLS 的路径。

### D2：认证放在应用层，而不是外部反代

`3090` 这条链路上**没有**任何反向代理可以挂鉴权，所以认证只能是应用的一部分。
**备选**：让宿主机网关反代并挂 Basic Auth——但那要改生产网关，且会把业务暴露面绑在一起。

### D3：URL 令牌 → 会话 Cookie（`HttpOnly` + `SameSite=Lax`）

首次 `GET ...?token=<令牌>` 且 `Accept` 含 `text/html` 时，用 `303` 把浏览器送到**去掉
`token` 参数**的同一路径，同时种下会话 Cookie。此后请求只带 Cookie。
**为什么不用纯 URL 令牌**：内部链接（`/todos`、`/messages`）全是绝对路径，纯 URL 令牌要求
把令牌写进每个链接，暴露面反而放大。**为什么不用 Basic Auth**：浏览器会自动重放凭据，
跨站 `POST` 因此可被利用；Cookie 方案配合 `SameSite=Lax` 天然挡住跨站 POST。
**为什么不用应用内登录页**：多一个模块的开发与测试成本，本次范围不值当。

### D4：令牌未配置 = 不启用鉴权（刻意的 fail-open 默认）

`server.auth_token` 为空时中间件完全透传。**理由**：现有 314 个测试与本地开发都基于
「无需凭据」，改成 fail-closed 会让每个 web/api 测试都要带凭据，改动面与风险远超收益。
**代价**：配置疏漏会静默暴露。**缓解**：启动时输出显著告警；部署侧的启动脚本在检测到
未配置令牌时**拒绝启动转发进程**（暴露路径因此被卡住，而不是靠告警提醒人）。

### D5：Cookie 值 = `HMAC-SHA256(token, "notify-hub-session-v1")`

不引入第二个密钥；跨进程重启稳定；Cookie 里不出现令牌本体（即便被读到，也无法反推
可用于构造 URL 的令牌）。比较一律用 `hmac.compare_digest`，且**先编码为 bytes** 再比较
（`compare_digest` 对含非 ASCII 的 `str` 会抛 `TypeError`）。

### D6：`/healthz` 不鉴权

它是探活端点，返回 `{"status":"ok","time":...,"version":...}`，不含业务数据。
**备选**：一并保护——但那会让「服务活着吗」这个最基本的运维问题也需要令牌。

### D7：本次不做 TLS（用户明确选择）

URL 为 `http://<公网IP>:3090/`。**后果**：令牌与待办正文明文过网；会话 Cookie
**不能**设 `Secure`（设了浏览器就不会在 HTTP 上回传，功能直接坏掉）。
这是一笔**有意识欠下的技术债**，已勘明两条低风险的上 TLS 路径（见 D1 备选），
届时只需补 `Secure` 与 TLS 终结，认证设计本身不用改。

### D8：令牌明文不入日志——修 `uvicorn` 的日志接管

`uvicorn.run()` 默认装 `dictConfig`，其中 `uvicorn.access` 为 `propagate=False` 且自带
handler，因此根 logger 上的 `SecretFilter` **对它无效**。改为 `log_config=None`，让
`uvicorn.*` 日志回到根 logger，由既有 `SecretFilter` + `redact_text`（`SENSITIVE_KEYS`
已含 `token`）统一脱敏；同时把令牌字面量加入 `credential_values`，使正文中的令牌也命中。
**备选**：`--no-access-log`（直接丢掉访问日志，牺牲可观测性，我们正是靠它验证过投递与请求）；
给 `uvicorn.access` 单独挂 filter（依赖 uvicorn 配置日志的时机，脆弱）。

### D9：`notify-hub` 仍绑 `127.0.0.1`；暴露由独立转发进程承担

`add-notify-hub/design.md` 决策 8 要求服务绑定回环，`main.py` 里也写死了这条约束。
本次变更有意**取代该决策的「服务不可从网络到达」意图**——这是用户明确要求的公网访问，
认证接管了这部分安全职责。但**应用自身的绑定不放松**：`0.0.0.0:3091` 由 `port-forward.js`
（宿主机既有工具，DSH 的 3081 通道同款）承担。好处是暴露面是一条可单独停止、可单独审计的
进程，而不是把服务的监听面永久改大。

## Risks / Trade-offs

- **令牌明文过网**（D7）→ 令牌可随时轮换；旧 Cookie 立刻失效。上 TLS 是既定后续，路径已勘明。
- **fail-open 默认**（D4）→ 启动脚本硬校验 + 启动告警；`server.auth_token` 一旦配置即全路由生效。
- **与 mobile DSH profile 抢 3090** → 该 profile 当前未运行；写入 `docs/deployment.md` 的已知冲突项。
- **阿里云安全组是否放行 3090 未验证** → 此前的验证是从容器内打公网 IP（hairpin），
  会绕过安全组入方向检查，**不能证明外网可达**。部署后由用户在手机上实测确认；若不通，
  改走 D1 的备选路径。
- **手机浏览器 Cookie 策略** → iOS Safari 对 IP 地址来源的 Cookie 无特殊限制，但
  `SameSite=Lax` 下从外部链接首次进入仍会带 Cookie；若实测异常，回退到每次带令牌。

## Migration Plan

1. 生成令牌（`python -c "import secrets;print(secrets.token_urlsafe(32))"`），写入
   `~/notify-hub-run/config.yaml` 的 `server.auth_token`（**仓库外，不入 git**）。
2. 重启 `notify-hub`（汇总去重状态在 `digest_runs` 表里，重启不丢当天状态）。
3. 起转发进程 `0.0.0.0:3091 → 127.0.0.1:8000`；启动脚本先校验令牌已配置。
4. 手机访问 `http://<公网IP>:3090/?token=<令牌>`，确认能看列表并勾选完成。
5. **回滚**：杀掉转发进程即可关掉公网入口。注意令牌**留着并不完全无害**——应用侧会继续
   要求凭据，`notify` CLI 需要带 `--token` 或设 `NOTIFY_HUB_TOKEN`。要完全回到变更前的行为，
   需同时清掉 `server.auth_token` 并重启。
6. **轮换**：改配置里的令牌并重启，旧 Cookie 立即失效。
