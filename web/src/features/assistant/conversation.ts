import type { PageContext } from './pageContext'

export type ConversationPart = {
  part_kind: string; content?: unknown; args?: unknown; tool_name?: string
  tool_call_id?: string; timestamp?: string; outcome?: string
}
export type ToolCallPart = ConversationPart & { part_kind: 'tool-call'; tool_name: string; tool_call_id: string }
export type ToolResultPart = ConversationPart & { part_kind: 'tool-return'; tool_name: string; tool_call_id: string }
export type DeferredRequests = { calls: ToolCallPart[]; approvals: ToolCallPart[] }
export type Conversation = {
  conversation_id: string; revision: string | null; usage: Record<string, unknown>
  messages: {
    kind: 'request' | 'response'; parts: ConversationPart[]; run_id?: string | null
    timestamp?: string | null; model_name?: string | null; provider_name?: string | null
    metadata?: { evalforge?: { text?: string; page_context?: PageContext | null; source_experiment_id?: number | null } } | null
  }[]
  deferred_tool_requests: DeferredRequests | null
}
type EventIdentity = { conversation_id: string; run_id: string }
export type ToolStreamEvent = EventIdentity & (
  { type: 'tool_call'; part: ToolCallPart; args_valid: boolean | null } |
  { type: 'tool_result'; part: ToolResultPart } |
  { type: 'tool_deferred'; requests: DeferredRequests }
)
export type ToolStatus = 'requested' | 'running' | 'awaiting_approval' | 'awaiting_result'
  | 'success' | 'failed' | 'denied' | 'interrupted' | 'retried'
export type ToolExecution = {
  id: string; toolCallId: string; tool: string; runId: string; args?: unknown; result?: unknown; status: ToolStatus
}
export type ChatMessage = {
  kind: 'message'; id: string; role: 'user' | 'assistant'; text: string; modelName?: string | null
  pageContext?: PageContext | null; sourceExperimentId?: number | null; temporary?: boolean
}
export type ExecutionGroup = { kind: 'execution'; id: string; runId: string; tools: ToolExecution[] }
export type ConversationItem = ChatMessage | ExecutionGroup
export type LiveContent = { message?: string; pageContext?: PageContext; events: ToolStreamEvent[] }

const unfinished = new Set<ToolStatus>(['requested', 'running', 'awaiting_approval', 'awaiting_result'])

/** Turn native messages and this request's tool events into one display timeline. */
export function conversationItems(conversation?: Conversation, live?: LiveContent): ConversationItem[] {
  const items: ConversationItem[] = []
  const groups = new Map<string, ExecutionGroup>()
  const calls = new Map<string, ToolExecution>()
  const latest = new Map<string, ToolExecution>()

  function tool(part: ConversationPart, runId: string, status: ToolStatus): ToolExecution | undefined {
    if (!part.tool_call_id || !part.tool_name) return
    const key = JSON.stringify([runId, part.tool_call_id])
    const previous = latest.get(part.tool_call_id)
    // Approval resume has a new run_id but answers the original call.
    let execution = calls.get(key) ?? (previous && unfinished.has(previous.status) ? previous : undefined)
    if (!execution) {
      let group = groups.get(runId)
      if (!group) {
        group = { kind: 'execution', id: `execution:${runId}`, runId, tools: [] }
        groups.set(runId, group)
        items.push(group)
      }
      execution = { id: key, runId, toolCallId: part.tool_call_id, tool: part.tool_name, status }
      group.tools.push(execution)
    }
    calls.set(key, execution)
    latest.set(part.tool_call_id, execution)
    return execution
  }

  function call(part: ConversationPart, runId: string, status: ToolStatus = 'requested') {
    const execution = tool(part, runId, status)
    if (execution) Object.assign(execution, { args: part.args, status })
  }
  function result(part: ConversationPart, runId: string) {
    const execution = (part.tool_call_id && latest.get(part.tool_call_id)) || tool(part, runId, 'requested')
    if (!execution) return
    const outcome = part.outcome ?? 'success'
    const businessError = typeof part.content === 'object' && part.content !== null
      && Object.keys(part.content).length === 1 && 'error' in part.content && part.content.error != null
    execution.result = part.content
    execution.status = outcome === 'success' ? (businessError ? 'failed' : 'success')
      : ['failed', 'denied', 'interrupted', 'retried'].includes(outcome) ? outcome as ToolStatus : 'failed'
  }
  function deferred(requests: DeferredRequests, runId: string) {
    for (const part of requests.approvals) call(part, runId, 'awaiting_approval')
    for (const part of requests.calls) call(part, runId, 'awaiting_result')
  }

  for (const [index, message] of (conversation?.messages ?? []).entries()) {
    const runId = message.run_id ?? `history-${index}`
    const metadata = message.metadata?.evalforge
    const parts = message.parts.filter((part) => part.part_kind === (message.kind === 'request' ? 'user-prompt' : 'text'))
    if (parts.length) {
      const text = metadata?.text ?? parts.map((part) => typeof part.content === 'string' ? part.content : '').join('')
      if (text) items.push({ kind: 'message', id: `message:${runId}:${index}`, role: message.kind === 'request' ? 'user' : 'assistant',
        text, modelName: message.model_name, pageContext: metadata?.page_context, sourceExperimentId: metadata?.source_experiment_id })
    }
    for (const part of message.parts) {
      if (part.part_kind === 'tool-call') call(part, runId)
      else if (part.part_kind === 'tool-return') result(part, runId)
      else if (part.part_kind === 'retry-prompt' && part.tool_call_id) result({ ...part, outcome: 'retried' }, runId)
    }
  }
  if (conversation?.deferred_tool_requests) deferred(conversation.deferred_tool_requests, 'pending')
  if (live?.message) items.push({ kind: 'message', id: 'live-user', role: 'user', text: live.message,
    pageContext: live.pageContext, temporary: true })
  for (const event of live?.events ?? []) {
    if (event.type === 'tool_call') call(event.part, event.run_id, event.args_valid === true ? 'running' : 'requested')
    else if (event.type === 'tool_result') result(event.part, event.run_id)
    else deferred(event.requests, event.run_id)
  }
  return items
}
