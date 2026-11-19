import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

// Exercise pure TS modules using the existing compiler; no test/browser dependency added.
async function moduleUrl(path, replacements = {}) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8')
  let code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
  for (const [specifier, replacement] of Object.entries(replacements)) code = code.replaceAll(`'${specifier}'`, `'${replacement}'`)
  return `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
}
const { conversationItems } = await import(await moduleUrl('../src/features/assistant/conversation.ts'))
const { defaultLayout, fitPosition, fitSize, loadLayout } = await import(await moduleUrl('../src/features/assistant/layout.ts'))
const libUrl = await moduleUrl('../src/lib/api.ts')
const { setCsrfToken } = await import(libUrl)
const { streamAssistant } = await import(await moduleUrl('../src/features/assistant/api.ts', { '../../lib/api': libUrl }))

const call = (id) => ({ part_kind: 'tool-call', tool_call_id: id, tool_name: 'inspect', args: { id } })
const result = (id, outcome = 'success') => ({ part_kind: 'tool-return', tool_call_id: id, tool_name: 'inspect', content: { id }, outcome })
const event = (type, part, run = 'run-1') => ({ type, part, args_valid: true, conversation_id: 'c', run_id: run })
const conversation = (messages, pending = null) => ({ conversation_id: 'c', revision: 'r', usage: {}, messages, deferred_tool_requests: pending })
const request = { type: 'message', conversation_id: 'c', expected_revision: 'r', message: '检查', model_name: 'model' }

test('launcher is clamped after dragging or shrinking the viewport', () => {
  assert.deepEqual(fitPosition({ x: -500, y: 5000 }, { width: 390, height: 844 }), { x: 8, y: 788 })
  assert.deepEqual(defaultLayout({ width: 1440, height: 900 }).position, { x: 1372, y: 832 })
})

test('panel sizes respect desktop bounds, short screens and the mobile inset', () => {
  assert.deepEqual(fitSize({ width: 2000, height: 2000 }, { width: 1440, height: 900 }), { width: 720, height: 796 })
  assert.deepEqual(fitSize({ width: 1, height: 1 }, { width: 1440, height: 320 }), { width: 360, height: 296 })
  assert.deepEqual(fitSize({ width: 720, height: 800 }, { width: 390, height: 844 }), { width: 366, height: 820 })
})

test('stored layout ignores malformed data and retains only geometry', () => {
  const viewport = { width: 1440, height: 900 }
  assert.deepEqual(loadLayout('{bad', viewport), defaultLayout(viewport))
  assert.deepEqual(loadLayout('{"version":2}', viewport), defaultLayout(viewport))
  const saved = { version: 1, position: { x: 100, y: 200 }, size: { width: 500, height: 600 } }
  assert.deepEqual(loadLayout(JSON.stringify({ ...saved, unrelated: 'ignore' }), viewport), saved)
})

test('native tool returns never become user messages; original text and metadata are preserved', () => {
  const items = conversationItems(conversation([
    { kind: 'request', run_id: 'run-1', parts: [{ part_kind: 'user-prompt', content: '<page_context>...</page_context>问题' }],
      metadata: { evalforge: { text: '问题', page_context: { type: 'experiment', id: 7 } } } },
    { kind: 'response', run_id: 'run-1', parts: [call('a')] },
    { kind: 'request', run_id: 'run-1', parts: [result('a')] },
    { kind: 'response', run_id: 'run-1', model_name: 'model', parts: [{ part_kind: 'text', content: '完成' }] },
  ]))
  assert.deepEqual(items.filter((item) => item.kind === 'message').map((item) => [item.role, item.text]), [['user', '问题'], ['assistant', '完成']])
  assert.equal(items[0].pageContext.id, 7)
  assert.equal(items[1].tools[0].status, 'success')
})

test('concurrent tool results are paired by call id even when completed in reverse order', () => {
  const items = conversationItems(undefined, { events: [event('tool_call', call('a')), event('tool_call', call('b')),
    event('tool_result', result('b')), event('tool_result', result('a'))] })
  assert.deepEqual(items[0].tools.map((tool) => [tool.toolCallId, tool.result.id]), [['a', 'a'], ['b', 'b']])
})

test('approval resume updates the original call across runs and records denial without duplicates', () => {
  const native = conversation([{ kind: 'response', run_id: 'run-1', parts: [call('a')] }], { approvals: [call('a')], calls: [] })
  assert.equal(conversationItems(native)[0].tools[0].status, 'awaiting_approval')
  const before = JSON.stringify(native)
  const items = conversationItems(native, { events: [event('tool_call', call('a'), 'run-2'), event('tool_result', result('a', 'denied'), 'run-2')] })
  assert.equal(items.length, 1)
  assert.equal(items[0].tools.length, 1)
  assert.equal(items[0].tools[0].status, 'denied')
  assert.equal(JSON.stringify(native), before)
})

test('a completed call id reused in another run is a new execution', () => {
  const items = conversationItems(conversation([
    { kind: 'response', run_id: 'run-1', parts: [call('a')] }, { kind: 'request', run_id: 'run-1', parts: [result('a')] },
    { kind: 'response', run_id: 'run-2', parts: [call('a')] }, { kind: 'request', run_id: 'run-2', parts: [result('a')] },
  ]))
  assert.equal(items.length, 2)
  assert.notEqual(items[0].tools[0].id, items[1].tools[0].id)
})

test('business errors do not render as successful tools', () => {
  const part = { ...result('a'), content: { error: { code: 'not_found' } } }
  assert.equal(conversationItems(undefined, { events: [event('tool_call', call('a')), event('tool_result', part)] })[0].tools[0].status, 'failed')
  const job = { ...result('b'), content: { job_id: 'b', error: null } }
  assert.equal(conversationItems(undefined, { events: [event('tool_call', call('b')), event('tool_result', job)] })[0].tools[0].status, 'success')
})

function frame(name, data) { return `event: ${name}\ndata: ${JSON.stringify(data)}\n\n` }
function mockStream(t, frames) {
  setCsrfToken('csrf')
  const bytes = new TextEncoder().encode(frames)
  t.mock.method(globalThis, 'fetch', async () => new Response(new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i += 7) controller.enqueue(bytes.slice(i, i + 7))
      controller.close()
    },
  }), { headers: { 'content-type': 'text/event-stream' } }))
}
function handlers() {
  const received = { text: '', tools: [], cards: [], approvals: [] }
  return { received, onText: (value) => { received.text += value }, onTool: (value) => received.tools.push(value),
    onDisplay: (value) => received.cards.push(value), onApproval: (value) => received.approvals.push(value) }
}

test('SSE parses split UTF-8 text, tool/deferred events and approval cards on one connection', async (t) => {
  const deferred = { conversation_id: 'c', run_id: 'run-1', requests: { calls: [], approvals: [call('a')] } }
  mockStream(t, frame('tool_call', event('tool_call', call('a'))) + frame('tool_deferred', deferred)
    + frame('text_delta', { text: '等待确认' }) + frame('approval_required', { revision: 'r2', cards: [] })
    + frame('done', { revision: 'r2', status: 'awaiting_approval' }))
  const h = handlers()
  assert.deepEqual(await streamAssistant(request, h), { revision: 'r2', status: 'awaiting_approval' })
  assert.equal(h.received.text, '等待确认')
  assert.deepEqual(h.received.tools.map((item) => item.type), ['tool_call', 'tool_deferred'])
  assert.equal(h.received.approvals[0].revision, 'r2')
})

test('SSE rejects events for another conversation or another run', async (t) => {
  mockStream(t, frame('tool_call', { ...event('tool_call', call('a')), conversation_id: 'foreign' }))
  await assert.rejects(streamAssistant(request, handlers()), /所属会话或运行不一致/)
  mockStream(t, frame('tool_call', event('tool_call', call('a'))) + frame('tool_result', event('tool_result', result('a'), 'other-run')))
  await assert.rejects(streamAssistant(request, handlers()), /所属会话或运行不一致/)
})

test('a broken stream preserves an already delivered display card and never reports done', async (t) => {
  mockStream(t, frame('display', { id: 'card', tool_call_id: 'a', title: 'Runner', description: '保存凭证',
    fields: [{ label: '凭证', value: 'test-token', sensitive: true }] }))
  const h = handlers()
  await assert.rejects(streamAssistant(request, h), /连接中断/)
  assert.equal(h.received.cards[0].fields[0].value, 'test-token')
})
