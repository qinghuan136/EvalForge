import { apiRequest } from '../../lib/api'

type RunnerConnection = {
  online: boolean
  phase: 'disconnected' | 'connected' | 'syncing' | 'ready'
  capabilities: string[]
}

export type Runner = {
  runner_id: string
  name: string
  enabled: boolean
  created_at: string
  updated_at: string
  last_seen_at: string | null
  connection: RunnerConnection
}

export type RunnerCreated = {
  runner: { runner_id: string; name: string }
  credential: { token: string; expires_at: string }
  connection: { websocket_url: string }
}

export function getRunner(id: string, signal: AbortSignal) {
  return apiRequest<Runner>(`/runners/${encodeURIComponent(id)}`, { signal })
}

export function createRunner(name: string) {
  return apiRequest<RunnerCreated>('/runners', { method: 'POST', json: { name } })
}

export function renameRunner(id: string, name: string) {
  return apiRequest<Runner>(`/runners/${encodeURIComponent(id)}`, { method: 'PATCH', json: { name } })
}

export function deleteRunner(id: string) {
  return apiRequest<void>(`/runners/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

type RunnerPage = {
  items: Runner[]
  page: number
  pagesize: number
  total: number
}

export function getRunners(page: number, signal: AbortSignal) {
  return apiRequest<RunnerPage>(`/runners?page=${page}&pagesize=20`, { signal })
}
