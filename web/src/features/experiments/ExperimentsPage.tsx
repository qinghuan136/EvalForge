import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router'
import { privateKey } from '../../app/queryClient'
import { errorMessage } from '../../lib/api'
import type { Dataset } from '../datasets/api'
import { CreateDialog } from './CreateDialog'
import { getAllPages, getExperiments, processed, statusLabels } from './api'

export function ExperimentsPage({ userId }: { userId: number }) {
  const [params, setParams] = useSearchParams()
  const [creating, setCreating] = useState(false)
  const datasets = useQuery({ queryKey: [...privateKey, userId, 'all-datasets'], queryFn: ({ signal }) => getAllPages<Dataset>('/datasets', signal) })
  const requested = Number(params.get('dataset'))
  const datasetId = Number.isInteger(requested) && requested > 0 && requested <= 2147483647 ? requested : datasets.data?.[0]?.dataset_id
  const requestedPage = params.get('page')
  const page = requestedPage && /^[1-9]\d*$/.test(requestedPage) && Number(requestedPage) <= 2147483647 ? Number(requestedPage) : 1
  const experiments = useQuery({
    queryKey: [...privateKey, userId, 'experiments', datasetId, page],
    queryFn: ({ signal }) => getExperiments(datasetId!, page, signal),
    enabled: !!datasetId,
  })
  const lastPage = experiments.data ? Math.max(1, Math.ceil(experiments.data.total / experiments.data.pagesize)) : 1
  function setPage(value: number) {
    const next = new URLSearchParams(params)
    if (datasetId) next.set('dataset', String(datasetId))
    if (value === 1) next.delete('page'); else next.set('page', String(value))
    setParams(next)
  }
  return (
    <>
      <header className="page-heading"><h1>实验</h1><p>按数据集查看评测记录。</p>
        <button className="button button-primary page-action" disabled={!datasets.data?.length} onClick={() => setCreating(true)}>新建实验</button>
      </header>
      {datasets.isPending ? <p role="status">正在加载数据集…</p> : null}
      {datasets.isError ? <div><p className="error-message" role="alert">{errorMessage(datasets.error)}</p><button className="button" onClick={() => void datasets.refetch()}>重试</button></div> : null}
      {datasets.data?.length === 0 ? <p className="panel-message">还没有数据集，请先导入数据集。</p> : null}
      {datasets.data?.length ? <>
        <label className="field experiment-filter">数据集<select value={datasetId ?? ''} onChange={(event) => { const next = new URLSearchParams(params); next.set('dataset', event.target.value); next.delete('page'); setParams(next) }}>
          {datasetId && !datasets.data.some((item) => item.dataset_id === datasetId) ? <option value={datasetId}>数据集 #{datasetId}</option> : null}
          {datasets.data.map((item) => <option key={item.dataset_id} value={item.dataset_id}>{item.name}</option>)}
        </select></label>
        {experiments.isPending ? <p className="panel-message" role="status">正在加载实验…</p> : null}
        {experiments.isError ? <div className="panel-message"><p className="error-message" role="alert">{errorMessage(experiments.error)}</p><button className="button" onClick={() => void experiments.refetch()}>重试</button></div> : null}
        {experiments.data?.items.length ? <div className="table-scroll"><table className="experiment-table"><thead><tr><th>实验名称</th><th>版本</th><th>状态</th><th>Job 进度</th><th>创建时间</th></tr></thead><tbody>
          {experiments.data.items.map((item) => <tr key={item.experiment_id}><th scope="row"><Link to={`/experiments/${item.experiment_id}`} state={{ returnTo: '/experiments?' + params.toString() }}>{item.name}</Link><small>EXP-{item.experiment_id}</small></th><td>#{item.dataset_version_id}</td><td>{statusLabels[item.status] ?? item.status}</td><td>{processed(item.progress)} / {item.progress.job_count}</td><td>{new Date(item.created_at).toLocaleString()}</td></tr>)}
        </tbody></table></div> : experiments.data ? <p className="panel-message">此数据集暂无实验。</p> : null}
        {experiments.data ? <div className="list-footer"><nav className="pagination" aria-label="实验分页">
          <button className="button button-quiet" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button>
          <span>第 {page} / {lastPage} 页</span><button className="button button-quiet" disabled={page >= lastPage} onClick={() => setPage(page + 1)}>下一页</button>
        </nav><span>共 {experiments.data.total} 个实验</span></div> : null}
      </> : null}
      {creating && datasets.data?.length && datasetId ? <CreateDialog userId={userId} datasets={datasets.data} initialId={datasets.data.some((d) => d.dataset_id === datasetId) ? datasetId : datasets.data[0].dataset_id} onClose={() => setCreating(false)} /> : null}
    </>
  )
}
