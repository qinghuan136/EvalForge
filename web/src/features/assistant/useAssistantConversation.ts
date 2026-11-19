import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { privateKey } from '../../app/queryClient'
import { ApiError, errorMessage } from '../../lib/api'
import { createAssistantSession, getAssistantSession, getAssistantSessions, streamAssistant } from './api'
import type { ApprovalRequired, DisplayCard, Transcript } from './api'
import { conversationItems } from './conversation'
import type { LiveContent } from './conversation'
import { pageContextLabel } from './pageContext'
import type { PageContext } from './pageContext'

type Input = { type: 'message'; message: string } | { type: 'decision'; decisions: Record<string, boolean> }
type LiveTurn = LiveContent & {
  conversationId: string; baseRevision: string; text: string; approval?: ApprovalRequired
  phase: 'streaming' | 'refreshing' | 'sync_failed' | 'failed'; savedRevision?: string; syncError?: string
}

/** Owned by the workspace controller; hiding its view does not stop the request. */
export function useAssistantConversation(userId: number, open: boolean, pageContext?: PageContext) {
  const client = useQueryClient()
  const selectedKey = `evalforge.assistant.selected:${userId}`
  const [sessionId, setSessionId] = useState<string | undefined>(() => {
    try { return sessionStorage.getItem(selectedKey) || undefined }
    catch { return undefined }
  })
  const [selectedModel, setSelectedModel] = useState('')
  const [draft, setDraft] = useState('')
  const [live, setLive] = useState<LiveTurn>()
  // Display cards can contain credentials: keep them out of query/browser storage.
  const [displays, setDisplays] = useState<{ conversationId: string; card: DisplayCard }[]>([])
  const inFlight = useRef<AbortController | undefined>(undefined)
  useEffect(() => () => inFlight.current?.abort(), [])

  const sessionsKey = [...privateKey, userId, 'assistant-sessions'] as const
  const keyFor = (id?: string) => [...privateKey, userId, 'assistant-session', id] as const
  const sessions = useQuery({ queryKey: sessionsKey,
    queryFn: ({ signal }) => getAssistantSessions(signal), enabled: open })
  const transcript = useQuery({ queryKey: keyFor(sessionId),
    queryFn: ({ signal }) => getAssistantSession(sessionId!, signal), enabled: open && sessionId !== undefined,
    refetchOnWindowFocus: !inFlight.current,
    refetchInterval: (query) => query.state.data?.status === 'running' && !inFlight.current ? 1000 : false,
  })
  const models = sessions.data?.models ?? []
  const model = models.includes(selectedModel) ? selectedModel : models[0] ?? ''

  function rememberSession(id?: string) {
    setSessionId(id)
    try {
      if (id) sessionStorage.setItem(selectedKey, id)
      else sessionStorage.removeItem(selectedKey)
    } catch { /* Selection stays in memory when browser storage is disabled. */ }
  }

  const send = useMutation({
    retry: false,
    mutationFn: async ({ input, snapshot, controller, context, modelName }: {
      input: Input; snapshot?: Transcript; controller: AbortController; context?: PageContext; modelName: string
    }) => {
      const title = context ? `${pageContextLabel(context)} 对话` : '新对话'
      const session = snapshot ?? await createAssistantSession(title, controller.signal)
      controller.signal.throwIfAborted()
      const id = session.conversation_id
      if (!snapshot) {
        client.setQueryData<Transcript>(keyFor(id), { ...session, messages: [], actions: [], status: 'completed', error: null,
          conversation: { conversation_id: id, revision: session.revision, messages: [], usage: {}, deferred_tool_requests: null } })
        rememberSession(id)
        void client.invalidateQueries({ queryKey: sessionsKey })
      }
      setLive({ conversationId: id, baseRevision: session.revision, phase: 'streaming', text: '', events: [],
        message: input.type === 'message' ? input.message : undefined,
        pageContext: input.type === 'message' ? context : undefined })
      const request = { conversation_id: id, expected_revision: session.revision }
      const runnerId = input.type === 'decision' ? snapshot?.actions.find((action) =>
        action.tool === 'EXPERIMENT_start_run' && input.decisions[action.tool_call_id])?.arguments.runner_id : undefined
      const turn = await streamAssistant(input.type === 'message'
        ? { ...request, ...input, model_name: modelName, page_context: context } : { ...request, ...input }, {
        onText: (text) => setLive((previous) => previous && { ...previous, text: previous.text + text }),
        onTool: (event) => setLive((previous) => previous && { ...previous, events: [...previous.events, event] }),
        onApproval: (approval) => setLive((previous) => previous && { ...previous, approval }),
        onDisplay: (card) => setDisplays((previous) => [
          ...previous.filter((item) => item.conversationId !== id || item.card.id !== card.id),
          { conversationId: id, card },
        ]),
      }, controller.signal, typeof runnerId === 'string' && /^[a-zA-Z0-9_-]{1,128}$/.test(runnerId) ? runnerId : undefined)
      controller.signal.throwIfAborted()
      setLive((previous) => previous && { ...previous, phase: 'refreshing', savedRevision: turn.revision })
      try {
        await client.cancelQueries({ queryKey: keyFor(id), exact: true })
        const saved = await client.fetchQuery({ queryKey: keyFor(id),
          queryFn: ({ signal }) => getAssistantSession(id, signal), staleTime: 0 })
        controller.signal.throwIfAborted()
        if (saved.revision === session.revision) throw new ApiError(409, 'assistant_history_pending', '尚未读取到本轮保存的对话。')
      } catch (error) {
        controller.signal.throwIfAborted()
        setLive((previous) => previous && { ...previous, phase: 'sync_failed', syncError: errorMessage(error) })
      }
      return { id }
    },
    onSuccess: (_, { controller }) => {
      if (controller.signal.aborted) return
      setDraft('')
      // Refresh business data without immediately repeating the history request above.
      void client.invalidateQueries({ queryKey: [...privateKey, userId],
        predicate: (query) => query.queryKey[2] !== 'assistant-session' })
    },
    onError: async (_, { controller }) => {
      if (controller.signal.aborted) return
      setLive((previous) => previous && { ...previous, phase: 'failed', approval: undefined })
      await client.invalidateQueries({ queryKey: [...privateKey, userId, 'assistant-session'] })
    },
    onSettled: (_, __, { controller }) => {
      if (inFlight.current === controller) inFlight.current = undefined
    },
  })

  const currentLive = live?.conversationId === sessionId ? live : undefined
  // A newer saved revision replaces the preview, including its optimistic user message.
  const showLive = currentLive !== undefined && transcript.data?.revision === currentLive.baseRevision
  const unreconciled = showLive && currentLive.savedRevision !== undefined
  const approval = showLive ? currentLive.approval : undefined
  const actions = approval?.cards ?? (transcript.data?.status === 'awaiting_approval' ? transcript.data.actions : [])
  const blocked = unreconciled || actions.length > 0 || ['awaiting_approval', 'unavailable', 'running'].includes(transcript.data?.status ?? '')
    || ['assistant_execution_uncertain', 'assistant_legacy_read_only'].includes(transcript.data?.error?.code ?? '')
  const events = showLive ? currentLive.events : undefined
  const message = showLive ? currentLive.message : undefined
  const context = showLive ? currentLive.pageContext : undefined
  const items = useMemo(() => conversationItems(transcript.data?.conversation,
    events ? { events, message, pageContext: context } : undefined), [transcript.data?.conversation, events, message, context])

  function selectSession(id?: string) {
    if (inFlight.current) return
    rememberSession(id)
    setLive(undefined)
    setDraft('')
    send.reset()
  }
  function submit(input: Input) {
    if (inFlight.current || send.isPending || unreconciled || (sessionId !== undefined && !transcript.data)) return
    if (input.type === 'message' && (!model || blocked || !input.message.trim())) return
    if (input.type === 'decision' && (transcript.data?.status !== 'awaiting_approval' || !actions.length)) return
    const controller = new AbortController()
    inFlight.current = controller
    send.mutate({ input, snapshot: transcript.data, controller, modelName: model,
      context: pageContext ? { ...pageContext } : undefined })
  }

  return {
    sessions, transcript, sessionId, selectSession, models, model, setSelectedModel, draft, setDraft,
    items, preview: showLive ? currentLive.text : '', previewFailed: showLive && currentLive.phase === 'failed',
    actions, approvalRevision: approval?.revision ?? transcript.data?.revision,
    displays: displays.filter((item) => item.conversationId === sessionId).map((item) => item.card),
    busy: send.isPending, blocked, error: send.isError ? send.error : undefined,
    syncError: unreconciled && currentLive.phase === 'sync_failed' ? currentLive.syncError : undefined,
    submitMessage: () => submit({ type: 'message', message: draft.trim() }),
    submitDecisions: (decisions: Record<string, boolean>) => submit({ type: 'decision', decisions }),
    refreshTranscript: () => transcript.refetch(),
  }
}

export type AssistantConversationController = ReturnType<typeof useAssistantConversation>
