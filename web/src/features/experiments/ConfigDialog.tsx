import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { privateKey } from '../../app/queryClient'
import { Dialog } from '../../components/ui/Dialog'
import { errorMessage } from '../../lib/api'
import type { Dataset } from '../datasets/api'
import type { Runner } from '../runners/api'
import { getAllPages, getBuiltinEvaluators, saveConfig } from './api'
import type { ConfigInput, EvaluatorDefinition, ExperimentConfig } from './api'
import { EvaluationEditor, evaluationConfigs, evaluationDrafts } from './EvaluationEditor'
import type { EvaluationDraft } from './EvaluationEditor'

type Props = {
  userId: number; datasets: Dataset[]; initialId: number; base?: ExperimentConfig; onClose: () => void
}

export function ConfigDialog(props: Props) {
  const { userId, base, onClose } = props
  const client = useQueryClient()
  const runners = useQuery({ queryKey: [...privateKey, userId, 'all-runners'],
    queryFn: ({ signal }) => getAllPages<Runner>('/runners', signal) })
  const catalog = useQuery({ queryKey: [...privateKey, userId, 'builtin-evaluators'],
    queryFn: ({ signal }) => getBuiltinEvaluators(signal) })
  const save = useMutation({
    mutationFn: (data: ConfigInput) => saveConfig(data, base),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: [...privateKey, userId, 'experiment-configs'] })
      onClose()
    },
  })
  const loadError = runners.error || catalog.error
  return <Dialog title={base ? `编辑实验配置 v${base.version}` : '新建实验配置'} onClose={onClose} busy={save.isPending}>
    {runners.data && catalog.data
      ? <ConfigForm {...props} runners={runners.data} definitions={catalog.data}
          busy={save.isPending} saveError={save.error} onSave={save.mutate} />
      : <div className="dialog-body">{loadError
        ? <><p className="error-message" role="alert">{errorMessage(loadError)}</p><button className="button"
            onClick={() => { if (runners.isError) void runners.refetch(); if (catalog.isError) void catalog.refetch() }}>重试</button></>
        : <p role="status">正在加载 Runner 和评分器…</p>}</div>}
  </Dialog>
}

function ConfigForm({ datasets, initialId, base, onClose, runners, definitions, busy, saveError, onSave }: Props & {
  runners: Runner[]; definitions: EvaluatorDefinition[]; busy: boolean; saveError: Error | null
  onSave: (data: ConfigInput) => void
}) {
  const [datasetId, setDatasetId] = useState(base?.dataset_id ?? initialId)
  const [runnerId, setRunnerId] = useState(base?.runner_id ?? '')
  const [drafts, setDrafts] = useState<EvaluationDraft[]>(() => evaluationDrafts(base?.evaluators ?? [], definitions))
  const [evaluatorsChanged, setEvaluatorsChanged] = useState(false)
  const nextDraftId = useRef(drafts.length)
  const [version, setVersion] = useState(String(base?.dataset_version_id ?? ''))
  const [localError, setLocalError] = useState('')
  const dataset = datasets.find((item) => item.dataset_id === datasetId)
  const runner = runners.find((item) => item.runner_id === runnerId)

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const name = String(form.get('name')).trim()
    const target = String(form.get('target')).trim()
    if (!name || !target || !dataset || !runner) {
      setLocalError('请检查配置名称、数据集、Runner 和 Agent 目标。')
      return
    }
    const values: ConfigInput = {
      name, description: String(form.get('description')).trim() || null, dataset_id: datasetId,
      dataset_version_id: version ? Number(version) : null, runner_id: runnerId, agent_target_id: target,
      timeout_seconds: Number(form.get('timeout')), repetitions: Number(form.get('repetitions')),
      stream_output: form.has('stream'),
    }
    const references = base?.evaluators.flatMap((item) => item.dataset_evaluator_id === null ? [] : [item.dataset_evaluator_id])
    try {
      if (base && references?.length && references.length === drafts.length && !evaluatorsChanged && datasetId === base.dataset_id) {
        values.dataset_evaluator_ids = references
      } else {
        values.evaluators = evaluationConfigs(drafts, definitions)
      }
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : '评分配置无效。')
      return
    }
    setLocalError('')
    onSave(values)
  }

  return <form className="dialog-body dialog-form" onSubmit={submit}>
    <label className="field">配置名称<input name="name" defaultValue={base?.name} maxLength={100} required autoFocus /></label>
    <label className="field">描述（选填）<textarea name="description" defaultValue={base?.description ?? ''} maxLength={2000} rows={2} /></label>
    <label className="field">数据集<select value={datasetId} onChange={(event) => { setDatasetId(Number(event.target.value)); setVersion('') }}>
      {datasets.map((item) => <option key={item.dataset_id} value={item.dataset_id}>{item.name}</option>)}</select></label>
    <label className="field">固定数据集版本（选填）<input type="number" min="1" max="2147483647" value={version}
      placeholder="留空，启动时使用最新版本" onChange={(event) => setVersion(event.target.value)} /></label>
    {dataset?.latest_version_id && <button type="button" className="button button-quiet"
      onClick={() => setVersion(String(dataset.latest_version_id))}>固定当前版本 #{dataset.latest_version_id}</button>}
    <label className="field">Runner<select aria-label="Runner" value={runnerId} required onChange={(event) => setRunnerId(event.target.value)}>
      <option value="">请选择 Runner</option>
      {runnerId && !runner && <option value={runnerId}>不可用 Runner（{runnerId}）</option>}
      {runners.map((item) => <option key={item.runner_id} value={item.runner_id}>{item.name}（{!item.enabled ? '已停用' : item.connection.online ? '在线' : '离线'}）</option>)}
    </select></label>
    <label className="field">Agent 目标 ID<input name="target" defaultValue={base?.agent_target_id} maxLength={128} required placeholder="Runner 本地配置中的目标 ID" /></label>
    <label className="field">重复次数<input name="repetitions" type="number" min="1" max="100" defaultValue={base?.repetitions ?? 1} required /></label>
    <label className="field">超时（秒）<input name="timeout" type="number" min="0.001" max="3600" step="any" defaultValue={base?.timeout_seconds ?? 60} required /></label>
    <EvaluationEditor drafts={drafts} definitions={definitions} disabled={busy}
      onAdd={() => {
        const draft = { id: nextDraftId.current++, name: '', rule: '', parameters: {} }
        setDrafts((items) => [...items, draft]); setEvaluatorsChanged(true)
      }}
      onChange={(draft) => { setDrafts((items) => items.map((item) => item.id === draft.id ? draft : item)); setEvaluatorsChanged(true) }}
      onRemove={(id) => { setDrafts((items) => items.filter((item) => item.id !== id)); setEvaluatorsChanged(true) }} />
    <label><input name="stream" type="checkbox" defaultChecked={base?.stream_output} /> 使用流式输出</label>
    {(localError || saveError) && <p className="error-message" role="alert">{localError || errorMessage(saveError)}</p>}
    <div className="dialog-actions"><button type="button" className="button" onClick={onClose} disabled={busy}>取消</button>
      <button className="button button-primary" disabled={busy || !runner}>{busy ? '保存中…' : '保存配置'}</button></div>
  </form>
}
