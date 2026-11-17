import { useMemo, useState } from 'react'
import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { Link, useLocation, useParams } from 'react-router'
import { privateKey } from '../../app/queryClient'
import { ApiError, errorMessage } from '../../lib/api'
import { getSpan, getTrace, traceIdPattern } from './api'
import type { SpanDetail } from './api'
import { displayDuration, displayTime, jobReturnTo, statusLabel } from './display'
import { TraceTree } from './TraceTree'

const tabs = ['基础信息', '属性', '事件', '链接'] as const
type SpanTab = typeof tabs[number]

function RawData({ data }: { data: unknown }) {
  return <pre className="trace-raw">{JSON.stringify(data, null, 2)}</pre>
}

function SpanInfo({ span }: { span: SpanDetail }) {
  const fields = [
    ['Span ID', span.span_id], ['父 Span ID', span.parent_span_id ?? '—'],
    ['名称', span.name], ['OTLP Kind', span.kind], ['状态码', span.status_code ?? '—'],
    ['状态说明', span.status_message ?? '—'], ['开始时间（ns）', span.start_time_unix_nano],
    ['结束时间（ns）', span.end_time_unix_nano], ['Trace State', span.trace_state || '—'], ['Flags', span.flags],
    ['丢弃的属性 / 事件 / 链接', `${span.dropped_attributes_count} / ${span.dropped_events_count} / ${span.dropped_links_count}`],
  ] as const
  return <>
    <dl className="detail-list">{fields.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    <details className="trace-extra"><summary>Resource</summary><RawData data={span.resource} /></details>
    <details className="trace-extra"><summary>Instrumentation Scope</summary><RawData data={span.instrumentation_scope} /></details>
  </>
}

function SpanInspector({ userId, traceId, spanId }: { userId: number; traceId: string; spanId: string }) {
  const [tab, setTab] = useState<SpanTab>('基础信息')
  const details = useQuery({
    queryKey: [...privateKey, userId, 'span', traceId, spanId],
    queryFn: ({ signal }) => getSpan(traceId, spanId, signal),
  })
  const span = details.data
  return <section className="trace-inspector" aria-label="Span 详情">
    <header className="trace-panel-head">
      <h2>{span?.name ?? 'Span 详情'}</h2>
      {span ? <p className="muted">{statusLabel(span.status_code)} · {displayDuration(span.start_time_unix_nano, span.end_time_unix_nano)}</p> : null}
    </header>
    {details.isPending ? <p className="trace-panel-body" role="status">正在加载 Span…</p> : null}
    {details.isError ? <div className="trace-panel-body"><p className="error-message" role="alert">{errorMessage(details.error)}</p>
      <button className="button" onClick={() => void details.refetch()}>重试</button></div> : null}
    {span ? <>
      <nav className="trace-tabs" aria-label="Span 字段">
        {tabs.map((item) => <button key={item} type="button" aria-pressed={tab === item} onClick={() => setTab(item)}>{item}</button>)}
      </nav>
      <div className="trace-panel-body">
        {tab === '基础信息' ? <SpanInfo span={span} /> : null}
        {tab === '属性' ? span.attributes.length ? <dl className="trace-attributes">{span.attributes.map(({ key, value }, index) =>
          <div key={index}><dt>{key}</dt><dd><RawData data={value ?? null} /></dd></div>)}</dl> : <p className="muted">没有上报属性。</p> : null}
        {tab === '事件' ? span.events.length ? <RawData data={span.events} /> : <p className="muted">没有上报事件。</p> : null}
        {tab === '链接' ? span.links.length ? <RawData data={span.links} /> : <p className="muted">没有上报链接。</p> : null}
      </div>
    </> : null}
  </section>
}

function TraceDetail({ userId, traceId }: { userId: number; traceId: string }) {
  const [selected, setSelected] = useState<string | null>(null)
  const [copyMessage, setCopyMessage] = useState('')
  const location = useLocation()
  const back = jobReturnTo(location.state)
  const listSearch = (location.state as { traceListSearch?: unknown } | null)?.traceListSearch
  const listPath = '/traces' + (typeof listSearch === 'string' && listSearch.startsWith('?') ? listSearch : '')
  const query = useInfiniteQuery({
    queryKey: [...privateKey, userId, 'trace', traceId],
    queryFn: ({ signal, pageParam }) => getTrace(traceId, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_span_id ?? undefined,
  })
  const pages = query.data?.pages
  const spans = useMemo(() => pages?.flatMap((page) => page.spans) ?? [], [pages])
  const trace = pages?.[pages.length - 1]
  const selectedId = selected ?? (spans.some((span) => span.span_id === trace?.root_span_id)
    ? trace?.root_span_id : spans[0]?.span_id) ?? null
  async function copyTraceId() {
    try {
      await navigator.clipboard.writeText(traceId)
      setCopyMessage('已复制')
    } catch {
      setCopyMessage('无法自动复制，请选中 ID 复制。')
    }
  }
  return <>
    <header className="page-heading trace-detail-heading">
      <h1>{trace?.title ?? '调用链详情'}</h1>
      <div className="trace-page-actions page-action">
        <Link className="button" to={listPath} state={{ jobReturnTo: back }}>返回调用链</Link>
        {back ? <Link className="button" to={back}>返回 Job</Link> : null}
      </div>
      <div className="trace-id-line"><code>{traceId}</code><button className="button button-quiet" onClick={() => void copyTraceId()}>复制</button>
        <span className="muted" role="status">{copyMessage}</span></div>
    </header>
    {query.isPending ? <p role="status">正在加载调用链…</p> : null}
    {query.isError && !trace ? <div><p role="alert" className="error-message">{query.error instanceof ApiError && query.error.status === 404
      ? '未找到这条调用链。如果 Agent 刚执行完，可以稍后重试。' : errorMessage(query.error)}</p>
      <button className="button" onClick={() => void query.refetch()}>重试</button></div> : null}
    {trace ? <>
      <dl className="trace-meta">
        <div><dt>服务</dt><dd>{trace.service_name ?? '—'}</dd></div>
        <div><dt>开始时间</dt><dd>{displayTime(trace.start_time_unix_nano)}</dd></div>
        <div><dt>调用链耗时</dt><dd>{displayDuration(trace.start_time_unix_nano, trace.end_time_unix_nano)}</dd></div>
        <div><dt>状态</dt><dd>{trace.root_span_id ? statusLabel(trace.status_code) : '根 Span 未收到'}</dd></div>
      </dl>
      <div className="trace-layout">
        <section className="trace-inspector" aria-label="调用树和耗时">
          <header className="trace-panel-head"><h2>调用树</h2><span className="muted">{spans.length} 个 Span</span></header>
          {spans.length ? <TraceTree spans={spans} selected={selectedId} onSelect={setSelected}
            start={trace.start_time_unix_nano} end={trace.end_time_unix_nano} hasMore={query.hasNextPage} />
            : <p className="trace-panel-body muted">没有可展示的 Span。</p>}
          {query.hasNextPage ? <div className="trace-panel-body"><button className="button"
            disabled={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>
            {query.isFetchingNextPage ? '加载中…' : '加载更多 Span'}</button>
            {query.isFetchNextPageError ? <p className="error-message" role="alert">{errorMessage(query.error)}</p> : null}</div> : null}
        </section>
        {selectedId ? <SpanInspector userId={userId} traceId={traceId} spanId={selectedId} /> : null}
      </div>
    </> : null}
  </>
}

export function TraceDetailPage({ userId }: { userId: number }) {
  const traceId = (useParams().traceId ?? '').toLowerCase()
  if (!traceIdPattern.test(traceId)) return <p className="error-message">Trace ID 格式不正确。</p>
  return <TraceDetail key={traceId} userId={userId} traceId={traceId} />
}
