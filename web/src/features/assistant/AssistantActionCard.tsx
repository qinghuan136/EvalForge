import { useRef, useState } from 'react'
import { ApiError, errorMessage } from '../../lib/api'
import { executeAssistantAction } from './api'
import type { AssistantAction, AssistantActionResult } from './api'

const labels: Record<AssistantAction['tool'], string> = {
  RUNNER_create_runner: '创建 Runner',
  RUNNER_delete_runner: '删除 Runner 并撤销凭证',
  DATASET_import_dataset: '导入数据集',
  EXPERIMENT_create_experiment: '启动实验',
  EXPERIMENT_cancel_experiment: '取消整场实验',
}

export function AssistantActionCard({ action, disabled, onBusy, onSuccess }: {
  action: AssistantAction; disabled: boolean; onBusy: (busy: boolean) => void; onSuccess: () => void
}) {
  const started = useRef(false)
  const [result, setResult] = useState<AssistantActionResult>()
  const [error, setError] = useState<string>()
  const [dismissed, setDismissed] = useState(false)

  async function confirm() {
    if (disabled || started.current) return
    started.current = true
    onBusy(true)
    try {
      setResult(await executeAssistantAction(action))
      onSuccess()
    } catch (err) {
      setError(err instanceof ApiError && (err.status === 0 || err.status >= 500)
        ? '操作结果暂时无法确认，请先查询资源状态。'
        : errorMessage(err))
    } finally {
      onBusy(false)
    }
  }

  if (dismissed) return null
  const { content, ...fields } = action.arguments as Record<string, unknown>
  return <section className="example" aria-label={labels[action.tool]}>
    <h3>{labels[action.tool]}</h3>
    {result ? <div role="status">
      <p>{result.message}</p>
      {result.runner ? <>
        <label className="field">连接凭证（仅显示一次）<textarea readOnly value={result.runner.credential.token} /></label>
        <label className="field">连接地址<input readOnly value={result.runner.connection.websocket_url} /></label>
      </> : null}
    </div> : error ? <p role="alert" className="error-message">{error}</p> : <>
      <pre>{JSON.stringify(fields, null, 2)}</pre>
      {typeof content === 'string' ? <details><summary>查看样例文件内容</summary><pre>{content}</pre></details> : null}
      <div className="dialog-actions">
        <button className="button" type="button" disabled={disabled} onClick={() => setDismissed(true)}>放弃</button>
        <button className="button button-primary" type="button" disabled={disabled}
          onClick={() => void confirm()}>确认执行</button>
      </div>
    </>}
  </section>
}
