/**
 * `lib/notify.mjs` 的单测：配置解析、请求体构造、以及**真实 HTTP** 投递。
 *
 * 这里刻意不用 mock fetch —— 起一个真的 `node:http` 服务端来收请求，才能断言
 * 「发出去的请求行、查询串、头、请求体」这一整条线，而不是断言我们自己的对象。
 * notify-hub 的服务端契约（`MessageIn`、查询串令牌、202 响应体）就是靠这个测试钉住的。
 *
 * 跑法：`cd dsh-plugin && npm test`
 */

import { createServer } from 'node:http'
import { after, describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  DEFAULT_TIMEOUT_MINUTES,
  buildMessage,
  normalizeEndpoint,
  parseBoolean,
  parsePositiveInt,
  resolveConfig,
  sendMessage,
} from '../lib/notify.mjs'

/**
 * 起一个一次性 HTTP 服务端，记录收到的请求。
 * @param {{ status?: number, responseBody?: string, delayMs?: number }} [options] - 行为配置。
 * @returns {Promise<{ endpoint: string, requests: object[], close: () => Promise<void> }>} 服务端句柄。
 */
async function startServer(options = {}) {
  const { status = 202, responseBody = '{"message_id":7,"todo_id":3}', delayMs = 0 } = options
  const requests = []
  const server = createServer((req, res) => {
    const chunks = []
    req.on('data', chunk => chunks.push(chunk))
    req.on('end', () => {
      requests.push({
        method: req.method,
        url: req.url,
        headers: req.headers,
        raw: Buffer.concat(chunks).toString('utf8'),
      })
      const send = () => {
        res.writeHead(status, { 'content-type': 'application/json' })
        res.end(responseBody)
      }
      if (delayMs > 0) setTimeout(send, delayMs)
      else send()
    })
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address()
  return {
    endpoint: `http://127.0.0.1:${port}`,
    requests,
    close: () => new Promise(resolve => server.close(() => resolve())),
  }
}

/** 关掉所有测试里起过的服务端。 */
const opened = []
after(async () => {
  await Promise.all(opened.map(close => close()))
})

/**
 * 起服务端并登记，保证测试结束一定关掉。
 * @param {object} [options] - 同 startServer。
 * @returns {Promise<object>} 服务端句柄。
 */
async function openServer(options) {
  const handle = await startServer(options)
  opened.push(handle.close)
  return handle
}

describe('parsePositiveInt / parseBoolean / normalizeEndpoint', () => {
  it('整数解析：合法值生效，非法值回退，越界回退', () => {
    assert.equal(parsePositiveInt('15', 60), 15)
    assert.equal(parsePositiveInt(' 15 ', 60), 15)
    assert.equal(parsePositiveInt(undefined, 60), 60)
    assert.equal(parsePositiveInt('', 60), 60)
    assert.equal(parsePositiveInt('abc', 60), 60)
    assert.equal(parsePositiveInt('1.5', 60), 60)
    assert.equal(parsePositiveInt('0', 60), 60)
    assert.equal(parsePositiveInt('-3', 60), 60)
    assert.equal(parsePositiveInt('99999', 60, { max: 10080 }), 60)
    assert.equal(parsePositiveInt('10080', 60, { max: 10080 }), 10080)
  })

  it('布尔解析：常见写法都认，其它回退', () => {
    for (const raw of ['1', 'true', 'TRUE', 'yes', 'on']) assert.equal(parseBoolean(raw, false), true)
    for (const raw of ['0', 'false', 'no', 'off']) assert.equal(parseBoolean(raw, true), false)
    assert.equal(parseBoolean('maybe', true), true)
    assert.equal(parseBoolean(undefined, false), false)
  })

  it('地址归一化：去尾斜杠，空值返回 null', () => {
    assert.equal(normalizeEndpoint('http://127.0.0.1:8000/'), 'http://127.0.0.1:8000')
    assert.equal(normalizeEndpoint('http://127.0.0.1:8000///'), 'http://127.0.0.1:8000')
    assert.equal(normalizeEndpoint('  http://h:1/  '), 'http://h:1')
    assert.equal(normalizeEndpoint(''), null)
    assert.equal(normalizeEndpoint('   '), null)
    assert.equal(normalizeEndpoint(undefined), null)
  })
})

describe('resolveConfig', () => {
  const complete = { DSH_NOTIFY_HUB_ENDPOINT: 'http://127.0.0.1:8000', DSH_NOTIFY_HUB_TOKEN: 'tok' }

  it('默认阈值是 60 分钟，且来源可配', () => {
    const settings = resolveConfig({}, complete)
    assert.equal(settings.timeoutMinutes, DEFAULT_TIMEOUT_MINUTES)
    assert.equal(settings.timeoutMinutes, 60)
    assert.equal(settings.source, 'dsh')
    assert.equal(settings.reason, null)
    assert.equal(settings.enabled, true)
  })

  it('环境变量覆盖 config：阈值与来源', () => {
    const settings = resolveConfig(
      { timeoutMinutes: 5, source: 'from-config' },
      { ...complete, DSH_ASK_TIMEOUT_MINUTES: '120', DSH_ASK_NOTIFY_SOURCE: 'from-env' },
    )
    assert.equal(settings.timeoutMinutes, 120)
    // source 不经环境变量覆盖，只认 config —— 保持来源标识稳定，便于规则匹配。
    assert.equal(settings.source, 'from-config')
  })

  it('config 兜底：没给环境变量时用 config 里的值', () => {
    const settings = resolveConfig(
      { timeoutMinutes: 30, endpoint: 'http://cfg:9000/', token: 'cfg-token', source: 'cfg-src' },
      {},
    )
    assert.equal(settings.timeoutMinutes, 30)
    assert.equal(settings.endpoint, 'http://cfg:9000')
    assert.equal(settings.token, 'cfg-token')
    assert.equal(settings.source, 'cfg-src')
    assert.equal(settings.reason, null)
  })

  it('缺地址或缺令牌时给出可读原因（仍装载，但不投递）', () => {
    assert.match(resolveConfig({}, {}).reason, /ENDPOINT/u)
    assert.match(resolveConfig({ endpoint: 'http://h:1' }, {}).reason, /TOKEN/u)
    assert.match(resolveConfig({ token: 'only-token' }, {}).reason, /ENDPOINT/u)
  })

  it('显式关闭', () => {
    const settings = resolveConfig({}, { ...complete, DSH_ASK_NOTIFY_ENABLED: 'false' })
    assert.equal(settings.enabled, false)
    assert.equal(settings.reason, 'disabled')
  })
})

describe('buildMessage', () => {
  const request = {
    questions: [
      {
        id: 'q1',
        header: '确认',
        question: '要不要现在重启 DSH？',
        options: [{ label: '现在重启', description: '立刻生效' }, { label: '稍后' }],
      },
      { id: 'q2', question: '超时阈值设多少？', detail: '按分钟计' },
    ],
  }
  const agent = { id: 'session-abc', session: { header: { cwd: '/workspace/proj' } } }

  it('请求体只带契约字段，need_ack=true，dedup_key 稳定', () => {
    const { body, dedupKey } = buildMessage({
      request,
      agent,
      timeoutMinutes: 60,
      source: 'dsh',
      repoPath: '/workspace/proj',
      now: new Date('2026-09-20T00:00:00Z'),
    })

    assert.deepEqual(Object.keys(body).sort(), [
      'body', 'dedup_key', 'level', 'meta', 'need_ack', 'source', 'title',
    ])
    assert.equal(body.source, 'dsh')
    assert.equal(body.need_ack, true)
    assert.equal(body.level, 'warning')
    assert.equal(body.dedup_key, 'ask:session-abc:q1,q2')
    assert.equal(dedupKey, body.dedup_key)
    assert.equal(body.meta.session, 'session-abc')
    assert.deepEqual(body.meta.question_ids, ['q1', 'q2'])
    assert.equal(body.meta.timeout_minutes, 60)
  })

  it('同一批问题重复构造得到同一个 dedup_key（服务端据此去重）', () => {
    const first = buildMessage({ request, agent, timeoutMinutes: 60, source: 'dsh' })
    const second = buildMessage({ request, agent, timeoutMinutes: 60, source: 'dsh' })
    assert.equal(first.dedupKey, second.dedupKey)
  })

  it('正文包含题干、选项与工作目录', () => {
    const { body } = buildMessage({
      request, agent, timeoutMinutes: 60, source: 'dsh', repoPath: '/workspace/proj',
      now: new Date('2026-09-20T00:00:00Z'),
    })
    assert.match(body.title, /60 分钟/u)
    assert.match(body.title, /要不要现在重启 DSH/u)
    assert.match(body.body, /要不要现在重启 DSH/u)
    assert.match(body.body, /- 现在重启 —— 立刻生效/u)
    assert.match(body.body, /- 稍后/u)
    assert.match(body.body, /2\. 超时阈值设多少/u)
    assert.match(body.body, /说明：按分钟计/u)
    assert.match(body.body, /工作目录：\/workspace\/proj/u)
  })

  it('缺 agent / 缺问题字段时不抛异常', () => {
    const bare = buildMessage({ request: {}, timeoutMinutes: 60, source: 'dsh' })
    assert.equal(bare.dedupKey, 'ask:unknown:')
    assert.equal(bare.body.meta.session, 'unknown')
    assert.equal(typeof bare.body.title, 'string')
  })

  it('超长题干被截断，标题保持单行', () => {
    const long = { questions: [{ id: 'q', question: 'x'.repeat(500) }] }
    const { body } = buildMessage({ request: long, timeoutMinutes: 60, source: 'dsh' })
    assert.ok(body.title.length < 120, `标题过长：${body.title.length}`)
    assert.ok(!body.title.includes('\n'))
  })
})

describe('sendMessage（真实 HTTP）', () => {
  it('POST /api/v1/messages?token=… 带 JSON 体，解析 202 响应', async () => {
    const server = await openServer()
    const payload = { source: 'dsh', title: 't', body: 'b', level: 'warning', need_ack: true, dedup_key: 'k', meta: {} }
    const result = await sendMessage({ endpoint: server.endpoint, token: 'secret-token', payload })

    assert.equal(result.ok, true)
    assert.equal(result.status, 202)
    assert.equal(result.messageId, 7)
    assert.equal(result.todoId, 3)

    assert.equal(server.requests.length, 1)
    const received = server.requests[0]
    assert.equal(received.method, 'POST')
    assert.equal(received.url, '/api/v1/messages?token=secret-token')
    assert.equal(received.headers['content-type'], 'application/json')
    assert.deepEqual(JSON.parse(received.raw), payload)
  })

  it('令牌按 URL 规则转义（含特殊字符也不会拼坏查询串）', async () => {
    const server = await openServer()
    await sendMessage({ endpoint: server.endpoint, token: 'a b&c=d', payload: { source: 's' } })
    assert.equal(server.requests[0].url, '/api/v1/messages?token=a%20b%26c%3Dd')
  })

  it('401 不抛异常，返回可读错误', async () => {
    const server = await openServer({ status: 401, responseBody: '{"detail":"unauthorized"}' })
    const result = await sendMessage({ endpoint: server.endpoint, token: 'wrong', payload: {} })
    assert.equal(result.ok, false)
    assert.equal(result.status, 401)
    assert.match(result.error, /401/u)
  })

  it('连不上服务端时不抛异常', async () => {
    // 127.0.0.1:1 上不会有服务端
    const result = await sendMessage({ endpoint: 'http://127.0.0.1:1', token: 't', payload: {} })
    assert.equal(result.ok, false)
    assert.equal(typeof result.error, 'string')
  })

  it('响应超时会被中断并报超时', async () => {
    const server = await openServer({ delayMs: 500 })
    const result = await sendMessage({
      endpoint: server.endpoint, token: 't', payload: {}, requestTimeoutMs: 80,
    })
    assert.equal(result.ok, false)
    assert.match(result.error, /超时/u)
  })

  it('非 JSON 响应按失败处理', async () => {
    const server = await openServer({ status: 200, responseBody: '<html>nope</html>' })
    const result = await sendMessage({ endpoint: server.endpoint, token: 't', payload: {} })
    assert.equal(result.ok, false)
    assert.match(result.error, /不是 JSON/u)
  })
})
