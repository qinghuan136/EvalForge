import { useLayoutEffect, useRef, useState } from 'react'
import { errorMessage } from '../../lib/api'
import { AssistantApprovals, AssistantDisplayCard } from './AssistantActionCard'
import { AssistantMessages } from './AssistantMessages'
import { pageContextLabel } from './pageContext'
import type { PageContext } from './pageContext'
import type { AssistantConversationController } from './useAssistantConversation'
import type { AssistantLayoutController } from './useAssistantLayout'

export function AssistantPanel({ open, pageContext, contextLabel, onClose, conversation: chat, layout }: {
  open: boolean; pageContext?: PageContext; contextLabel?: string; onClose: () => void
  conversation: AssistantConversationController; layout: AssistantLayoutController
}) {
  const panel = useRef<HTMLElement>(null)
  const body = useRef<HTMLDivElement>(null)
  const follow = useRef(true)
  const [unread, setUnread] = useState(false)
  const { sessions, transcript, sessionId, busy, blocked } = chat
  function latest() {
    follow.current = true
    setUnread(false)
    if (body.current) body.current.scrollTop = body.current.scrollHeight
  }
  useLayoutEffect(() => {
    if (open) { follow.current = true; setUnread(false); panel.current?.focus({ preventScroll: true }) }
  }, [open, sessionId])
  useLayoutEffect(() => {
    if (!open) return
    if (follow.current) latest()
    else setUnread(true)
  }, [open, chat.items, chat.preview, chat.displays.length, chat.actions, layout.size.height])

  return <section ref={panel} id="assistant-panel" className="assistant-panel" role="dialog" aria-modal="false"
    aria-labelledby="assistant-panel-title" hidden={!open} tabIndex={-1}
    style={{ width: layout.size.width, height: layout.size.height }}
    onKeyDown={(event) => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onClose() } }}>
    <button className="assistant-resize" type="button" {...layout.resizeHandlers} aria-label="调整助手面板大小"
      title="拖动左上角或使用方向键调整大小">
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" aria-hidden="true"><path d="M2 8V2h6M2 2l7 7" /></svg>
    </button>
    <header className="assistant-panel-header">
      <div><h2 id="assistant-panel-title">评测助手</h2>{busy ? <span className="muted">正在回答…</span> : null}</div>
      <div className="assistant-header-actions">
        <button className="button button-quiet" type="button" disabled={busy} onClick={() => chat.selectSession()}>新对话</button>
        <button className="button button-quiet" type="button" onClick={onClose} aria-label="收起助手面板">收起</button>
      </div>
    </header>
    <div className="assistant-context" aria-label="当前页面上下文">
      <span className="muted">当前上下文</span>
      <strong>{contextLabel ?? (pageContext ? pageContextLabel(pageContext) : '未关联具体对象')}</strong>
      {contextLabel && pageContext ? <small className="muted">{pageContextLabel(pageContext)}</small> : null}
    </div>
    <div className="assistant-record">
      <label className="field">会话<select aria-label="会话" value={sessionId ?? ''} disabled={busy}
        onChange={(event) => chat.selectSession(event.target.value || undefined)}>
        <option value="">新对话</option>
        {sessions.data?.items.map((session) => <option key={session.conversation_id} value={session.conversation_id}>{session.title}</option>)}
      </select></label>
      <button className="button button-quiet" type="button" onClick={layout.reset} aria-label="重置助手布局" title="恢复入口位置和面板大小">重置布局</button>
    </div>
    <div ref={body} className="assistant-panel-body" onScroll={() => {
      const element = body.current
      if (element) { follow.current = element.scrollHeight - element.scrollTop - element.clientHeight < 48; if (follow.current) setUnread(false) }
    }}>
      {sessions.isPending ? <p role="status">正在读取会话…</p> : null}
      {sessions.isError ? <div><p role="alert" className="error-message">{errorMessage(sessions.error)}</p>
        <button className="button" onClick={() => void sessions.refetch()}>重新加载会话</button></div> : null}
      {sessionId !== undefined && transcript.isPending ? <p role="status">正在读取消息…</p> : null}
      {transcript.isError ? <div><p role="alert" className="error-message">{errorMessage(transcript.error)}</p>
        <button className="button" onClick={() => void chat.refreshTranscript()}>重新加载消息</button></div> : null}
      {!chat.items.length && !busy && !transcript.isFetching ? <p className="assistant-empty muted">输入问题，开始新对话。</p> : null}
      <AssistantMessages items={chat.items} preview={chat.preview} failed={chat.previewFailed} />
      {chat.syncError ? <div><p role="alert" className="error-message">本轮已保存，历史尚未同步：{chat.syncError}</p>
        <button className="button" disabled={transcript.isFetching} onClick={() => void chat.refreshTranscript()}>重新读取已保存的对话</button></div> : null}
      {transcript.data?.error ? <p role="alert" className="error-message">{transcript.data.error.message}</p> : null}
      {chat.actions.length ? <AssistantApprovals key={`${sessionId}:${chat.approvalRevision}`}
        actions={chat.actions} disabled={busy || !!chat.syncError} onSubmit={chat.submitDecisions} /> : null}
      {chat.displays.map((card) => <AssistantDisplayCard key={card.id} card={card} />)}
      {sessions.isSuccess && !chat.models.length ? <p role="status">管理员尚未配置助手模型。</p> : null}
      {unread ? <button className="button assistant-latest" type="button" onClick={latest}>回到最新</button> : null}
    </div>
    <form className="assistant-composer" onSubmit={(event) => { event.preventDefault(); chat.submitMessage() }}>
      {chat.error ? <p role="alert" className="error-message">{errorMessage(chat.error)}</p> : null}
      <label className="field"><span className="assistant-sr-only">消息</span>
        <textarea aria-label="消息" rows={3} maxLength={4000} placeholder="输入你的问题…" value={chat.draft}
          disabled={!chat.model || busy || blocked} onChange={(event) => chat.setDraft(event.target.value)} />
      </label>
      <div className="assistant-composer-actions">
        <label className="field"><span className="assistant-sr-only">本轮模型</span><select aria-label="本轮模型" value={chat.model}
          disabled={busy || blocked || !chat.models.length} onChange={(event) => chat.setSelectedModel(event.target.value)}>
          {chat.models.map((name) => <option key={name}>{name}</option>)}
        </select></label>
        <button className="button button-primary" type="submit"
          disabled={!chat.model || !chat.draft.trim() || busy || blocked || (sessionId !== undefined && !transcript.data)}>发送</button>
      </div>
    </form>
  </section>
}
