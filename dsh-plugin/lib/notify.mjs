/**
 * 纯函数层：配置解析 + 通知内容构造 + 向 notify-hub 投递。
 *
 * 拆出来的唯一目的是可测——`lib/index.js` 负责 Cordis 接线（事件、定时器、
 * effect 生命周期），本文件不 import 任何 Cordis 模块，因此测试可以直接调用，
 * 也可以用一个真实的 node:http 假服务端断言**发出的请求体**。
 *
 * 设计约束（重要，改代码前先读）：
 *   - 服务端契约是 `POST /api/v1/messages`（见 notify-hub `src/notify_hub/api/schemas.py`
 *     的 `MessageIn`）。请求体**只放契约里声明过的字段**，多一个字段就是 422。
 *   - `need_ack: true` 是让 notify-hub 建待办的唯一开关：分类器按
 *     `declared_need_ack or rule.need_ack` 裁决，并记 `ack_reason=caller_declared`。
 *     所以本插件**不需要** notify-hub 侧加任何规则。
 *   - `dedup_key` 让同一问题重复超时不会刷出多条待办：建待办时按
 *     `(source, dedup_key)` 查未完成的待办并复用（`services/todos.py`）。
 *   - 令牌只出现在查询串里，**不写日志**；`AuthGuard` 对「已认证 + 非 GET 导航」
 *     直接透传，所以 POST 不会触发放置 Cookie 的 303。
 */

/** 默认阈值：1 小时。 */
export const DEFAULT_TIMEOUT_MINUTES = 60

/** HTTP 投递超时（毫秒）。比阈值小得多——只是别让一个卡住的连接挂着。 */
export const DEFAULT_REQUEST_TIMEOUT_MS = 5000

/** 待办来源标识；`(source, dedup_key)` 是服务端去重键的一半。 */
export const NOTIFY_SOURCE = 'dsh'

/** 通知正文里单条问题的截断长度（字符）。 */
const QUESTION_PREVIEW_MAX = 80

/** 标题里问题预览的截断长度（字符）。 */
const TITLE_PREVIEW_MAX = 60

/**
 * 解析正整数值。入参可能是环境变量（字符串）或 `cordis.patch.yml` 里的 config 值
 * （YAML 解析出来是数字），所以统一先转成字符串再判——只认字符串会让 config 路径
 * 静默失效（永远走回退值）。
 *
 * @param {unknown} raw - 原始值。
 * @param {number} fallback - 无法解析时的回退值。
 * @param {{ min?: number, max?: number }} [bounds] - 允许区间（含端点）。
 * @returns {number} 解析结果。
 */
export function parsePositiveInt(raw, fallback, bounds = {}) {
  if (raw === null || raw === undefined || typeof raw === 'object' || typeof raw === 'boolean') return fallback
  const text = String(raw).trim()
  if (text === '') return fallback
  const value = Number(text)
  if (!Number.isFinite(value) || !Number.isInteger(value)) return fallback
  const min = bounds.min ?? 1
  const max = bounds.max ?? Number.MAX_SAFE_INTEGER
  if (value < min || value > max) return fallback
  return value
}

/**
 * 解析布尔环境变量：`1/true/yes/on` 为真，`0/false/no/off` 为假，其余回退。
 * @param {string | undefined} raw - 环境变量原文。
 * @param {boolean} fallback - 无法解析时的回退值。
 * @returns {boolean} 解析结果。
 */
export function parseBoolean(raw, fallback) {
  if (typeof raw !== 'string') return fallback
  const value = raw.trim().toLowerCase()
  if (['1', 'true', 'yes', 'on'].includes(value)) return true
  if (['0', 'false', 'no', 'off'].includes(value)) return false
  return fallback
}

/**
 * 归一化 endpoint：去掉尾部斜杠，保证后面拼路径不会出现 `//api`。
 * @param {unknown} raw - 候选地址。
 * @returns {string | null} 归一化后的地址；非法输入返回 null。
 */
export function normalizeEndpoint(raw) {
  if (typeof raw !== 'string') return null
  const trimmed = raw.trim().replace(/\/+$/u, '')
  if (trimmed === '') return null
  return trimmed
}

