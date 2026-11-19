import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router'
import { privateKey } from '../../app/queryClient'
import { errorMessage } from '../../lib/api'
import type { Dataset } from '../datasets/api'
import { ConfigDialog } from './ConfigDialog'
import { RunDialog } from './RunDialog'
import { getAllPages, getConfig, getConfigs, getRuns, processed, statusLabels } from './api'
import type { ExperimentConfig } from './api'

function positive(value: string | null) {
  if (!value || !/^[1-9]\d*$/.test(value) || Number(value) > 2147483647) return undefined
  return Number(value)
}

export function ExperimentsPage({ userId }: { userId: number }) {
  const [params, setParams] = useSearchParams()
  const [form, setForm] = useState<'new' | ExperimentConfig>()
  const [run, setRun] = useState<ExperimentConfig>()
  const group = params.get('group') ?? ''
  const datasetId = positive(params.get('dataset'))
  const page = positive(params.get('page')) ?? 1
  const version = positive(params.get('version'))
  const key = [...privateKey, userId, 'experiment-configs']
  const datasets = useQuery({ queryKey: [...privateKey, userId, 'all-datasets'],
    queryFn: ({ signal }) => getAllPages<Dataset>('/datasets', signal) })
  const configs = useQuery({ queryKey: [...key, 'list', datasetId, page],
    queryFn: ({ signal }) => getConfigs(datasetId, page, signal), enabled: !group })
  const config = useQuery({ queryKey: [...key, group], queryFn: ({ signal }) => getConfig(group, signal), enabled: !!group })
  const runs = useQuery({ queryKey: [...privateKey, userId, 'experiment-runs', group, page, version],
    queryFn: ({ signal }) => getRuns(group, page, version, signal), enabled: !!group,
    refetchInterval: (query) => query.state.data?.items.some((item) =>
      ['pending', 'running', 'cancelling'].includes(item.status)) ? 3000 : false })
  const datasetName = new Map(datasets.data?.map((item) => [item.dataset_id, item.name]))
  const selected = group ? runs : configs
  const loadError = selected.error || (group && config.error) || datasets.error
  const latest = config.data
  const startDatasetId = datasetId && datasetName.has(datasetId) ? datasetId : datasets.data?.[0]?.dataset_id

  function update(name: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(name, value)
    else next.delete(name)
    if (name !== 'page') next.delete('page')
    setParams(next)
  }

  return <>
    <header className="page-heading">
      <h1>{group ? latest?.name ?? '运行历史' : '实验配置'}</h1>
      <p>{group ? (latest ? `配置最新版 v${latest.version} · ${latest.description ?? '运行历史'}` : '运行历史') : '保存可复用的配置，按需启动 Run。'}</p>
      {group ? <Link className="button page-action" to={datasetId ? `/experiments?dataset=${datasetId}` : '/experiments'}>返回配置列表</Link>
        : <button className="button button-primary page-action" disabled={!datasets.data?.length}
            onClick={() => setForm('new')}>新建实验配置</button>}
    </header>
    {group ? latest && <>
      <div className="config-controls">
        <button className="button button-primary" onClick={() => setRun(latest)}>启动 Run</button>
        <button className="button" disabled={!datasets.data?.length} onClick={() => setForm(latest)}>编辑配置</button>
      </div>
      <form className="config-controls" onSubmit={(event) => {
        event.preventDefault()
        update('version', String(new FormData(event.currentTarget).get('version') ?? ''))
      }}>
        <label className="field experiment-filter">配置版本<input key={version} name="version" type="number"
          min="1" max={latest.version} placeholder="全部版本" defaultValue={version} /></label>
        <button className="button">筛选 Run</button>
      </form>
    </> : <label className="field experiment-filter">数据集<select value={datasetId ?? ''} onChange={(event) => update('dataset', event.target.value)}>
      <option value="">全部数据集</option>
      {datasetId && !datasetName.has(datasetId) && <option value={datasetId}>数据集 #{datasetId}</option>}
      {datasets.data?.map((item) => <option key={item.dataset_id} value={item.dataset_id}>{item.name}</option>)}
    </select></label>}
    {(selected.isPending || (group && config.isPending)) && <p className="panel-message" role="status">正在加载{group ? '运行历史' : '配置'}…</p>}
    {loadError && <div className="panel-message"><p className="error-message" role="alert">{errorMessage(loadError)}</p>
      <button className="button" onClick={() => {
        if (selected.isError) void selected.refetch()
        if (group && config.isError) void config.refetch()
        if (datasets.isError) void datasets.refetch()
      }}>重试</button></div>}
    {group ? runs.data && <>
      {runs.data.items.length ? <div className="table-scroll"><table className="experiment-table">
        <thead><tr><th>Run</th><th>配置</th><th>数据集版本</th><th>状态</th><th>Job 进度</th><th>创建时间</th></tr></thead>
        <tbody>{runs.data.items.map((item) => <tr key={item.experiment_id}>
          <th scope="row"><Link to={`/experiments/${item.experiment_id}`} state={{ returnTo: '/experiments?' + params.toString() }}>
            {item.name}</Link><small>Run #{item.experiment_id}</small></th>
          <td>v{item.config_version}</td><td>#{item.dataset_version_id}</td><td>{statusLabels[item.status]}</td>
          <td>{processed(item.progress)} / {item.progress.job_count}</td><td>{new Date(item.created_at).toLocaleString()}</td>
        </tr>)}</tbody></table></div> : <p className="panel-message">暂无匹配的 Run。</p>}
    </> : configs.data && <>
      {configs.data.items.length ? <div className="table-scroll"><table className="experiment-table">
        <thead><tr><th>配置名称</th><th>最新版</th><th>数据集</th><th>更新时间</th><th>操作</th></tr></thead>
        <tbody>{configs.data.items.map((item) => <tr key={item.config_group_id}>
          <th scope="row"><Link to={`/experiments?group=${item.config_group_id}${datasetId ? `&dataset=${datasetId}` : ''}`}>{item.name}</Link></th>
          <td>v{item.version}</td><td>{datasetName.get(item.dataset_id) ?? `#${item.dataset_id}`}</td>
          <td>{new Date(item.created_at).toLocaleString()}</td>
          <td><button className="button button-quiet" onClick={() => setRun(item)}>启动 Run</button></td>
        </tr>)}</tbody></table></div>
      : <p className="panel-message">{datasets.data?.length === 0 ? '请先导入数据集。' : '暂无匹配的实验配置。'}</p>}
    </>}
    {selected.data && <div className="list-footer"><nav className="pagination" aria-label={group ? '运行历史分页' : '配置分页'}>
      <button className="button button-quiet" disabled={page <= 1} onClick={() => update('page', String(page - 1))}>上一页</button>
      <span>第 {page} / {Math.max(1, Math.ceil(selected.data.total / selected.data.pagesize))} 页</span>
      <button className="button button-quiet" disabled={page * selected.data.pagesize >= selected.data.total}
        onClick={() => update('page', String(page + 1))}>下一页</button>
    </nav><span>共 {selected.data.total} {group ? '个 Run' : '组配置'}</span></div>}
    {form && datasets.data?.length && startDatasetId ? <ConfigDialog userId={userId} datasets={datasets.data}
      initialId={startDatasetId} base={form === 'new' ? undefined : form} onClose={() => setForm(undefined)} /> : null}
    {run && <RunDialog userId={userId} latest={run} onClose={() => setRun(undefined)} />}
  </>
}
