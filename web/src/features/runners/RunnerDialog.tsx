import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { privateKey } from '../../app/queryClient'
import { Dialog } from '../../components/ui/Dialog'
import { errorMessage } from '../../lib/api'
import { AskAssistantButton } from '../assistant/AskAssistantButton'
import { createRunner, deleteRunner, getRunner, renameRunner } from './api'
import type { RunnerCreated } from './api'

export function RunnerDialog({ userId, id, onClose }: { userId: number; id: string | null; onClose: () => void }) {
  const client = useQueryClient()
  const [name, setName] = useState('')
  const [created, setCreated] = useState<RunnerCreated | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [copied, setCopied] = useState(false)
  const detail = useQuery({
    queryKey: [...privateKey, userId, 'runner', id],
    queryFn: ({ signal }) => getRunner(id!, signal),
    enabled: id !== null,
  })
  const refresh = () => client.invalidateQueries({ queryKey: [...privateKey, userId, 'runners'] })
  const create = useMutation({ mutationFn: createRunner, onSuccess: (result) => { setCreated(result); void refresh() } })
  const rename = useMutation({
    mutationFn: (value: string) => renameRunner(id!, value),
    onSuccess: (runner) => { client.setQueryData([...privateKey, userId, 'runner', id], runner); setName(''); void refresh() },
  })
  const remove = useMutation({ mutationFn: () => deleteRunner(id!), onSuccess: () => { void refresh(); onClose() } })
  const busy = create.isPending || rename.isPending || remove.isPending
  const error = create.error || rename.error || remove.error

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const value = name.trim()
    if (value) {
      if (id) rename.mutate(value)
      else create.mutate(value)
    }
  }

  return (
    <Dialog title={created ? 'Runner 已创建' : id ? 'Runner 详情' : '创建 Runner'} onClose={onClose} busy={busy}>
      <div className="dialog-body">
        {created ? (
          <>
            <p>请复制并保存凭证，完整凭证仅在创建时显示一次。</p>
            <label className="field">Runner ID<input readOnly value={created.runner.runner_id} /></label>
            <label className="field">凭证<textarea readOnly rows={3} value={created.credential.token} /></label>
            <button className="button" type="button" onClick={async () => {
              try { await navigator.clipboard.writeText(created.credential.token); setCopied(true) }
              catch { setCopied(false) }
            }}>{copied ? '已复制' : '复制凭证'}</button>
            <p className="muted">连接地址 {created.connection.websocket_url}</p>
            <p className="muted">凭证有效期至 {new Date(created.credential.expires_at).toLocaleString()}</p>
          </>
        ) : id ? (
          <>
            {detail.isPending ? <p role="status">正在加载…</p> : null}
            {detail.isError ? <><p className="error-message" role="alert">{errorMessage(detail.error)}</p><button className="button" onClick={() => void detail.refetch()}>重试</button></> : null}
            {detail.data ? (
              <>
                <dl className="detail-list">
                  <div><dt>Runner ID</dt><dd>{detail.data.runner_id}</dd></div>
                  <div><dt>连接状态</dt><dd>{!detail.data.enabled ? '已停用' : !detail.data.connection.online ? '离线' : ({ connected: '已连接', syncing: '同步中', ready: '已就绪', disconnected: '离线' })[detail.data.connection.phase]}</dd></div>
                  <div><dt>最近活动</dt><dd>{detail.data.last_seen_at ? new Date(detail.data.last_seen_at).toLocaleString() : '暂无'}</dd></div>
                  <div><dt>已声明能力</dt><dd>{detail.data.connection.capabilities.join('、') || '无'}</dd></div>
                  <div><dt>流式输出</dt><dd>{detail.data.connection.capabilities.includes('http_json') && detail.data.connection.capabilities.includes('http_sse') ? '支持' : '当前不可用'}</dd></div>
                </dl>
                <div><AskAssistantButton pageContext={{ type: 'runner', id }} /></div>
                <form className="dialog-form" onSubmit={submit}>
                  <label className="field">名称<input value={name} onChange={(event) => setName(event.target.value)} placeholder={detail.data.name} maxLength={100} required /></label>
                  <button className="button" disabled={busy}>保存名称</button>
                </form>
                {confirmDelete ? (
                  <div className="dialog-actions"><span>删除后凭证将失效。</span><button className="button" disabled={busy} onClick={() => setConfirmDelete(false)}>取消</button><button className="button danger" disabled={busy} onClick={() => remove.mutate()}>确认删除</button></div>
                ) : <button className="button danger" disabled={busy} onClick={() => setConfirmDelete(true)}>删除 Runner</button>}
              </>
            ) : null}
          </>
        ) : (
          <form className="dialog-form" onSubmit={submit}>
            <label className="field">名称<input value={name} onChange={(event) => setName(event.target.value)} maxLength={100} required autoFocus /></label>
            <button className="button button-primary" disabled={busy}>{busy ? '创建中…' : '创建'}</button>
          </form>
        )}
        {error ? <p className="error-message" role="alert">{errorMessage(error)}</p> : null}
      </div>
    </Dialog>
  )
}