/**
 * 合并「插件 config」与「环境变量」，环境变量优先——因为同一份插件装在多台机器上时，
 * 只有环境变量能表达机器差异（见 README 的配置一节）。
 *
 * @param {object} [config] - cordis.patch.yml 里那一行的 config。
 * @param {Record<string, string | undefined>} [env] - 环境变量表，默认 process.env。
 * @returns {{ enabled: boolean, endpoint: string | null, token: string | null,
 *   timeoutMinutes: number, requestTimeoutMs: number, source: string, reason: string | null }}
 *   归一化配置；`reason` 非空表示配置不可用及原因（调用方据此决定是否 warn）。
 */
export function resolveConfig(config = {}, env = process.env) {
  const rawTimeout = env.DSH_ASK_TIMEOUT_MINUTES ?? config.timeoutMinutes
  const timeoutMinutes = parsePositiveInt(rawTimeout, DEFAULT_TIMEOUT_MINUTES, { min: 1, max: 10080 })
  const requestTimeoutMs = parsePositiveInt(
    env.DSH_ASK_NOTIFY_REQUEST_TIMEOUT_MS ?? config.requestTimeoutMs,
    DEFAULT_REQUEST_TIMEOUT_MS,
    { min: 100, max: 60000 },
  )
  const endpoint = normalizeEndpoint(env.DSH_NOTIFY_HUB_ENDPOINT ?? config.endpoint)
  const token = typeof env.DSH_NOTIFY_HUB_TOKEN === 'string' && env.DSH_NOTIFY_HUB_TOKEN.trim() !== ''
    ? env.DSH_NOTIFY_HUB_TOKEN.trim()
    : (typeof config.token === 'string' && config.token.trim() !== '' ? config.token.trim() : null)
  const enabled = parseBoolean(env.DSH_ASK_NOTIFY_ENABLED, config.enabled !== false)
  const source = typeof config.source === 'string' && config.source.trim() !== ''
    ? config.source.trim()
    : NOTIFY_SOURCE

  let reason = null
  if (!enabled) reason = 'disabled'
  else if (endpoint === null) reason = 'missing DSH_NOTIFY_HUB_ENDPOINT | 未配置 notify-hub 地址'
  else if (token === null) reason = 'missing DSH_NOTIFY_HUB_TOKEN | 未配置 notify-hub 令牌'

  return { enabled, endpoint, token, timeoutMinutes, requestTimeoutMs, source, reason }
}

/**
 * 截断并压平空白，用于标题/预览。
 * @param {unknown} text - 原文。
 * @param {number} max - 上限（字符）。
 * @returns {string} 单行摘要。
 */
function condense(text, max) {
  const flat = String(text ?? '').replace(/\s+/gu, ' ').trim()
  return flat.length <= max ? flat : `${flat.slice(0, max - 1)}…`
}

/**
 * 把一条问题渲染成人类可读的几行（题干 + 选项 + 补充说明）。
 * @param {{ question?: unknown, header?: unknown, options?: unknown, detail?: unknown }} item - 问题。
 * @param {number} index - 序号，从 1 开始。
 * @param {number} total - 问题总数。
 * @returns {string[]} 行数组。
 */
function renderQuestion(item, index, total) {
  const lines = []
  const prefix = total > 1 ? `${index}. ` : ''
  const header = typeof item?.header === 'string' && item.header.trim() !== '' ? `【${item.header.trim()}】` : ''
  lines.push(`${prefix}${header}${String(item?.question ?? '(空问题)')}`)
  if (typeof item?.detail === 'string' && item.detail.trim() !== '') {
    lines.push(`   说明：${condense(item.detail, 300)}`)
  }
  const options = Array.isArray(item?.options) ? item.options : []
  for (const option of options) {
    const label = String(option?.label ?? '')
    const description = typeof option?.description === 'string' && option.description.trim() !== ''
      ? ` —— ${condense(option.description, 120)}`
      : ''
    lines.push(`   - ${label}${description}`)
  }
  return lines
}

