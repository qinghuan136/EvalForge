import { apiRequest } from '../../lib/api'
import type { PageContext } from './pageContext'

export type AssistantSession = { id: number; title: string; version: number }
export type AssistantMessage = {
  id: number
  role: 'user' | 'assistant'
  text: string
  model_name: string | null
  source_experiment_id: number | null
  page_context?: PageContext | null
}
export type Transcript = AssistantSession & { messages: AssistantMessage[] }

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

export function sendAssistantMessage(id: number, expected_version: number, message: string,
  model_name: string, page_context?: PageContext) {
  return apiRequest<{ version: number; messages: AssistantMessage[] }>(`${sessions}/${id}/chat`, {
    method: 'POST', json: { page_context, expected_version, message, model_name },
  })
}
