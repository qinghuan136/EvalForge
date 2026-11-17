import { useState } from 'react'
import type { FormEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useLocation, useSearchParams } from 'react-router'
import { privateKey } from '../../app/queryClient'
import { errorMessage } from '../../lib/api'
import { getTraces, traceIdPattern } from './api'
import { displayDuration, displayTime, jobReturnTo, statusLabel } from './display'

function TraceFilter({ traceId, apply }: { traceId: string; apply: (id: string) => void }) {
  const [draft, setDraft] = useState(traceId)
  const [invalid, setInvalid] = useState(false)
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const id = draft.trim().toLowerCase()
    if (id && !traceIdPattern.test(id)) {
      setInvalid(true)
      return
    }
    setInvalid(false)
    setDraft(id)
    apply(id)
  }

  return <form className="trace-filter" onSubmit={submit}>
    <label className="field">Trace ID
      <input value={draft} maxLength={32} placeholder="输入完整 Trace ID" spellCheck={false}
        onChange={(event) => { setDraft(event.target.value); setInvalid(false) }} />
    </label>
    <button className="button">查找</button>
    {traceId ? <button className="button button-quiet" type="button" onClick={() => apply('')}>清除筛选</button> : null}
    {invalid ? <p className="error-message" role="alert">请输入 32 位十六进制 Trace ID。</p> : null}
  </form>
}

export function TracesPage({ userId }: { userId: number }) {
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  const traceId = (params.get('trace_id') ?? '').toLowerCase()
  const isValid = !traceId || traceIdPattern.test(traceId)
  const candidate = params.get('page') ?? ''
  const page = /^[1-9]\d*$/.test(candidate) && Number(candidate) <= 2147483647 ? Number(candidate) : 1
  const back = jobReturnTo(location.state)
  const list = useQuery({
    queryKey: [...privateKey, userId, 'traces', page, traceId],
    queryFn: ({ signal }) => getTraces(page, traceId, signal),
    enabled: isValid,
  })
  const data = list.data
  const lastPage = data ? Math.max(1, Math.ceil(data.total / data.pagesize)) : 1
  function update(id: string, nextPage = 1) {
    const next = new URLSearchParams()
    if (id) next.set('trace_id', id)
    if (nextPage > 1) next.set('page', String(nextPage))
    setParams(next, { state: { jobReturnTo: back } })
  }

  return <>
    <header className="page-heading">
      <h1>调用链</h1><p>查看 Agent 上报的调用及耗时。</p>
      <div className="trace-page-actions page-action">
        {back ? <Link className="button" to={back}>返回 Job</Link> : null}
        <button className="button" disabled={!isValid || list.isFetching} onClick={() => void list.refetch()}>刷新</button>
      </div>
    </header>
    <TraceFilter key={traceId} traceId={traceId} apply={update} />
    {!isValid ? <p className="error-message" role="alert">Trace ID 格式不正确。</p> : null}
    {isValid && list.isPending ? <p className="panel-message" role="status">正在加载调用链…</p> : null}
    {list.isError ? <div className="panel-message"><p className="error-message" role="alert">{errorMessage(list.error)}</p>
      <button className="button" onClick={() => void list.refetch()}>重试</button></div> : null}
    {data?.items.length ? <div className="table-scroll"><table className="experiment-table trace-table">
      <thead><tr><th>调用名称</th><th>服务</th><th>状态</th><th>开始时间</th><th>耗时</th></tr></thead>
      <tbody>{data.items.map((trace) => <tr key={trace.trace_id} className={traceId === trace.trace_id ? 'trace-match' : undefined}>
        <th scope="row">
          <Link to={`/traces/${trace.trace_id}`} state={{ traceListSearch: location.search, jobReturnTo: back }}>{trace.title}</Link>
          <small><code title={trace.trace_id}>{trace.trace_id.slice(0, 8)}…{trace.trace_id.slice(-6)}</code>
            {trace.root_span_id === null ? ' · 根 Span 未收到' : ''}</small>
        </th>
        <td>{trace.service_name ?? '—'}</td>
        <td className={trace.status_code === 2 ? 'error-message' : undefined}>{trace.root_span_id ? statusLabel(trace.status_code) : '—'}</td>
        <td className="trace-time">{displayTime(trace.start_time_unix_nano)}</td>
        <td className="trace-time">{displayDuration(trace.start_time_unix_nano, trace.end_time_unix_nano)}</td>
      </tr>)}</tbody>
    </table></div> : data ? <p className="panel-message" role="status">
      {traceId ? '未找到这条调用链。如果 Agent 刚执行完，可以稍后刷新。' : '还没有调用链。Agent 上报后会显示在这里。'}
    </p> : null}
    {data ? <div className="list-footer"><nav className="pagination" aria-label="调用链分页">
      <button className="button button-quiet" disabled={page <= 1} onClick={() => update(traceId, page - 1)}>上一页</button>
      <span>第 {page} / {lastPage} 页</span>
      <button className="button button-quiet" disabled={page >= lastPage} onClick={() => update(traceId, page + 1)}>下一页</button>
    </nav><span>共 {data.total} 条调用链</span></div> : null}
  </>
}