/**
 * 构造 notify-hub 的 `MessageIn` 请求体。
 *
 * @param {object} params - 入参。
 * @param {{ questions?: unknown[] }} params.request - user-questions 请求。
 * @param {{ id?: string }} [params.agent] - 提问的 agent（会话投影）。
 * @param {number} params.timeoutMinutes - 实际等待的分钟数（写进正文与 meta）。
 * @param {string} params.source - 来源标识。
 * @param {string} [params.repoPath] - 会话工作目录，方便待办里一眼看出在哪个项目。
 * @param {Date} [params.now] - 当前时间（测试注入）。
 * @returns {{ body: object, dedupKey: string }} 请求体与去重键。
 */
export function buildMessage({ request, agent, timeoutMinutes, source, repoPath, now = new Date() }) {
  const questions = Array.isArray(request?.questions) ? request.questions : []
  const sessionId = typeof agent?.id === 'string' && agent.id !== '' ? agent.id : 'unknown'
  const ids = questions
    .map(item => String(item?.id ?? '').trim())
    .filter(id => id !== '')
  const preview = condense(questions[0]?.question, TITLE_PREVIEW_MAX)
  const waited = timeoutMinutes === 1 ? '1 分钟' : `${timeoutMinutes} 分钟`
  const dedupKey = `ask:${sessionId}:${ids.join(',')}`

  const lines = [
    `模型通过 ask_user_question 提问后已等待超过 ${waited}，尚未收到回答。`,
    `提问时间：${now.toISOString()}`,
  ]
  if (repoPath) lines.push(`工作目录：${repoPath}`)
  lines.push('', ...questions.flatMap((item, index) => renderQuestion(item, index + 1, questions.length)))
  lines.push('', '打开 DSH 会话回答上面的问题后，即可在对应界面把本条待办标记完成。')

  return {
    dedupKey,
    body: {
      source,
      title: `DSH 提问等待超时（${waited}）：${preview || '等待回答'}`,
      body: lines.join('\n'),
      level: 'warning',
      need_ack: true,
      dedup_key: dedupKey,
      meta: {
        session: sessionId,
        question_ids: ids,
        timeout_minutes: timeoutMinutes,
      },
    },
  }
}

/**
 * 投递一条消息到 notify-hub。
 *
 * 永不抛异常：任何失败都以 `{ok: false, error}` 返回，由调用方记日志——
 * 通知失败绝不能影响 DSH 里正在等待的那个问答（它才是主流程）。
 *
 * @param {object} params - 入参。
 * @param {string} params.endpoint - notify-hub 基地址。
 * @param {string} params.token - 访问令牌（放查询串）。
 * @param {object} params.payload - 请求体。
 * @param {number} [params.requestTimeoutMs] - HTTP 超时。
 * @param {typeof fetch} [params.fetchImpl] - 注入的 fetch（测试用）。
 * @returns {Promise<{ ok: boolean, status?: number, messageId?: number, todoId?: number|null, error?: string }>}
 *   投递结果。
 */
export async function sendMessage({ endpoint, token, payload, requestTimeoutMs = DEFAULT_REQUEST_TIMEOUT_MS, fetchImpl = fetch }) {
  const url = `${endpoint}/api/v1/messages?token=${encodeURIComponent(token)}`
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), requestTimeoutMs)
  try {
    const response = await fetchImpl(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
      signal: controller.signal,
    })
    const text = await response.text()
    if (!response.ok) {
      return { ok: false, status: response.status, error: `HTTP ${response.status}: ${condense(text, 200)}` }
    }
    let parsed = null
    try {
      parsed = JSON.parse(text)
    } catch {
      parsed = null
    }
    if (parsed === null || typeof parsed !== 'object') {
      return { ok: false, status: response.status, error: `响应不是 JSON：${condense(text, 200)}` }
    }
    return {
      ok: true,
      status: response.status,
      messageId: parsed.message_id,
      todoId: parsed.todo_id ?? null,
    }
  } catch (error) {
    const reason = error?.name === 'AbortError' ? `请求超时（${requestTimeoutMs}ms）` : String(error?.message ?? error)
    return { ok: false, error: reason }
  } finally {
    clearTimeout(timer)
  }
}
