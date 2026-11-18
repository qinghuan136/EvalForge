import { ApiError, apiRequest, apiStream } from '../../lib/api'
import type { RunnerCreated } from '../runners/api'
import type { PageContext } from './pageContext'

export type AssistantSession = { id: number; title: string; version: number }
export type AssistantMessage = {
  id: number; role: 'user' | 'assistant'; text: string; model_name: string | null
  source_experiment_id: number | null; page_context?: PageContext | null
}
export type AssistantAction = { tool_call_id: string; tool: string; arguments: Record<string, unknown> }
export type Transcript = AssistantSession & {
  messages: AssistantMessage[]; status: string; actions: AssistantAction[]
  credential_call_ids: string[]; error: { code: string; message: string } | null
}
export type AssistantTurn = {
  version: number; status: 'completed' | 'awaiting_approval'
  messages: AssistantMessage[]; actions: AssistantAction[]
}
export type AssistantStreamRequest = { session_id: number; expected_version: number } & (
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
export function getAssistantSession(id: number, signal: AbortSignal) {
  return apiRequest<Transcript>(`${sessions}/${id}`, { signal })
}
export function takeRunnerCredential(id: number, callId: string) {
  return apiRequest<RunnerCreated>(`${sessions}/${id}/runner-credentials/${encodeURIComponent(callId)}`, { method: 'POST' })
}

export async function streamAssistant(request: AssistantStreamRequest, onText: (text: string) => void): Promise<AssistantTurn> {
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
        if (event === 'text_delta' && typeof data.text === 'string') onText(data.text)
        if (event === 'error') throw new ApiError(502, data.code ?? 'assistant_failed', data.message ?? '助手执行失败')
        // Cards become actionable only after done confirms the saved version.
        if (event === 'done') {
          if (!Number.isInteger(data.version) || !Array.isArray(data.messages) || !Array.isArray(data.actions)
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
