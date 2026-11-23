import { Link } from 'react-router'
import { runDetailPath } from '../experiments/routes'
import type { ConversationItem, ToolStatus } from './conversation'
import { pageContextLabel } from './pageContext'

const statusLabels: Record<ToolStatus, string> = {
  requested: '已请求', running: '执行中', awaiting_approval: '等待审批', awaiting_result: '等待结果',
  success: '已完成', failed: '失败', denied: '已拒绝', interrupted: '已中断', retried: '需要重试',
}
function format(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? ''
}

export function AssistantMessages({ items, preview, failed }: {
  items: ConversationItem[]; preview: string; failed: boolean
}) {
  if (!items.length && !preview) return null
  return <ol className="assistant-messages" aria-label="对话消息" aria-live="polite">
    {items.map((item) => item.kind === 'message' ? <li key={item.id}>
      <strong>{item.role === 'user' ? '你' : '助手'}</strong>
      {item.modelName ? <small className="muted"> · {item.modelName}</small> : null}
      {item.pageContext ? <small className="muted"> · {pageContextLabel(item.pageContext)}</small> : null}
      {item.temporary ? <small className="muted"> · 本轮消息</small> : null}
      <p>{item.text}</p>
      {item.sourceExperimentId != null ? <Link to={runDetailPath(item.sourceExperimentId)}>查看数据来源</Link> : null}
    </li> : <li key={item.id}>
      <details>
        <summary>执行详情 · {item.tools.length} 个工具</summary>
        {item.tools.map((tool) => <section className="assistant-tool" key={tool.id}>
          <p><strong>{tool.tool}</strong> · {statusLabels[tool.status]}</p>
          {tool.args !== undefined ? <details><summary>调用参数</summary><pre>{format(tool.args)}</pre></details> : null}
          {tool.result !== undefined ? <details><summary>返回结果</summary><pre>{format(tool.result)}</pre></details> : null}
        </section>)}
      </details>
    </li>)}
    {preview ? <li aria-label="助手实时回答"><strong>助手</strong><small className="muted"> · {failed ? '本轮未确认保存' : '本轮回答'}</small>
      <p>{preview}</p></li> : null}
  </ol>
}
