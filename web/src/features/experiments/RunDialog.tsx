import { useId, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router'
import { privateKey } from '../../app/queryClient'
import { Dialog } from '../../components/ui/Dialog'
import { errorMessage } from '../../lib/api'
import { getExamples } from '../datasets/api'
import { getVersions, startRun } from './api'
import type { ExperimentConfig, RunInput } from './api'

export function RunDialog({ userId, latest, onClose }: {
  userId: number; latest: ExperimentConfig; onClose: () => void
}) {
  const [configId, setConfigId] = useState(latest.id)
  const [busy, setBusy] = useState(false)
  const versions = useQuery({
    queryKey: [...privateKey, userId, 'experiment-configs', latest.config_group_id, 'versions'],
    queryFn: ({ signal }) => getVersions(latest.config_group_id, signal),
  })
  const config = versions.data?.find((item) => item.id === configId) ?? latest
  return <Dialog title="启动 Run" onClose={onClose} busy={busy}>
    <div className="dialog-body dialog-form">
      <label className="field">配置版本<select aria-label="配置版本" value={config.id} disabled={busy} onChange={(event) => setConfigId(Number(event.target.value))}>
        {(versions.data ?? [latest]).map((item) => <option key={item.id} value={item.id}>v{item.version} · {item.name}</option>)}
      </select></label>
      {versions.isPending && <p className="muted" role="status">正在加载配置版本…</p>}
      {versions.isError && <p className="error-message" role="alert">{errorMessage(versions.error)}{' '}
        <button className="button" onClick={() => void versions.refetch()}>重试</button></p>}
      <RunSamples key={config.id} userId={userId} config={config} onClose={onClose} onBusy={setBusy} />
    </div>
  </Dialog>
}

function RunSamples({ userId, config, onClose, onBusy }: {
  userId: number; config: ExperimentConfig; onClose: () => void; onBusy: (busy: boolean) => void
}) {
  const [page, setPage] = useState(1)
  const [subset, setSubset] = useState(false)
  const [selected, setSelected] = useState(() => new Set<number>())
  const navigate = useNavigate()
  const client = useQueryClient()
  const snapshotKey = useId()
  // Keep page one and its resolved version fixed for this dialog.
  const discovered = useQuery({
    queryKey: [...privateKey, userId, 'run-dataset-version', snapshotKey, config.dataset_id, config.dataset_version_id],
    queryFn: ({ signal }) => getExamples(config.dataset_id, 1, signal, config.dataset_version_id ?? undefined),
    staleTime: 'static',
    gcTime: 0,
  })
  const pickedVersion = discovered.data?.dataset_version_id ?? undefined
  const otherPage = useQuery({
    queryKey: [...privateKey, userId, 'run-dataset-examples', config.dataset_id, pickedVersion, page],
    queryFn: ({ signal }) => getExamples(config.dataset_id, page, signal, pickedVersion),
    enabled: subset && pickedVersion !== undefined && page > 1,
  })
  const examples = page === 1 ? discovered : otherPage
  const ready = pickedVersion !== undefined && !!discovered.data?.total
  const jobCount = (subset ? selected.size : discovered.data?.total ?? 0) * config.repetitions
  const run = useMutation({
    mutationFn: (payload: RunInput) => startRun(config, payload),
    retry: false,
    onMutate: () => onBusy(true),
    onSettled: () => onBusy(false),
    onSuccess: (item) => {
      void client.invalidateQueries({ queryKey: [...privateKey, userId, 'experiment-runs', config.config_group_id] })
      const returnTo = `/experiments?group=${config.config_group_id}`
      navigate(`/experiments/${item.experiment_id}`, { state: { returnTo } })
    },
  })

  function toggle(id: number, checked: boolean) {
    setSelected((previous) => {
      const next = new Set(previous)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }

  return <>
    {discovered.isPending || (discovered.isFetching && pickedVersion === undefined)
      ? <p role="status">正在解析数据集版本…</p>
      : ready ? <p>数据集 #{config.dataset_id} · 版本 #{pickedVersion} · {discovered.data?.total} 条样本</p>
        : discovered.isSuccess && <p className="panel-message">该数据集版本没有有效样本。</p>}
    {discovered.isError && <p className="error-message" role="alert">{errorMessage(discovered.error)}{' '}
      <button className="button" onClick={() => void discovered.refetch()}>重试</button></p>}
    <fieldset className="evaluation-options run-scope" disabled={!ready || run.isPending}>
      <legend>运行范围</legend>
      <label><input type="radio" name="run-scope" checked={!subset} onChange={() => { setSubset(false); setPage(1) }} /> 全部样本</label>{' '}
      <label><input type="radio" name="run-scope" checked={subset} onChange={() => setSubset(true)} /> 选择样本</label>
      {subset && <>
        {examples.isPending && <p role="status">正在加载样本…</p>}
        {examples.isError && <p className="error-message" role="alert">{errorMessage(examples.error)}{' '}
          <button className="button" onClick={() => void examples.refetch()}>重试</button></p>}
        {examples.data?.items.map((item) => {
          const input = JSON.stringify(item.input)
          return <label key={item.example_id} className="run-sample">
            <input type="checkbox" checked={selected.has(item.example_id)}
              onChange={(event) => toggle(item.example_id, event.target.checked)} />
            <span>Example #{item.example_id}<code className="sample-input-preview" title={input}>{input}</code></span>
          </label>
        })}
        {examples.data && <nav className="pagination" aria-label="样本分页">
          <button className="button button-quiet" disabled={page === 1} onClick={() => setPage(page - 1)}>上一页</button>
          <span>第 {page} 页</span><button className="button button-quiet"
            disabled={page * examples.data.pagesize >= examples.data.total} onClick={() => setPage(page + 1)}>下一页</button>
        </nav>}
      </>}
    </fieldset>
    <p className="muted">{subset ? `已选择 ${selected.size} 条样本。 ` : ''}将生成 {jobCount} 个 Job。</p>
    {jobCount > 10_000 && <p className="error-message" role="alert">一次 Run 最多生成 10,000 个 Job，请缩小样本范围。</p>}
    {run.error && <p className="error-message" role="alert">{errorMessage(run.error)}。重试前请确认运行历史。</p>}
    <div className="dialog-actions"><button className="button" onClick={onClose} disabled={run.isPending}>取消</button>
      <button className="button button-primary" disabled={!ready || run.isPending || jobCount === 0 || jobCount > 10_000}
        onClick={() => {
          if (pickedVersion !== undefined) run.mutate({ dataset_version_id: pickedVersion,
            example_ids: subset ? [...selected] : undefined })
        }}>{run.isPending ? '启动中…' : '启动 Run'}</button></div>
  </>
}
