import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router'
import { privateKey } from '../../app/queryClient'
import { Dialog } from '../../components/ui/Dialog'
import { errorMessage } from '../../lib/api'
import { getDataset } from '../datasets/api'
import type { Dataset } from '../datasets/api'
import type { Runner } from '../runners/api'
import { createExperiment, getAllPages, getBuiltinEvaluators } from './api'
import type { CreateExperiment, EvaluatorConfig } from './api'
import { EvaluationEditor, evaluationConfigs } from './EvaluationEditor'
import type { EvaluationDraft } from './EvaluationEditor'

const emptyDefinitions: Awaited<ReturnType<typeof getBuiltinEvaluators>> = []

export function CreateDialog({ userId, datasets, initialId, onClose }: {
  userId: number; datasets: Dataset[]; initialId: number; onClose: () => void
}) {
  const [datasetId, setDatasetId] = useState(initialId)
  const [runnerId, setRunnerId] = useState('')
  const [drafts, setDrafts] = useState<EvaluationDraft[]>([])
  const nextDraftId = useRef(0)
  const [stream, setStream] = useState(false)
  const [repetitions, setRepetitions] = useState(1)
  const [localError, setLocalError] = useState('')
  const client = useQueryClient()
  const navigate = useNavigate()
  const dataset = useQuery({ queryKey: [...privateKey, userId, 'dataset', datasetId], queryFn: ({ signal }) => getDataset(datasetId, signal) })
  const runners = useQuery({ queryKey: [...privateKey, userId, 'all-runners'], queryFn: ({ signal }) => getAllPages<Runner>('/runners', signal) })
  const catalog = useQuery({ queryKey: [...privateKey, userId, 'builtin-evaluators'], queryFn: ({ signal }) => getBuiltinEvaluators(signal) })
  const definitions = catalog.data ?? emptyDefinitions
  const runner = runners.data?.find((item) => item.runner_id === runnerId)
  const canStream = !!runner && runner.enabled && runner.connection.online && runner.connection.capabilities.includes('http_json') && runner.connection.capabilities.includes('http_sse')
  const mutation = useMutation({
    mutationFn: (data: CreateExperiment) => createExperiment(datasetId, data),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: [...privateKey, userId, 'experiments', datasetId] })
      navigate(`/experiments/${result.experiment_id}`)
    },
  })
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const name = String(form.get('name')).trim()
    const target = String(form.get('target')).trim()
    const timeout = Number(form.get('timeout'))
    if (!name || !target || !dataset.data?.latest_version_id || !dataset.data.example_count || !runner?.enabled || !runner.connection.online || (stream && !canStream) || !Number.isInteger(repetitions) || repetitions < 1 || repetitions > 100 || repetitions * dataset.data.example_count > 10000 || !Number.isFinite(timeout) || timeout <= 0 || timeout > 3600) {
      setLocalError('请检查实验名称、数据集、在线 Runner、目标 ID、重复次数和超时设置。')
      return
    }
    let evaluators: EvaluatorConfig[]
    try {
      evaluators = evaluationConfigs(drafts, definitions)
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : '评分配置无效。')
      return
    }
    setLocalError('')
    mutation.mutate({
      name, description: String(form.get('description')).trim() || undefined,
      dataset_version_id: dataset.data.latest_version_id, repetitions, evaluators,
      execution_config: { runner_id: runnerId, agent_target_id: target, timeout_seconds: timeout, stream_output: stream },
    })
  }
  const loadError = dataset.error || runners.error
  return (
    <Dialog title="新建实验" onClose={onClose} busy={mutation.isPending}>
      <form className="dialog-body dialog-form" onSubmit={submit}>
        <p className="muted">使用所选数据集的最新版本，Runner 就绪后接收任务。</p>
        <label className="field">实验名称<input name="name" maxLength={100} required autoFocus /></label>
        <label className="field">描述（选填）<textarea name="description" maxLength={2000} rows={2} /></label>
        <label className="field">数据集<select value={datasetId} onChange={(event) => setDatasetId(Number(event.target.value))}>{datasets.map((item) => <option key={item.dataset_id} value={item.dataset_id}>{item.name}</option>)}</select></label>
        <p>数据集版本 {dataset.data?.latest_version_id ? `#${dataset.data.latest_version_id}` : '暂无'}，样本 {dataset.data?.example_count ?? '…'} 条</p>
        <label className="field">Runner<select aria-label="Runner" value={runnerId} required onChange={(event) => { setRunnerId(event.target.value); setStream(false) }}>
          <option value="">请选择 Runner</option>
          {runners.data?.map((item) => <option key={item.runner_id} value={item.runner_id} disabled={!item.enabled || !item.connection.online}>{item.name}（{!item.enabled ? '已停用' : item.connection.online ? item.connection.phase === 'ready' ? '已就绪' : '在线' : '离线'}）</option>)}
        </select></label>
        <label className="field">Agent 目标 ID<input name="target" maxLength={128} required placeholder="Runner 本地配置中的目标 ID" /></label>
        <label className="field">重复次数<input type="number" min="1" max="100" value={repetitions} onChange={(event) => setRepetitions(Number(event.target.value))} required /></label>
        <label className="field">超时（秒）<input name="timeout" type="number" min="0.001" max="3600" step="any" defaultValue="60" required /></label>
        <EvaluationEditor drafts={drafts} definitions={definitions} disabled={mutation.isPending}
          onAdd={() => {
            const draft = { id: nextDraftId.current++, name: '', rule: '', parameters: {} }
            setDrafts((items) => [...items, draft])
          }}
          onChange={(draft) => setDrafts((items) => items.map((item) => item.id === draft.id ? draft : item))}
          onRemove={(id) => setDrafts((items) => items.filter((item) => item.id !== id))} />
        {catalog.isPending && <p className="muted">正在加载评分器…</p>}
        {catalog.error && <p className="error-message" role="alert">评分器加载失败。 <button type="button" className="button" onClick={() => void catalog.refetch()}>重试</button></p>}
        <label><input type="checkbox" checked={stream} disabled={!canStream} onChange={(event) => setStream(event.target.checked)} /> 使用流式输出</label>
        <p className="muted">预计生成 {dataset.data ? dataset.data.example_count * repetitions : '…'} 个 Job。流式执行需要 Runner 支持 http_json 和 http_sse。</p>
        {loadError ? <p className="error-message" role="alert">{errorMessage(loadError)} <button className="button" type="button" onClick={() => { void dataset.refetch(); void runners.refetch() }}>重试</button></p> : null}
        {localError || mutation.error ? <p className="error-message" role="alert">{localError || errorMessage(mutation.error)}{mutation.error ? '。请先检查实验列表，避免重复创建。' : ''}</p> : null}
        <div className="dialog-actions"><button type="button" className="button" onClick={onClose} disabled={mutation.isPending}>取消</button><button className="button button-primary" disabled={mutation.isPending || !dataset.data || !runners.data}>{mutation.isPending ? '创建中…' : '创建实验'}</button></div>
      </form>
    </Dialog>
  )
}
