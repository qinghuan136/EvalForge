import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import CodeMirror, { EditorView } from '@uiw/react-codemirror'
import { python } from '@codemirror/lang-python'
import { json } from '@codemirror/lang-json'
import { Link, useParams } from 'react-router'
import { privateKey } from '../../app/queryClient'
import { Dialog } from '../../components/ui/Dialog'
import { errorMessage } from '../../lib/api'
import { positiveId } from '../experiments/routes'
import { archiveCodeEvaluator, getCodeEvaluator, getVersion, listVersions, previewCode, publishVersion, saveDraft } from './api'
import type { CodeEvaluatorDetail, Preview, PreviewContext } from './api'
import './evaluators.css'

const starterSource = `def evaluate(context):
    actual = context["output"]
    expected = context["reference"]
    return {"score": float(actual == expected)}
`
const starterContext = JSON.stringify({
  input: {}, reference: { status: 'ok' }, output: { status: 'ok' }, metadata: {},
}, null, 2)
const editorTheme = EditorView.theme({
  '&': { color: 'var(--color-text)', backgroundColor: 'var(--color-surface)' },
  '.cm-scroller': { fontFamily: 'var(--font-mono)' },
  '.cm-gutters': { color: 'var(--color-text-muted)', backgroundColor: 'var(--color-surface-muted)',
    borderColor: 'var(--color-border)' },
})
const pythonEditor = [python(), editorTheme, EditorView.contentAttributes.of({ 'aria-label': 'Python 源码' })]
const contextEditor = [json(), editorTheme, EditorView.contentAttributes.of({ 'aria-label': '测试输入 JSON' })]
const historyEditor = [python(), editorTheme, EditorView.contentAttributes.of({ 'aria-label': '历史版本源码' })]

export default function EvaluatorEditor({ userId }: { userId: number }) {
  const { evaluatorId } = useParams()
  const id = positiveId(evaluatorId ?? null)
  const detail = useQuery({
    queryKey: [...privateKey, userId, 'code-evaluator', id],
    queryFn: ({ signal }) => getCodeEvaluator(id!, signal),
    enabled: id !== undefined,
  })

  if (id === undefined) return <p className="panel-message">评分器 ID 无效。 <Link to="/evaluators?kind=code">返回评分器库</Link></p>
  if (detail.data) return <EditorWorkspace key={`${userId}:${id}`} userId={userId} item={detail.data} />
  if (detail.isPending) return <p className="panel-message" role="status">正在加载评分器…</p>
  return <div className="panel-message">
    <p className="error-message" role="alert">{errorMessage(detail.error)}</p>
    <button className="button" onClick={() => void detail.refetch()}>重试</button>{' '}
    <Link className="button" to="/evaluators?kind=code">返回评分器库</Link>
  </div>
}

