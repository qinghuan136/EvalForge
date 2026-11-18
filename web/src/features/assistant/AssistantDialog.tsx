import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { privateKey } from '../../app/queryClient'
import { Dialog } from '../../components/ui/Dialog'
import { errorMessage } from '../../lib/api'
import { AssistantApprovals, AssistantRunnerCredential } from './AssistantActionCard'
import { createAssistantSession, getAssistantSession, getAssistantSessions, streamAssistant } from './api'
import { pageContextLabel } from './pageContext'
import type { PageContext } from './pageContext'

export function AssistantDialog({ userId, open, pageContext, onClose }: {
  userId: number; open: boolean; pageContext?: PageContext; onClose: () => void
}) {
  const client = useQueryClient()
  const [sessionId, setSessionId] = useState<number>()
  const [selectedModel, setSelectedModel] = useState('')
  const [draft, setDraft] = useState('')
  const [preview, setPreview] = useState('')
  const sessionsKey = [...privateKey, userId, 'assistant-sessions'] as const
  const transcriptKey = [...privateKey, userId, 'assistant-session', sessionId] as const
  const sessions = useQuery({
    queryKey: sessionsKey,
    queryFn: ({ signal }) => getAssistantSessions(signal),
    enabled: open,
  })
  const transcript = useQuery({
    queryKey: transcriptKey,
    queryFn: ({ signal }) => getAssistantSession(sessionId!, signal),
    enabled: open && sessionId !== undefined,
    refetchInterval: (query) => query.state.data?.status === 'running' ? 1000 : false,
  })
  const models = sessions.data?.models ?? []
  const model = models.includes(selectedModel) ? selectedModel : models[0] ?? ''
  const send = useMutation({
    mutationFn: async (input: { type: 'message'; message: string } | { type: 'decision'; decisions: Record<string, boolean> }) => {
      setPreview('')
      const title = pageContext ? `${pageContextLabel(pageContext)} 对话` : '新对话'
      const session = transcript.data ?? await createAssistantSession(title)
      if (!transcript.data) {
        client.setQueryData([...privateKey, userId, 'assistant-session', session.id], {
          ...session, messages: [], actions: [], status: 'completed', credential_call_ids: [], error: null,
        })
        setSessionId(session.id)
        void client.invalidateQueries({ queryKey: sessionsKey })
      }
      const request = { session_id: session.id, expected_version: session.version }
      const turn = await streamAssistant(input.type === 'message'
        ? { ...request, ...input, model_name: model, page_context: pageContext }
        : { ...request, ...input }, (text) => setPreview((previous) => previous + text))
      return { session, turn }
    },
    onSuccess: async ({ session }) => {
      const key = [...privateKey, userId, 'assistant-session', session.id] as const
      await client.cancelQueries({ queryKey: key, exact: true })
      await client.invalidateQueries({ queryKey: key, exact: true })
      setDraft('')
      void client.invalidateQueries({ queryKey: [...privateKey, userId] })
    },
    onError: async () => { await client.invalidateQueries({ queryKey: [...privateKey, userId, 'assistant-session'] }) },
    onSettled: () => setPreview(''),
  })

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const message = draft.trim()
    if (message && model && !send.isPending && (sessionId === undefined || transcript.data)) {
      send.mutate({ type: 'message', message })
    }
  }

  if (!open) return null

  const busy = send.isPending
  const blocked = transcript.data?.status === 'awaiting_approval' || transcript.data?.status === 'unavailable'
    || transcript.data?.status === 'running' || transcript.data?.error?.code === 'assistant_execution_uncertain'

  return <Dialog title="评测助手" onClose={onClose} busy={busy}>
    <div className="dialog-body">
      <p className="muted">当前上下文：{pageContext ? pageContextLabel(pageContext) : '未选择对象'}</p>
      <label className="field">会话
        <select value={sessionId ?? ''} disabled={busy}
          onChange={(event) => { setSessionId(Number(event.target.value) || undefined); send.reset(); setPreview('') }}>
          <option value="">新对话</option>
          {sessions.data?.items.map((session) => <option key={session.id} value={session.id}>
            {session.title}（#{session.id}）
          </option>)}
        </select>
      </label>
      {sessions.isPending ? <p role="status">正在读取会话…</p> : null}
      {sessions.isError ? <div><p role="alert" className="error-message">{errorMessage(sessions.error)}</p>
        <button className="button" onClick={() => void sessions.refetch()}>重新加载会话</button></div> : null}
      {sessionId !== undefined && transcript.isPending ? <p role="status">正在读取消息…</p> : null}
      {transcript.isError ? <div><p role="alert" className="error-message">{errorMessage(transcript.error)}</p>
        <button className="button" onClick={() => void transcript.refetch()}>重新加载消息</button></div> : null}
      {transcript.data?.messages.length ? <ol className="assistant-messages" aria-label="对话消息" aria-live="polite">
        {transcript.data.messages.map((message) => <li key={message.id}>
          <strong>{message.role === 'user' ? '你' : '助手'}</strong>
          {message.model_name ? <small className="muted"> · {message.model_name}</small> : null}
          {message.page_context ? <small className="muted"> · {pageContextLabel(message.page_context)}</small> : null}
          <p>{message.text}</p>
          {message.source_experiment_id !== null ? <Link to={`/experiments/${message.source_experiment_id}`}>查看数据来源</Link> : null}
        </li>)}
      </ol> : null}
      {preview ? <p aria-label="助手实时回答" aria-live="polite">{preview}</p> : null}
      {transcript.data?.error ? <p role="alert" className="error-message">{transcript.data.error.message}</p> : null}
      {transcript.data?.actions.length ? <AssistantApprovals key={`${sessionId}:${transcript.data.version}`}
        actions={transcript.data.actions} disabled={busy}
        onSubmit={(decisions) => send.mutate({ type: 'decision', decisions })} /> : null}
      {sessionId !== undefined ? transcript.data?.credential_call_ids.map((callId) =>
        <AssistantRunnerCredential key={`${sessionId}:${callId}`} sessionId={sessionId} callId={callId} />) : null}
      {sessions.isSuccess && !models.length ? <p role="status">管理员尚未配置助手模型。</p> : null}
      <form className="dialog-form" onSubmit={submit}>
        {models.length > 1 ? <label className="field">本轮模型
          <select value={model} disabled={busy || blocked} onChange={(event) => setSelectedModel(event.target.value)}>
            {models.map((name) => <option key={name}>{name}</option>)}
          </select>
        </label> : null}
        <label className="field">消息
          <textarea rows={3} maxLength={4000}
            placeholder={pageContext?.type === 'experiment' ? '这次实验跑到哪了？' : '输入你的问题'} value={draft}
            disabled={!model || busy || blocked} onChange={(event) => setDraft(event.target.value)} />
        </label>
        {send.isError ? <p role="alert" className="error-message">{errorMessage(send.error)}</p> : null}
        {send.isPending ? <p role="status">助手正在回答…</p> : null}
        <div className="dialog-actions"><button className="button button-primary" type="submit"
          disabled={!model || !draft.trim() || busy || blocked || (sessionId !== undefined && !transcript.data)}>
          发送
        </button></div>
      </form>
    </div>
  </Dialog>
}
