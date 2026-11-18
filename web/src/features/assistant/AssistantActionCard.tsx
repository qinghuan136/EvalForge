import { useRef, useState } from 'react'
import type { AssistantAction } from './api'
import { takeRunnerCredential } from './api'
import type { RunnerCreated } from '../runners/api'
import { errorMessage } from '../../lib/api'

const labels: Record<string, string> = {
  RUNNER_create_runner: '创建 Runner', RUNNER_delete_runner: '删除 Runner 并撤销凭证',
  DATASET_import_dataset: '导入数据集', EXPERIMENT_create_experiment: '启动实验',
  EXPERIMENT_cancel_experiment: '取消整场实验',
}

export function AssistantRunnerCredential({ sessionId, callId }: { sessionId: number; callId: string }) {
  const [result, setResult] = useState<RunnerCreated>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const attempted = useRef(false)
  async function reveal() {
    if (attempted.current) return
    attempted.current = true
    setBusy(true)
    try { setResult(await takeRunnerCredential(sessionId, callId)) }
    catch (err) { setError(errorMessage(err)) }
    finally { setBusy(false) }
  }
  return <section className="example" aria-label="新 Runner 凭证">
    {result ? <>
      <h3>{result.runner.name}</h3>
      <label className="field">连接凭证（仅显示一次）<textarea readOnly value={result.credential.token} /></label>
      <label className="field">连接地址<input readOnly value={result.connection.websocket_url} /></label>
    </> : error ? <p role="alert">{error}</p> : <button className="button" type="button" disabled={busy}
      onClick={() => void reveal()}>领取新 Runner 凭证</button>}
  </section>
}

export function AssistantApprovals({ actions, disabled, onSubmit }: {
  actions: AssistantAction[]; disabled: boolean; onSubmit: (decisions: Record<string, boolean>) => void
}) {
  const [decisions, setDecisions] = useState<Record<string, boolean>>({})
  const supported = actions.every((action) => Object.hasOwn(labels, action.tool))
  return <section aria-label="待审批操作">
    {actions.map((action) => {
      const label = Object.hasOwn(labels, action.tool) ? labels[action.tool] : undefined
      const { content, ...fields } = action.arguments
      return <section className="example" key={action.tool_call_id}>
        <h3>{label ?? `未支持的工具：${action.tool}`}</h3>
        <pre>{JSON.stringify(fields, null, 2)}</pre>
        {typeof content === 'string' ? <details><summary>查看样例文件内容</summary><pre>{content}</pre></details> : null}
        {label ? <fieldset disabled={disabled}>
          <legend>审批决定</legend>
          {[true, false].map((approve) => <label key={String(approve)}>
            <input type="radio" name={`approval-${action.tool_call_id}`} checked={decisions[action.tool_call_id] === approve}
              onChange={() => setDecisions((previous) => ({ ...previous, [action.tool_call_id]: approve }))} />
            {approve ? '批准' : '拒绝'}
          </label>)}
        </fieldset> : <p role="alert">此操作不能在当前页面审批。</p>}
      </section>
    })}
    <div className="dialog-actions"><button className="button button-primary" type="button"
      disabled={disabled || !supported || actions.some((action) => !Object.hasOwn(decisions, action.tool_call_id))}
      onClick={() => onSubmit(decisions)}>提交全部决定</button></div>
  </section>
}
