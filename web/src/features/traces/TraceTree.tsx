import { useMemo, useState } from 'react'
import type { SpanSummary } from './api'
import { displayDuration, statusLabel } from './display'

type TreeRow = { span: SpanSummary; depth: number; children: boolean; missingParent: boolean; invalidParent: boolean }

function traceRows(spans: SpanSummary[]): TreeRow[] {
  const ids = new Set(spans.map((span) => span.span_id))
  const children = new Map<string, SpanSummary[]>()
  const roots: SpanSummary[] = []
  for (const span of spans) {
    const parent = span.parent_span_id
    if (!parent || !ids.has(parent)) roots.push(span)
    else {
      const siblings = children.get(parent)
      if (siblings) siblings.push(span)
      else children.set(parent, [span])
    }
  }
  const visited = new Set<string>()
  const rows: TreeRow[] = []
  function walk(root: SpanSummary, invalidParent = false) {
    const stack = [{ span: root, depth: 0 }]
    while (stack.length) {
      const { span, depth } = stack.pop()!
      if (visited.has(span.span_id)) continue
      visited.add(span.span_id)
      const descendants = children.get(span.span_id) ?? []
      rows.push({
        span, depth, children: descendants.some((child) => !visited.has(child.span_id)),
        missingParent: span.parent_span_id !== null && !ids.has(span.parent_span_id),
        invalidParent: depth === 0 && invalidParent,
      })
      for (let i = descendants.length - 1; i >= 0; i--) stack.push({ span: descendants[i], depth: depth + 1 })
    }
  }
  roots.forEach((root) => walk(root))
  for (const span of spans) if (!visited.has(span.span_id)) walk(span, true)
  return rows
}

export function TraceTree({ spans, selected, onSelect, start, end, hasMore }: {
  spans: SpanSummary[]; selected: string | null; onSelect: (id: string) => void
  start: string; end: string; hasMore: boolean
}) {
  const rows = useMemo(() => traceRows(spans), [spans])
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set())
  const traceStart = BigInt(start)
  const duration = Number(BigInt(end) - traceStart)
  const percent = (time: string) => duration > 0 ? Math.max(0, Math.min(100, Number(BigInt(time) - traceStart) / duration * 100)) : 0
  let hiddenDepth: number | null = null
  const visible = rows.filter(({ span, depth }) => {
    if (hiddenDepth !== null && depth > hiddenDepth) return false
    hiddenDepth = collapsed.has(span.span_id) ? depth : null
    return true
  })

  return <div className="trace-tree-scroll">
    <div className="trace-tree-head">
      <span>调用</span><span className="trace-scale"><span className="trace-ticks"><span>0</span><span>{displayDuration(start, end)}</span></span></span>
    </div>
    <ul className="trace-tree" aria-label="调用树">
      {visible.map(({ span, depth, children, missingParent, invalidParent }) => {
        const left = percent(span.start_time_unix_nano)
        const width = Math.max(0, percent(span.end_time_unix_nano) - left)
        return <li key={span.span_id} className={selected === span.span_id ? 'trace-node-selected' : undefined}>
          <div className="trace-node" style={{ paddingInlineStart: 8 + Math.min(depth, 8) * 11 }}>
            {children ? <button type="button" className="trace-toggle" aria-expanded={!collapsed.has(span.span_id)}
              aria-label={`${collapsed.has(span.span_id) ? '展开' : '折叠'} ${span.name}`}
              onClick={() => setCollapsed((old) => {
                const next = new Set(old)
                if (next.has(span.span_id)) next.delete(span.span_id); else next.add(span.span_id)
                return next
              })}>{collapsed.has(span.span_id) ? '▸' : '▾'}</button> : <span className="trace-toggle" aria-hidden="true" />}
            <button type="button" className="trace-node-label" aria-pressed={selected === span.span_id}
              onClick={() => onSelect(span.span_id)} title={span.name}>
              <span>{span.name}</span>
              {missingParent ? <small>{hasMore ? '父 Span 未加载' : '父 Span 未收到'}</small> : null}
              {invalidParent ? <small>父子关系异常</small> : null}
            </button>
          </div>
          <div className="trace-span-time" title={`${statusLabel(span.status_code)} · ${displayDuration(span.start_time_unix_nano, span.end_time_unix_nano)}`}>
            <span className="trace-span-ruler" aria-hidden="true"><span data-error={span.status_code === 2}
              style={{ left: `${Math.min(left, 99.5)}%`, width: `${width}%` }} /></span>
            <span>{displayDuration(span.start_time_unix_nano, span.end_time_unix_nano)}</span>
          </div>
        </li>
      })}
    </ul>
  </div>
}
