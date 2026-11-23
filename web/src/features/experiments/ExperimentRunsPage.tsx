import { useQuery } from '@tanstack/react-query'
import { Link, useLocation, useSearchParams } from 'react-router'
import { privateKey } from '../../app/queryClient'
import { errorMessage } from '../../lib/api'
import { getAllPages, getRuns, processed, runStatuses, statusLabels } from './api'
import type { ExperimentConfig } from './api'
import { configListPath, configPath, positiveId, runDetailPath, runListPath } from './routes'

export function ExperimentRunsPage({ userId }: { userId: number }) {
  const location = useLocation()
  const [params, setParams] = useSearchParams()
  const group = params.get('group') || undefined
  const version = positiveId(params.get('version'))
  const rawStatus = params.get('status')
  const status = runStatuses.find((value) => value === rawStatus)
  const page = positiveId(params.get('page')) ?? 1
  const filters = { group, version, status, page }
  const runs = useQuery({
    queryKey: [...privateKey, userId, 'experiment-runs', filters],
    queryFn: ({ signal }) => getRuns(filters, signal),
    refetchInterval: (query) => query.state.data?.items.some((item) =>
      ['pending', 'running', 'cancelling'].includes(item.status)) ? 3000 : false,
  })
  const configs = useQuery({
    queryKey: [...privateKey, userId, 'experiment-configs', 'choices'],
    queryFn: ({ signal }) => getAllPages<ExperimentConfig>('/experiment-configs', signal),
  })
  const data = runs.data
  const selectedConfig = configs.data?.find((item) => item.config_group_id === group)
  const returnTo = location.pathname + location.search

  function goToPage(targetPage: number) {
    const next = new URLSearchParams(params)
    if (targetPage > 1) next.set('page', String(targetPage))
    else next.delete('page')
    setParams(next)
  }

  return <>
    <header className="page-heading"><h1>实验 Run</h1></header>
    <form key={JSON.stringify([group, version, status])} className="config-controls run-filters" aria-label="筛选实验 Run"
      onSubmit={(event) => {
        event.preventDefault()
        const form = new FormData(event.currentTarget)
        const next = new URLSearchParams()
        for (const field of ['group', 'version', 'status']) {
          const value = form.get(field)
          if (typeof value === 'string' && value) next.set(field, value)
        }
        setParams(next)
      }}>
      <label className="field">配置组<select name="group" defaultValue={group ?? ''}>
        <option value="">全部配置组</option>
        {group && <option value={group}>{selectedConfig?.name ?? group}</option>}
        {configs.data?.filter((item) => item.config_group_id !== group).map((item) =>
          <option key={item.config_group_id} value={item.config_group_id}>{item.name}</option>)}
      </select></label>
      <label className="field">配置版本<input name="version" type="number" min="1" max="2147483647"
        placeholder="全部版本" defaultValue={version} /></label>
      <label className="field">状态<select name="status" defaultValue={status ?? ''}>
        <option value="">全部状态</option>
        {runStatuses.map((value) => <option key={value} value={value}>{statusLabels[value]}</option>)}
      </select></label>
      <button className="button button-primary">筛选</button>
      {(group || version || status) && <Link className="button button-quiet" to={runListPath}>清除筛选</Link>}
    </form>
    {configs.isError && <p className="error-message" role="alert">加载配置组失败。{errorMessage(configs.error)}{' '}
      <button className="button" onClick={() => void configs.refetch()}>重试配置组</button></p>}
    {runs.isPending && <p className="panel-message" role="status">正在加载 Run…</p>}
    {runs.isError && <div className="panel-message">
      <p className="error-message" role="alert">{errorMessage(runs.error)}</p>
      <button className="button" onClick={() => void runs.refetch()}>重试</button>
    </div>}
    {data && <>
      {data.items.length ? <div className="table-scroll"><table className="experiment-table" aria-label="实验 Run 列表">
        <thead><tr><th>Run</th><th>配置版本</th><th>数据集版本</th><th>状态</th><th>Job 进度</th><th>创建时间</th></tr></thead>
        <tbody>{data.items.map((item) => <tr key={item.experiment_id}>
          <th scope="row"><Link to={runDetailPath(item.experiment_id)} state={{ returnTo }}>{item.name}</Link>
            <small>Run #{item.experiment_id}</small></th>
          <td><Link to={configPath(item.config_group_id)} aria-label={`查看 ${item.name} 的配置组`}>v{item.config_version}</Link></td>
          <td>#{item.dataset_version_id}</td><td>{statusLabels[item.status]}</td>
          <td>{processed(item.progress)} / {item.progress.job_count}</td>
          <td>{new Date(item.created_at).toLocaleString()}</td>
        </tr>)}</tbody>
      </table></div> : <p className="panel-message">{group || version || status ? '暂无匹配的 Run。' : '暂无 Run。'}{' '}
        {!group && !version && !status && <Link to={configListPath}>前往实验配置</Link>}</p>}
      <div className="list-footer"><nav className="pagination" aria-label="Run 分页">
        <button className="button button-quiet" disabled={page <= 1} onClick={() => goToPage(page - 1)}>上一页</button>
        <span>第 {page} / {Math.max(1, Math.ceil(data.total / data.pagesize))} 页</span>
        <button className="button button-quiet" disabled={page * data.pagesize >= data.total}
          onClick={() => goToPage(page + 1)}>下一页</button>
      </nav><span>共 {data.total} 个 Run</span></div>
    </>}
  </>
}
