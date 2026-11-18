import { ApiError, apiRequest, apiStream } from '../../lib/api'
import type { PageContext } from './pageContext'

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
export function createAssistantSession(title: string) {
  return apiRequest<AssistantSession>(sessions, { method: 'POST', json: { title } })
}
export function getAssistantSession(id: string, signal: AbortSignal) {
  return apiRequest<Transcript>(`${sessions}/${encodeURIComponent(id)}`, { signal })
}
type StreamHandlers = {
  onText: (text: string) => void
  onApproval: (approval: ApprovalRequired) => void
  onDisplay: (card: DisplayCard) => void
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

export async function streamAssistant(request: AssistantStreamRequest, handlers: StreamHandlers): Promise<AssistantTurn> {
  const response = await apiStream('/assistant/stream', { method: 'POST', json: request })
  if (!response.headers.get('content-type')?.startsWith('text/event-stream') || !response.body) {
    throw new ApiError(502, 'invalid_response', '服务未返回对话事件流')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
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
        if (event === 'text_delta' && typeof data.text === 'string') handlers.onText(data.text)
        if (event === 'approval_required') {
          if (typeof data.revision !== 'string' || !data.revision || !Array.isArray(data.cards)
            || !data.cards.every(approvalCard)) throw new ApiError(502, 'invalid_response', '批准请求格式不合法')
          handlers.onApproval(data as ApprovalRequired)
        }
        if (event === 'display') {
          if (!displayCard(data)) throw new ApiError(502, 'invalid_response', '展示卡片格式不合法')
          handlers.onDisplay(data)
        }
        if (event === 'error') throw new ApiError(502, data.code ?? 'assistant_failed', data.message ?? '助手执行失败')
        if (event === 'done') {
          if (typeof data.revision !== 'string' || !data.revision
            || !['completed', 'awaiting_approval'].includes(data.status)) throw new Error('Invalid assistant response')
          return data as AssistantTurn
        }
      }
      if (done) throw new ApiError(0, 'assistant_stream_interrupted', '连接中断，请查询会话状态后继续。')
    }
  } finally {
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}
