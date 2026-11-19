import { ApiError, apiRequest, apiStream } from '../../lib/api'
import type { PageContext } from './pageContext'
import type { Conversation, ToolCallPart, ToolResultPart, ToolStreamEvent } from './conversation'

export type AssistantSession = { conversation_id: string; title: string; revision: string }
export type AssistantMessage = {
  id: string; role: 'user' | 'assistant'; text: string; model_name: string | null
  source_experiment_id: number | null; page_context?: PageContext | null
}
export type AssistantAction = {
  tool_call_id: string; tool: string; title: '批准请求'; description: string; arguments: Record<string, unknown>
}
export type ApprovalRequired = { revision: string; cards: AssistantAction[] }
export type DisplayField = { label: string; value: string; sensitive: boolean }
export type DisplayCard = { id: string; tool_call_id: string; title: string; description: string; fields: DisplayField[] }
export type Transcript = AssistantSession & {
  conversation: Conversation
  messages: AssistantMessage[]; status: string; actions: AssistantAction[]
  error: { code: string; message: string } | null
}
export type AssistantTurn = {
  revision: string; status: 'completed' | 'awaiting_approval'
}
export type AssistantStreamRequest = { conversation_id: string; expected_revision: string } & (
  { type: 'message'; message: string; model_name: string; page_context?: PageContext } |
  { type: 'decision'; decisions: Record<string, boolean> }
)

const sessions = '/assistant/sessions'
export function getAssistantSessions(signal: AbortSignal) {
  return apiRequest<{ items: AssistantSession[]; models: string[] }>(sessions, { signal })
}
export function createAssistantSession(title: string, signal?: AbortSignal) {
  return apiRequest<AssistantSession>(sessions, { method: 'POST', json: { title }, signal })
}
export function getAssistantSession(id: string, signal: AbortSignal) {
  return apiRequest<Transcript>(`${sessions}/${encodeURIComponent(id)}`, { signal })
}
type StreamHandlers = {
  onText: (text: string) => void
  onApproval: (approval: ApprovalRequired) => void
  onDisplay: (card: DisplayCard) => void
  onTool: (event: ToolStreamEvent) => void
}

function approvalCard(value: unknown): value is AssistantAction {
  if (typeof value !== 'object' || value === null) return false
  const card = value as AssistantAction
  return typeof card.tool_call_id === 'string' && typeof card.tool === 'string' && card.title === '批准请求'
    && typeof card.description === 'string' && typeof card.arguments === 'object' && card.arguments !== null
    && !Array.isArray(card.arguments)
}

function displayCard(value: unknown): value is DisplayCard {
  if (typeof value !== 'object' || value === null) return false
  const card = value as DisplayCard
  return typeof card.id === 'string' && typeof card.tool_call_id === 'string' && typeof card.title === 'string'
    && typeof card.description === 'string' && Array.isArray(card.fields) && card.fields.length > 0
    && card.fields.every((field) => field && typeof field.label === 'string' && typeof field.value === 'string'
      && typeof field.sensitive === 'boolean')
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
function toolCall(value: unknown): value is ToolCallPart {
  return record(value) && value.part_kind === 'tool-call' && typeof value.tool_name === 'string'
    && typeof value.tool_call_id === 'string' && !!value.tool_call_id
}
function toolResult(value: unknown): value is ToolResultPart {
  return record(value) && value.part_kind === 'tool-return' && typeof value.tool_name === 'string'
    && typeof value.tool_call_id === 'string' && !!value.tool_call_id
}

export async function streamAssistant(request: AssistantStreamRequest, handlers: StreamHandlers,
  signal?: AbortSignal, runnerId?: string): Promise<AssistantTurn> {
  const path = runnerId ? `/runners/${encodeURIComponent(runnerId)}/assistant/stream` : '/assistant/stream'
  const response = await apiStream(path, { method: 'POST', json: request, signal })
  if (!response.headers.get('content-type')?.startsWith('text/event-stream') || !response.body) {
    throw new ApiError(502, 'invalid_response', '服务未返回对话事件流')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let runId: string | undefined
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      let boundary: RegExpExecArray | null
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index)
        buffer = buffer.slice(boundary.index + boundary[0].length)
        const lines = frame.split(/\r?\n/)
        const event = lines.find((line) => line.startsWith('event:'))?.slice(6).trim()
        const json = lines.filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n')
        if (!json) continue
        const data = JSON.parse(json)
        if (!record(data)) throw new ApiError(502, 'invalid_response', '对话事件格式不合法')
        if (event === 'text_delta' && typeof data.text === 'string') handlers.onText(data.text)
        if (event === 'tool_call' || event === 'tool_result' || event === 'tool_deferred') {
          if (data.conversation_id !== request.conversation_id || typeof data.run_id !== 'string' || !data.run_id
            || (runId !== undefined && runId !== data.run_id)) throw new ApiError(502, 'invalid_response', '工具事件所属会话或运行不一致')
          runId = data.run_id
          const identity = { conversation_id: request.conversation_id, run_id: runId }
          if (event === 'tool_call') {
            if (!toolCall(data.part) || !(data.args_valid === null || typeof data.args_valid === 'boolean')) {
              throw new ApiError(502, 'invalid_response', '工具调用格式不合法')
            }
            handlers.onTool({ ...identity, type: event, part: data.part, args_valid: data.args_valid })
          } else if (event === 'tool_result') {
            if (!toolResult(data.part)) throw new ApiError(502, 'invalid_response', '工具结果格式不合法')
            handlers.onTool({ ...identity, type: event, part: data.part })
          } else {
            if (!record(data.requests) || !Array.isArray(data.requests.calls) || !Array.isArray(data.requests.approvals)
              || !data.requests.calls.every(toolCall) || !data.requests.approvals.every(toolCall)) {
              throw new ApiError(502, 'invalid_response', '工具等待请求格式不合法')
            }
            handlers.onTool({ ...identity, type: event, requests: { calls: data.requests.calls, approvals: data.requests.approvals } })
          }
        }
        if (event === 'approval_required') {
          if (typeof data.revision !== 'string' || !data.revision || !Array.isArray(data.cards)
            || !data.cards.every(approvalCard)) throw new ApiError(502, 'invalid_response', '批准请求格式不合法')
          handlers.onApproval({ revision: data.revision, cards: data.cards })
        }
        if (event === 'display') {
          if (!displayCard(data)) throw new ApiError(502, 'invalid_response', '展示卡片格式不合法')
          handlers.onDisplay(data)
          continue
        }
        if (event === 'error') throw new ApiError(502,
          typeof data.code === 'string' ? data.code : 'assistant_failed',
          typeof data.message === 'string' ? data.message : '助手执行失败')
        if (event === 'done') {
          if (typeof data.revision !== 'string' || !data.revision
            || (data.status !== 'completed' && data.status !== 'awaiting_approval')) throw new Error('Invalid assistant response')
          return { revision: data.revision, status: data.status }
        }
      }
      if (done) throw new ApiError(0, 'assistant_stream_interrupted', '连接中断，请查询会话状态后继续。')
    }
  } finally {
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}
