import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, Navigate, useSearchParams } from 'react-router'
import { privateKey } from '../../app/queryClient'
import { errorMessage } from '../../lib/api'
import { getRunners } from './api'
import { RunnerDialog } from './RunnerDialog'

const connectionLabels = {
  disconnected: '离线',
  connected: '已连接',
  syncing: '同步中',
  ready: '已就绪',
}

export function RunnersPage({ userId }: { userId: number }) {
  const [dialog, setDialog] = useState<string | null | undefined>(undefined)
  const [searchParams] = useSearchParams()
  const requestedPage = searchParams.get('page')
  const parsedPage = Number(requestedPage)
  const page = requestedPage && /^[1-9]\d*$/.test(requestedPage) && parsedPage <= 2147483647
    ? parsedPage
    : 1
  const runners = useQuery({
    queryKey: [...privateKey, userId, 'runners', page],
    queryFn: ({ signal }) => getRunners(page, signal),
  })

  function pageLink(targetPage: number) {
    const params = new URLSearchParams(searchParams)
    if (targetPage === 1) params.delete('page')
    else params.set('page', String(targetPage))
    return { pathname: '/runners', search: params.toString() }
  }

  const data = runners.data
  const lastPage = data ? Math.max(1, Math.ceil(data.total / data.pagesize)) : 1

  if (data && page > lastPage) return <Navigate replace to={pageLink(lastPage)} />

  return (
    <>
      <header className="page-heading">
        <h1>Runners</h1>
        <p>查看客户端连接状态。</p>
        <button className="button button-primary page-action" onClick={() => setDialog(null)}>创建 Runner</button>
      </header>
      <section className="runner-panel" aria-label="Runner 列表" aria-busy={runners.isFetching}>
        {runners.isPending ? <p className="panel-message" role="status">正在加载 Runner…</p> : null}
        {runners.isError ? (
          <div className="panel-message">
            <p className="error-message" role="alert">{errorMessage(runners.error)}</p>
            <button className="button" disabled={runners.isFetching} onClick={() => void runners.refetch()}>
              {runners.isFetching ? '重试中…' : '重试'}
            </button>
          </div>
        ) : null}
        {data?.items.length ? (
          <table className="runner-table">
            <thead>
              <tr><th scope="col">Runner</th><th scope="col">连接状态</th></tr>
            </thead>
            <tbody>
              {data.items.map((runner) => {
                const status = !runner.enabled ? 'disabled'
                  : runner.connection.online ? runner.connection.phase : 'disconnected'
                return (
                  <tr key={runner.runner_id}>
                    <th scope="row" className="runner-identity">
                      <button className="text-link" onClick={() => setDialog(runner.runner_id)}>{runner.name}</button>
                      <span>{runner.runner_id}</span>
                    </th>
                    <td className="runner-status" data-status={status}>
                      {status === 'disabled' ? '已停用' : connectionLabels[status]}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        ) : data ? <p className="panel-message">还没有 Runner。</p> : null}
        {data ? (
          <div className="list-footer">
            {lastPage > 1 ? (
              <nav className="pagination" aria-label="Runner 分页">
                {page > 1
                  ? <Link className="button button-quiet" to={pageLink(page - 1)}>上一页</Link>
                  : <button className="button button-quiet" disabled>上一页</button>}
                <span aria-current="page">第 {page} / {lastPage} 页</span>
                {page < lastPage
                  ? <Link className="button button-quiet" to={pageLink(page + 1)}>下一页</Link>
                  : <button className="button button-quiet" disabled>下一页</button>}
              </nav>
            ) : null}
            <span className="runner-total">共 {data.total} 个 Runner</span>
          </div>
        ) : null}
      </section>
      {dialog !== undefined ? <RunnerDialog userId={userId} id={dialog} onClose={() => setDialog(undefined)} /> : null}
    </>
  )
}