function EditorWorkspace({ userId, item }: { userId: number; item: CodeEvaluatorDetail }) {
  const client = useQueryClient()
  const [source, setSource] = useState(item.draft_source_code ?? starterSource)
  const [contextText, setContextText] = useState(starterContext)
  const [previewInputError, setPreviewInputError] = useState('')
  const [historyOpen, setHistoryOpen] = useState(false)
  const [archiveOpen, setArchiveOpen] = useState(false)
  const detailKey = [...privateKey, userId, 'code-evaluator', item.id]
  const historyKey = [...privateKey, userId, 'code-evaluator-versions', item.id]
  const archived = item.archived_at !== null
  const dirty = !archived && source !== (item.draft_source_code ?? '')
  const save = useMutation({
    mutationFn: (text: string) => saveDraft(item.id, text),
    onSuccess: ({ draft_source_code }) => {
      client.setQueryData<CodeEvaluatorDetail>(detailKey, (cached) => cached && { ...cached, draft_source_code })
    },
  })
  const publish = useMutation({
    mutationFn: () => publishVersion(item.id),
    onSuccess: (latest_version) => {
      client.setQueryData<CodeEvaluatorDetail>(detailKey, (cached) => cached && { ...cached, latest_version })
      void client.invalidateQueries({ queryKey: historyKey })
      void client.invalidateQueries({ queryKey: [...privateKey, userId, 'code-evaluators'] })
    },
  })
  const preview = useMutation({
    mutationFn: ({ code, context }: { code: string; context: PreviewContext; contextText: string }) => previewCode(code, context),
  })
  const archive = useMutation({
    mutationFn: () => archiveCodeEvaluator(item.id),
    onSuccess: async (detail) => {
      await client.cancelQueries({ queryKey: detailKey, exact: true })
      client.setQueryData(detailKey, detail)
      void client.invalidateQueries({ queryKey: [...privateKey, userId, 'code-evaluators'] })
      setArchiveOpen(false)
    },
  })
  const busy = save.isPending || publish.isPending || archive.isPending
  const validSource = source.trim().length > 0 && Array.from(source).length <= 100_000
  const matchingPreview = preview.variables?.code === source && preview.variables.contextText === contextText
  const writeError = save.error ?? publish.error

  function runPreview() {
    preview.reset()
    let value: unknown
    try {
      value = JSON.parse(contextText)
    } catch {
      setPreviewInputError('测试输入需要是有效的 JSON。')
      return
    }
    if (!isContext(value)) {
      setPreviewInputError('请输入包含 input、reference、output 和 metadata 的 JSON 对象。除 output 外，其余字段都应为对象。')
      return
    }
    setPreviewInputError('')
    preview.mutate({ code: source, context: value, contextText })
  }

  return <div className="evaluator-workspace">
    <header className="evaluator-editor-header">
      <div>
        <Link to="/evaluators?kind=code" onClick={(event) => {
          if (dirty && !window.confirm('离开后将丢失未保存的草稿。继续离开？')) event.preventDefault()
        }}>返回评分器库</Link>
        <h1>{item.name}</h1>
        {item.description && <p className="muted">{item.description}</p>}
        <p className="muted" role="status">Python · {archived ? '已归档' : dirty ? '草稿有未保存修改' : '草稿已保存'}
          {' · '}{item.latest_version ? `已发布 v${item.latest_version.version_number}` : '未发布'}</p>
      </div>
      <div className="evaluator-actions">
        <button className="button" onClick={() => setHistoryOpen(true)}>历史版本</button>
        {!archived && <>
          <button className="button" disabled={busy} onClick={() => { archive.reset(); setArchiveOpen(true) }}>归档</button>
          <button className="button" disabled={!dirty || Array.from(source).length > 100_000 || busy}
            onClick={() => { publish.reset(); save.mutate(source) }}>{save.isPending ? '保存中…' : '保存草稿'}</button>
          <button className="button button-primary" disabled={dirty || busy || !validSource}
            title={dirty ? '请先保存草稿' : undefined}
            onClick={() => { save.reset(); publish.mutate() }}>{publish.isPending ? '发布中…' : '发布版本'}</button>
        </>}
      </div>
    </header>
    {!archived && writeError && <p className="error-message" role="alert">{errorMessage(writeError)}</p>}
    {!archived && Array.from(source).length > 100_000 && <p className="error-message" role="alert">源码最多 100,000 字符。</p>}
    <div className={archived ? undefined : 'evaluator-editor-grid'}>
      <section className="evaluator-pane">
        <h2>evaluator.py</h2>
        <CodeMirror key={archived ? 'archived' : 'draft'} className="evaluator-codemirror"
          value={archived ? item.draft_source_code ?? '' : source} extensions={pythonEditor}
          onChange={archived ? undefined : setSource} height="450px" readOnly={archived || busy} editable={!archived} />
      </section>
      {!archived && <div className="evaluator-preview">
        <section className="evaluator-pane">
          <div className="evaluator-pane-heading">
            <h2>测试输入 · JSON</h2>
            <button className="button" disabled={!validSource || preview.isPending}
              onClick={runPreview}>{preview.isPending ? '试跑中…' : '试跑'}</button>
          </div>
          <CodeMirror className="evaluator-codemirror" value={contextText} extensions={contextEditor} height="225px"
            onChange={(text) => { setContextText(text); setPreviewInputError('') }} />
          {previewInputError && <p className="error-message" role="alert">{previewInputError}</p>}
        </section>
        <section className="detail-panel evaluator-result" aria-live="polite">
          <h2>试跑结果</h2>
          {preview.isPending ? <p className="muted">正在等待试跑结果…</p>
            : preview.isError && matchingPreview ? <p className="error-message">{errorMessage(preview.error)}</p>
              : preview.data && matchingPreview ? <PreviewResult preview={preview.data} />
                : <p className="muted">{preview.data || preview.isError ? '输入已修改，可以重新试跑。' : '点击「试跑」查看结果。'}</p>}
        </section>
      </div>}
    </div>
    {historyOpen && <VersionHistory userId={userId} id={item.id} onClose={() => setHistoryOpen(false)} />}
    {archiveOpen && !archived && <Dialog title="归档评分器" busy={archive.isPending} onClose={() => setArchiveOpen(false)}>
      <div className="dialog-body dialog-form">
        <p>归档后将保留已保存的草稿和发布版本，评分器将转为只读。</p>
        {dirty && <p>当前未保存的修改将丢弃。</p>}
        {archive.isError && <p className="error-message" role="alert">{errorMessage(archive.error)}</p>}
        <div className="dialog-actions">
          <button className="button" disabled={archive.isPending} onClick={() => setArchiveOpen(false)}>取消</button>
          <button className="button button-primary" disabled={archive.isPending}
            onClick={() => archive.mutate()}>{archive.isPending ? '归档中…' : '确认归档'}</button>
        </div>
      </div>
    </Dialog>}
  </div>
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isContext(value: unknown): value is PreviewContext {
  return isRecord(value) && Object.keys(value).length === 4 && 'output' in value
    && isRecord(value.input) && isRecord(value.reference) && isRecord(value.metadata)
}

function PreviewResult({ preview }: { preview: Preview }) {
  return <>
    {preview.simulated && <p className="evaluator-simulation">模拟结果 · 未执行 Python</p>}
    {preview.status === 'succeeded' ? <dl className="detail-list">
      <div><dt>分数</dt><dd>{preview.result.score ?? '—'}</dd></div>
      {preview.result.label !== null && <div><dt>标签</dt><dd>{preview.result.label}</dd></div>}
      {preview.result.explanation !== null && <div><dt>说明</dt><dd>{preview.result.explanation}</dd></div>}
      {!preview.simulated && <div><dt>耗时</dt><dd>{preview.duration_ms} ms</dd></div>}
    </dl> : <p className="error-message">{preview.error.message}</p>}
    {preview.stdout && <pre>{preview.stdout}</pre>}
  </>
}

function VersionHistory({ userId, id, onClose }: { userId: number; id: number; onClose: () => void }) {
  const [page, setPage] = useState(1)
  const [versionId, setVersionId] = useState<number>()
  const versions = useQuery({
    queryKey: [...privateKey, userId, 'code-evaluator-versions', id, page],
    queryFn: ({ signal }) => listVersions(id, page, signal),
  })
  const selected = useQuery({
    queryKey: [...privateKey, userId, 'code-evaluator-version', id, versionId],
    queryFn: ({ signal }) => getVersion(id, versionId!, signal),
    enabled: versionId !== undefined,
  })
  const lastPage = versions.data ? Math.max(1, Math.ceil(versions.data.total / versions.data.pagesize)) : page
  return <Dialog title="历史版本" onClose={onClose}>
    <div className="dialog-body dialog-form">
      {versions.isPending && <p role="status">正在加载版本…</p>}
      {versions.isError && <p className="error-message" role="alert">{errorMessage(versions.error)}{' '}
        <button className="button" onClick={() => void versions.refetch()}>重试</button></p>}
      {versions.data?.total === 0 && <p className="muted">暂无已发布版本。</p>}
      {versions.data && versions.data.total > 0 && <>
        <label className="field"><span>发布版本</span>
          <select value={versionId ?? ''} onChange={(event) => setVersionId(positiveId(event.target.value))}>
            <option value="">请选择版本</option>
            {versions.data.items.map((version) => <option value={version.id} key={version.id}>
              v{version.version_number} · {new Date(version.created_at).toLocaleString()}
            </option>)}
          </select>
        </label>
        {lastPage > 1 && <nav className="pagination" aria-label="版本分页">
          <button className="button button-quiet" disabled={page <= 1}
            onClick={() => { setVersionId(undefined); setPage(page - 1) }}>上一页</button>
          <span>第 {page} / {lastPage} 页</span>
          <button className="button button-quiet" disabled={page >= lastPage}
            onClick={() => { setVersionId(undefined); setPage(page + 1) }}>下一页</button>
        </nav>}
      </>}
      {versionId !== undefined && selected.isPending && <p role="status">正在加载源码…</p>}
      {selected.isError && <p className="error-message" role="alert">{errorMessage(selected.error)}{' '}
        <button className="button" onClick={() => void selected.refetch()}>重试</button></p>}
      {selected.data && <section className="evaluator-pane">
        <h2>v{selected.data.version_number} · 只读</h2>
        <CodeMirror className="evaluator-codemirror" value={selected.data.source_code} extensions={historyEditor}
          height="270px" readOnly editable={false} />
      </section>}
    </div>
  </Dialog>
}
