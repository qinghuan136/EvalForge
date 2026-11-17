import { apiRequest } from '../../lib/api'
import type { Page } from '../datasets/api'

export const traceIdPattern = /^[0-9a-f]{32}$/

type TimeRange = { start_time_unix_nano: string; end_time_unix_nano: string }

export type TraceSummary = TimeRange & {
  trace_id: string; title: string; service_name: string | null
  root_span_id: string | null; status_code: number | null
}

export type SpanSummary = TimeRange & {
  span_id: string; parent_span_id: string | null; name: string; kind: number; status_code: number | null
}

export type TraceDetail = TraceSummary & { spans: SpanSummary[]; next_span_id: string | null }

export type SpanDetail = SpanSummary & {
  trace_id: string; status_message: string | null; trace_state: string; flags: number
  attributes: { key: string; value?: unknown }[]; events: unknown[]; links: unknown[]
  resource: object | null; instrumentation_scope: object | null
  dropped_attributes_count: number; dropped_events_count: number; dropped_links_count: number
}

export function getTraces(page: number, traceId: string, signal: AbortSignal) {
  const params = new URLSearchParams({ page: String(page), pagesize: '20' })
  if (traceId) params.set('trace_id', traceId)
  return apiRequest<Page<TraceSummary>>(`/traces?${params}`, { signal })
}

export function getTrace(traceId: string, afterSpanId: string | null, signal: AbortSignal) {
  const query = afterSpanId ? `?after_span_id=${encodeURIComponent(afterSpanId)}` : ''
  return apiRequest<TraceDetail>(`/traces/${encodeURIComponent(traceId)}${query}`, { signal })
}

export function getSpan(traceId: string, spanId: string, signal: AbortSignal) {
  return apiRequest<SpanDetail>(`/traces/${encodeURIComponent(traceId)}/spans/${encodeURIComponent(spanId)}`, { signal })
}
