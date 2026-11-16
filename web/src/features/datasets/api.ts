import { apiRequest } from '../../lib/api'

export type Dataset = {
  dataset_id: number
  name: string
  description: string | null
  latest_version_id: number | null
  example_count: number
}

export type Page<T> = { items: T[]; page: number; pagesize: number; total: number }
export type Example = { example_id: number; input: object; output: object; metadata: object }

export function getDatasets(page: number, signal: AbortSignal) {
  return apiRequest<Page<Dataset>>(`/datasets?page=${page}&pagesize=20`, { signal })
}

export function getDataset(id: number, signal: AbortSignal) {
  return apiRequest<Dataset>(`/datasets/${id}`, { signal })
}

export function getExamples(id: number, page: number, signal: AbortSignal, versionId?: number) {
  return apiRequest<Page<Example> & { dataset_version_id: number | null }>(
    `/datasets/${id}/examples?page=${page}&pagesize=10${versionId ? `&version_id=${versionId}` : ''}`, { signal },
  )
}

export function importDataset(body: FormData) {
  return apiRequest<{ dataset_id: number; dataset_version_id: number; example_count: number }>(
    '/datasets/import', { method: 'POST', body },
  )
}
