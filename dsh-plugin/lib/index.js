/**
 * dsh-ask-timeout-notify — 宿主侧 Cordis 插件。
 *
 * 作用：模型通过 `ask_user_question` 提问后，如果超过阈值（默认 60 分钟）仍无人回答，
 * 就往 notify-hub 投递一条 `need_ack` 消息，由 notify-hub 落成**待办**并按渠道推送
 * （飞书 / webhook / email）。
 *
 * 插在哪：`ctx.userQuestions.ask()` 会把请求广播到 `user-questions/request` 这条
 * **waterfall**（中间件链）上。本插件挂在链首，只做「起表 + 等待 + 到点通知」，
 * 然后原样 `await next()` 把请求交给下游——通常是 Web 端 `ui-user-questions`
 * 的 remote 事件回答者。因此：
 *   - 用户正常回答 → `next()` 返回，定时器销毁，**不发任何通知**；
 *   - 用户一直不答 → 定时器到点发通知，`next()` 继续挂着等用户（本插件不改变
 *     DSH 的等待语义，也不替用户作答）。
 *
 * 不变量：
 *   - 通知失败绝不影响问答主流程：所有投递异常都在 `notifyTimeout` 内部被吞成日志。
 *   - 同一个问题重复超时只产生一条待办：`dedup_key = ask:<session>:<question ids>`，
 *     服务端按 `(source, dedup_key)` 复用未完成的待办。
 *   - 令牌不写日志。
 *
 * @module dsh-ask-timeout-notify
 */

import { buildMessage, resolveConfig, sendMessage } from './notify.mjs'

/** 插件显示名。 */
export const name = 'ask-timeout-notify'

/**
 * 本插件只用到 cordis 核心能力（`ctx.timeout` 来自 base bundle 的 timer 服务、
 * `ctx.on`、`ctx.logger`），没有额外服务依赖；显式列出 `timer` 是为了让
 * "定时器由谁提供" 这件事在装载期就可校验。
 */
export const inject = ['timer']

/**
 * 等待中的提问。key 用 agent id + 问题 id，便于回答/失败时精确清理。
 * @type {Map<string, () => void>}
 */
const pendingTimers = new Map()

/**
 * 组装一个 pending 键。
 * @param {string} sessionId - 会话 id。
 * @param {string[]} ids - 问题 id 列表。
 * @returns {string} 键。
 */
function pendingKey(sessionId, ids) {
  return `${sessionId}::${ids.join(',')}`
}

/**
 * 从 agent 投影里安全地取工作目录（不同版本的 Session 或其 header 可能缺字段）。
 * @param {{ session?: { header?: Record<string, unknown> } }} [agent] - 提问的 agent。
 * @returns {string | undefined} 工作目录；取不到时 undefined。
 */
function workingDirectory(agent) {
  const cwd = agent?.session?.header?.cwd
  return typeof cwd === 'string' && cwd !== '' ? cwd : undefined
}

/**
 * 插件入口。
 *
 * @param {import('@deepseek-ai/cordis').Context} ctx - 宿主上下文。
 * @param {object} [config] - `cordis.patch.yml` 中本插件那一行的 `config`。
 */
export function apply(ctx, config = {}) {
  const settings = resolveConfig(config)

  if (!settings.enabled) {
    ctx.logger.info('ask-timeout-notify: 已禁用（DSH_ASK_NOTIFY_ENABLED=false），不装载超时通知')
    return
  }
  if (settings.reason !== null) {
    // 配置不全时仍装载：这样用户补上环境变量重启后立刻生效，而不是静默缺席。
    ctx.logger.warn(
      `ask-timeout-notify: 未启用超时通知——${settings.reason}；`
      + ' 超时后不会产生待办。见 dsh-plugin/README.md 的配置一节。',
    )
  } else {
    ctx.logger.info(
      `ask-timeout-notify: 已启用，阈值 ${settings.timeoutMinutes} 分钟，投递到 ${settings.endpoint}`,
    )
  }

  /**
   * 到点后的通知动作。永不抛异常。
   * @param {{ request: object, agent?: object }} params - 待通知的请求。
   */
  async function notifyTimeout({ request, agent }) {
    if (settings.endpoint === null || settings.token === null) return
    const { body, dedupKey } = buildMessage({
      request,
      agent,
      timeoutMinutes: settings.timeoutMinutes,
      source: settings.source,
      repoPath: workingDirectory(agent),
    })
    const result = await sendMessage({
      endpoint: settings.endpoint,
      token: settings.token,
      payload: body,
      requestTimeoutMs: settings.requestTimeoutMs,
    })
    if (result.ok) {
      ctx.logger.info(
        `ask-timeout-notify: 已投递待办 message_id=${result.messageId} todo_id=${result.todoId} dedup=${dedupKey}`,
      )
    } else {
      ctx.logger.warn(`ask-timeout-notify: 投递失败（${result.error}）；不影响等待中的问答`)
    }
  }

  ctx.on('user-questions/request', async (request, next) => {
    const sessionId = typeof request?.agent?.id === 'string' ? request.agent.id : 'unknown'
    const ids = (Array.isArray(request?.questions) ? request.questions : [])
      .map(item => String(item?.id ?? '').trim())
      .filter(id => id !== '')
    const key = pendingKey(sessionId, ids)

    // 起表：到点只发一次通知，然后等待继续由 next() 承担。
    const dispose = ctx.timeout(() => {
      pendingTimers.delete(key)
      void notifyTimeout({ request, agent: request?.agent })
    }, settings.timeoutMinutes * 60_000)
    pendingTimers.set(key, dispose)

    try {
      return await next()
    } finally {
      // 用户答了、请求被取消、或链上抛错——都要销毁定时器。
      const armed = pendingTimers.get(key)
      if (armed !== undefined) {
        pendingTimers.delete(key)
        armed()
      }
    }
  })

  ctx.logger.info('ask-timeout-notify: 已挂载 user-questions/request 监听')
}
