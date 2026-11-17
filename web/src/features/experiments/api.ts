import { apiRequest } from '../../lib/api'
import type { Page } from '../datasets/api'

export type EvaluationProgress = { expected: number; completed: number; errors: number }
export type Progress = {
  example_count: number; job_count: number; queued: number; assigned: number; running: number
  succeeded: number; failed: number; cancelled: number; unknown: number
  evaluations?: EvaluationProgress
}
export type Experiment = {
  experiment_id: number; dataset_id: number; dataset_version_id: number; name: string
  status: 'pending' | 'running' | 'cancelling' | 'completed' | 'failed' | 'cancelled'
  repetitions: number; progress: Progress; created_at: string
  description?: string | null
  execution_config?: { runner_id?: string; agent_target_id?: string; timeout_seconds?: number; stream_output?: boolean }
  evaluators?: Evaluator[]
}
export type Evaluator = { dataset_evaluator_id: number | null; name: string; evaluator_key: string; description: string | null }
export type EvaluatorDefinition = {
  key: string
  description: string
  input_schema: {
    properties: Record<string, { type?: string; default?: unknown }>
    required?: string[]
  }
}
export type EvaluatorConfig = {
  name: string
  evaluator_key: string
  input_mapping: { path_mapping: Record<string, string>; literal_mapping: Record<string, unknown> }
}
export type Job = {
  job_id: string; example_id: number; repetition_number: number; status: string
  duration_ms: number | null; http_status: number | null; evaluation_progress: EvaluationProgress
}
export type JobDetail = Job & {
  trace_id: string | null
  input: unknown; reference: unknown; metadata: unknown; output: unknown
  error: { code: string; message: string } | null; started_at: string | null; ended_at: string | null
  evaluations: { name: string; score: number | null; label: string | null; explanation: string | null; error: string | null }[]
}
export type CreateExperiment = {
  name: string; description?: string; dataset_version_id: number; repetitions: number; evaluators: EvaluatorConfig[]
  execution_config: { runner_id: string; agent_target_id: string; timeout_seconds: number; stream_output: boolean }
}

export function getExperiments(dataset: number, page: number, signal: AbortSignal) {
  return apiRequest<Page<Experiment>>(`/datasets/${dataset}/experiments?page=${page}&pagesize=20`, { signal })
}
export function getExperiment(id: number, signal: AbortSignal) {
  return apiRequest<Experiment>(`/experiments/${id}`, { signal })
}
export function getBuiltinEvaluators(signal: AbortSignal) {
  return apiRequest<EvaluatorDefinition[]>('/evaluators/builtin', { signal })
}
export function createExperiment(dataset: number, data: CreateExperiment) {
  const runner = encodeURIComponent(data.execution_config.runner_id)
  return apiRequest<Experiment>(`/runners/${runner}/datasets/${dataset}/experiments`, { method: 'POST', json: data })
}
export function cancelExperiment(id: number) {
  return apiRequest<Experiment>(`/experiments/${id}/cancel`, { method: 'POST' })
}
export function getJobs(id: number, page: number, status: string, signal: AbortSignal) {
  const params = new URLSearchParams({ page: String(page), pagesize: '20' })
  if (status) params.set('status', status)
  return apiRequest<Page<Job>>(`/experiments/${id}/jobs?${params}`, { signal })
}
export function getJob(id: number, job: string, signal: AbortSignal) {
  return apiRequest<JobDetail>(`/experiments/${id}/jobs/${encodeURIComponent(job)}`, { signal })
}
export function jobEventsUrl(id: number, job: string) {
  return `/api/v1/experiments/${id}/jobs/${encodeURIComponent(job)}/events`
}
export async function getAllPages<T>(path: string, signal: AbortSignal) {
  const items: T[] = []
  let page = 1
  while (true) {
    const response = await apiRequest<Page<T>>(`${path}?page=${page}&pagesize=100`, { signal })
    items.push(...response.items)
    if (!response.items.length || items.length >= response.total) return items
    page++
  }
}

export const statusLabels: Record<string, string> = {
  pending: '等待中', running: '运行中', cancelling: '取消中', completed: '已完成', failed: '失败',
  cancelled: '已取消', queued: '排队中', assigned: '已派发', succeeded: '成功', unknown: '未知',
}
export function processed(progress: Progress) {
  return progress.succeeded + progress.failed + progress.cancelled + progress.unknown
}
