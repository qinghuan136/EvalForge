import { useState } from 'react'
import type { AssistantAction, DisplayCard, DisplayField } from './api'

const labels: Record<string, string> = {
  RUNNER_create_runner: '创建 Runner', RUNNER_delete_runner: '删除 Runner 并撤销凭证',
  DATASET_import_dataset: '导入数据集', EXPERIMENT_create_experiment: '启动实验',
  EXPERIMENT_cancel_experiment: '取消整场实验',
}

function DisplayValue({ field }: { field: DisplayField }) {
  const [shown, setShown] = useState(!field.sensitive)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')
  async function copy() {
    try { await navigator.clipboard.writeText(field.value); setCopied(true); setError('') }
    catch { setError('复制失败，请显示内容后手动复制。') }
  }
  return <div>
    <label className="field">{field.label}<input readOnly type={shown ? 'text' : 'password'} value={field.value} /></label>
    <div className="dialog-actions">
      {field.sensitive ? <button className="button button-quiet" type="button" onClick={() => setShown(!shown)}
        aria-label={`${shown ? '隐藏' : '显示'}${field.label}`}>{shown ? '隐藏' : '显示'}</button> : null}
      <button className="button button-quiet" type="button" aria-label={`复制${field.label}`}
        onClick={() => void copy()}>{copied ? '已复制' : '复制'}</button>
    </div>
    {error ? <p role="alert">{error}</p> : null}
  </div>
}

export function AssistantDisplayCard({ card }: { card: DisplayCard }) {
  return <section className="example" aria-label={card.title}>
    <h3>{card.title}</h3>
    <p>{card.description}</p>
    {card.fields.map((field, index) => <DisplayValue key={`${card.id}:${index}`} field={field} />)}
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
        <h3>{action.title}</h3>
        <p>{action.description}</p>
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
