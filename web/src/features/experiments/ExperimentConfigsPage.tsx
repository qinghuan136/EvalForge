import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, Navigate, useSearchParams } from 'react-router'
import { privateKey } from '../../app/queryClient'
import { errorMessage } from '../../lib/api'
import type { Dataset } from '../datasets/api'
import { ConfigDialog } from './ConfigDialog'
import { RunDialog } from './RunDialog'
import { getAllPages, getConfig, getConfigs } from './api'
import type { ExperimentConfig } from './api'
import { configListPath, configPath, positiveId, runListPath, runsForGroup } from './routes'

export function ExperimentConfigsPage({ userId }: { userId: number }) {
  const [params] = useSearchParams()
  const group = params.get('group')
  if (group && (params.has('version') || params.has('page'))) {
    const legacy = new URLSearchParams(params)
    legacy.delete('dataset')
    return <Navigate replace to={`${runListPath}?${legacy}`} />
  }
  return <Configs key={group ?? ''} userId={userId} group={group} />
}

function Configs({ userId, group }: { userId: number; group: string | null }) {
  const [params, setParams] = useSearchParams()
  const [form, setForm] = useState<'new' | ExperimentConfig>()
  const [run, setRun] = useState<ExperimentConfig>()
  const datasetId = positiveId(params.get('dataset'))
  const page = positiveId(params.get('page')) ?? 1
  const key = [...privateKey, userId, 'experiment-configs']
  const datasets = useQuery({
    queryKey: [...privateKey, userId, 'all-datasets'],
    queryFn: ({ signal }) => getAllPages<Dataset>('/datasets', signal),
  })
  const configs = useQuery({
    queryKey: [...key, 'list', datasetId, page],
    queryFn: ({ signal }) => getConfigs(datasetId, page, signal), enabled: !group,
  })
  const config = useQuery({
    queryKey: [...key, group],
    queryFn: ({ signal }) => getConfig(group!, signal), enabled: !!group,
  })
  const datasetNames = new Map(datasets.data?.map((item) => [item.dataset_id, item.name]))
  const selected = group ? config : configs
  const loadError = selected.error || datasets.error
  const latest = config.data
  const startDatasetId = datasetId && datasetNames.has(datasetId) ? datasetId : datasets.data?.[0]?.dataset_id
  const configListUrl = datasetId ? `${configListPath}?dataset=${datasetId}` : configListPath

  function update(name: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(name, value)
    else next.delete(name)
    if (name !== 'page') next.delete('page')
    setParams(next)
  }

  return <>
    <header className="page-heading">
      <h1>{group ? latest?.name ?? '实验配置详情' : '实验配置'}</h1>
      {group ? latest && <p>最新版 v{latest.version}{latest.description ? ` · ${latest.description}` : ''}</p>
        : <p>保存可复用的配置，按需启动 Run。</p>}
      {group ? <Link className="button page-action" to={configListUrl}>返回配置列表</Link>
        : <button className="button button-primary page-action" disabled={!startDatasetId}
          onClick={() => setForm('new')}>新建实验配置</button>}
    </header>
    {!group && <label className="field experiment-filter">数据集<select value={datasetId ?? ''}
      onChange={(event) => update('dataset', event.target.value)}>
      <option value="">全部数据集</option>
      {datasetId && !datasetNames.has(datasetId) && <option value={datasetId}>数据集 #{datasetId}</option>}
      {datasets.data?.map((item) => <option key={item.dataset_id} value={item.dataset_id}>{item.name}</option>)}
    </select></label>}
    {selected.isPending && <p className="panel-message" role="status">正在加载配置…</p>}
    {loadError && <div className="panel-message">
      <p className="error-message" role="alert">{errorMessage(loadError)}</p>
      <button className="button" onClick={() => {
        if (selected.isError) void selected.refetch()
        if (datasets.isError) void datasets.refetch()
      }}>重试</button>
    </div>}
    {group && latest && <section className="detail-panel" aria-label="配置详情">
      <dl className="detail-list">
        <div><dt>数据集</dt><dd>{datasetNames.get(latest.dataset_id) ?? `#${latest.dataset_id}`}</dd></div>
        <div><dt>数据集版本</dt><dd>{latest.dataset_version_id ? `#${latest.dataset_version_id}` : '启动时使用最新版本'}</dd></div>
        <div><dt>Runner</dt><dd>{latest.runner_id}</dd></div>
        <div><dt>Agent 目标 ID</dt><dd>{latest.agent_target_id}</dd></div>
        <div><dt>重复次数</dt><dd>{latest.repetitions}</dd></div>
        <div><dt>超时</dt><dd>{latest.timeout_seconds} 秒</dd></div>
        <div><dt>执行方式</dt><dd>{latest.stream_output ? '流式' : '普通'}</dd></div>
        <div><dt>评分配置</dt><dd>{latest.evaluators.map((item) => 'kind' in item
          ? `${item.name}（${item.code_evaluator_name} v${item.code_evaluator_version_number}）` : item.name
        ).join('、') || '未配置'}</dd></div>
      </dl>
      <div className="config-controls">
        <button className="button button-primary" onClick={() => setRun(latest)}>启动 Run</button>
        <button className="button" disabled={!startDatasetId} onClick={() => setForm(latest)}>编辑配置</button>
        <Link className="button" to={runsForGroup(group)}>查看运行历史</Link>
      </div>
    </section>}
    {!group && configs.data && <>
      {configs.data.items.length ? <div className="table-scroll"><table className="experiment-table">
        <thead><tr><th>配置名称</th><th>最新版</th><th>数据集</th><th>更新时间</th><th>操作</th></tr></thead>
        <tbody>{configs.data.items.map((item) => <tr key={item.config_group_id}>
          <th scope="row"><Link to={configPath(item.config_group_id, datasetId ? `dataset=${datasetId}` : '')}>{item.name}</Link></th>
          <td>v{item.version}</td><td>{datasetNames.get(item.dataset_id) ?? `#${item.dataset_id}`}</td>
          <td>{new Date(item.created_at).toLocaleString()}</td>
          <td><button className="button button-quiet" onClick={() => setRun(item)}>启动 Run</button></td>
        </tr>)}</tbody>
      </table></div>
        : <p className="panel-message">{datasets.data?.length === 0 ? '请先导入数据集。' : '暂无匹配的实验配置。'}</p>}
      <div className="list-footer"><nav className="pagination" aria-label="配置分页">
        <button className="button button-quiet" disabled={page <= 1} onClick={() => update('page', String(page - 1))}>上一页</button>
        <span>第 {page} / {Math.max(1, Math.ceil(configs.data.total / configs.data.pagesize))} 页</span>
        <button className="button button-quiet" disabled={page * configs.data.pagesize >= configs.data.total}
          onClick={() => update('page', String(page + 1))}>下一页</button>
      </nav><span>共 {configs.data.total} 组配置</span></div>
    </>}
    {form && datasets.data && startDatasetId && <ConfigDialog userId={userId} datasets={datasets.data}
      initialId={startDatasetId} base={form === 'new' ? undefined : form} onClose={() => setForm(undefined)} />}
    {run && <RunDialog userId={userId} latest={run} onClose={() => setRun(undefined)} />}
  </>
}
