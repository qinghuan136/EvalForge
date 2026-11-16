import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { privateKey } from '../../app/queryClient'
import { Dialog } from '../../components/ui/Dialog'
import { ApiError, errorMessage } from '../../lib/api'
import { createAssistantSession, getAssistantSession, getAssistantSessions, sendAssistantMessage } from './api'
import { pageContextLabel } from './pageContext'
import type { PageContext } from './pageContext'

export function AssistantDialog({ userId, open, pageContext, onClose }: {
  userId: number; open: boolean; pageContext?: PageContext; onClose: () => void
}) {
  const client = useQueryClient()
  const [sessionId, setSessionId] = useState<number>()
  const [selectedModel, setSelectedModel] = useState('')
  const [draft, setDraft] = useState('')
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
  })
  const models = sessions.data?.models ?? []
  const model = models.includes(selectedModel) ? selectedModel : models[0] ?? ''
  const send = useMutation({
    mutationFn: async ({ message, modelName, context }: { message: string; modelName: string; context?: PageContext }) => {
      const title = context ? `${pageContextLabel(context)} 对话` : '新对话'
      const session = transcript.data ?? await createAssistantSession(title)
      if (!transcript.data) {
        client.setQueryData([...privateKey, userId, 'assistant-session', session.id], { ...session, messages: [] })
        setSessionId(session.id)
        void client.invalidateQueries({ queryKey: sessionsKey })
      }
      const turn = await sendAssistantMessage(session.id, session.version, message, modelName, context)
      return { session, turn, previous: transcript.data?.messages ?? [] }
    },
    onSuccess: async ({ session, turn, previous }) => {
      const key = [...privateKey, userId, 'assistant-session', session.id] as const
      await client.cancelQueries({ queryKey: key, exact: true })
      client.setQueryData<typeof transcript.data>(key, (current) => {
        if (current && current.version >= turn.version) return current
        return { ...session, version: turn.version, messages: [...previous, ...turn.messages].slice(-100) }
      })
      setDraft('')
      void client.invalidateQueries({ queryKey: sessionsKey })
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        void client.invalidateQueries({ queryKey: [...privateKey, userId, 'assistant-session'] })
      }
    },
  })

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const message = draft.trim()
    if (message && model && !send.isPending && (sessionId === undefined || transcript.data)) {
      send.mutate({ message, modelName: model, context: pageContext })
    }
  }

  if (!open) return null

  return <Dialog title="评测助手" onClose={onClose} busy={send.isPending}>
    <div className="dialog-body">
      <p className="muted">当前上下文：{pageContext ? pageContextLabel(pageContext) : '未选择对象'}</p>
      <label className="field">会话
        <select value={sessionId ?? ''} disabled={send.isPending}
          onChange={(event) => { setSessionId(Number(event.target.value) || undefined); send.reset() }}>
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
      {sessions.isSuccess && !models.length ? <p role="status">管理员尚未配置助手模型。</p> : null}
      <form className="dialog-form" onSubmit={submit}>
        {models.length > 1 ? <label className="field">本轮模型
          <select value={model} disabled={send.isPending} onChange={(event) => setSelectedModel(event.target.value)}>
            {models.map((name) => <option key={name}>{name}</option>)}
          </select>
        </label> : null}
        <label className="field">消息
          <textarea rows={3} maxLength={4000}
            placeholder={pageContext?.type === 'experiment' ? '这次实验跑到哪了？' : '输入你的问题'} value={draft}
            disabled={!model || send.isPending} onChange={(event) => setDraft(event.target.value)} />
        </label>
        {send.isError ? <p role="alert" className="error-message">{errorMessage(send.error)}</p> : null}
        {send.isPending ? <p role="status">助手正在回答…</p> : null}
        <div className="dialog-actions"><button className="button button-primary" type="submit"
          disabled={!model || !draft.trim() || send.isPending || (sessionId !== undefined && !transcript.data)}>
          发送
        </button></div>
      </form>
    </div>
  </Dialog>
}
