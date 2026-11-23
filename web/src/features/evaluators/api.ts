import { apiRequest } from '../../lib/api'
import type { Page } from '../datasets/api'

export type Version = { id: number; version_number: number; created_at: string }
export type VersionDetail = Version & { source_code: string }
export type CodeEvaluator = {
  id: number
  name: string
  description: string | null
  latest_version: Version | null
  archived_at: string | null
}
export type CodeEvaluatorDetail = CodeEvaluator & { draft_source_code: string | null }
export type PreviewContext = {
  input: Record<string, unknown>
  reference: Record<string, unknown>
  output: unknown
  metadata: Record<string, unknown>
}
type PreviewResponse = { simulated?: boolean; duration_ms: number; stdout?: string }
export type Preview = PreviewResponse & (
  { status: 'succeeded'; result: { score: number | null; label: string | null; explanation: string | null } }
  | { status: 'failed'; result: null; error: { code: string; message: string } }
)

const root = '/evaluators/code'

export function listCodeEvaluators(page: number, includeArchived: boolean, signal: AbortSignal) {
  const params = new URLSearchParams({ page: String(page), pagesize: '20' })
  if (includeArchived) params.set('include_archived', 'true')
  return apiRequest<Page<CodeEvaluator>>(`${root}?${params}`, { signal })
}

export function createCodeEvaluator(data: { name: string; description: string | null }) {
  return apiRequest<CodeEvaluatorDetail>(root, { method: 'POST', json: data })
}

export function getCodeEvaluator(id: number, signal: AbortSignal) {
  return apiRequest<CodeEvaluatorDetail>(`${root}/${id}`, { signal })
}

export function saveDraft(id: number, source_code: string) {
  return apiRequest<{ draft_source_code: string }>(`${root}/${id}/draft`, { method: 'PATCH', json: { source_code } })
}

export function archiveCodeEvaluator(id: number) {
  return apiRequest<CodeEvaluatorDetail>(`${root}/${id}/archive`, { method: 'POST' })
}

export function previewCode(source_code: string, context: PreviewContext) {
  return apiRequest<Preview>(`${root}/preview`, { method: 'POST', json: { source_code, context } })
}

export function publishVersion(id: number) {
  return apiRequest<Version>(`${root}/${id}/versions`, { method: 'POST' })
}

export function listVersions(id: number, page: number, signal: AbortSignal) {
  return apiRequest<Page<Version>>(`${root}/${id}/versions?page=${page}&pagesize=20`, { signal })
}

export function getVersion(id: number, versionId: number, signal: AbortSignal) {
  return apiRequest<VersionDetail>(`${root}/${id}/versions/${versionId}`, { signal })
}
