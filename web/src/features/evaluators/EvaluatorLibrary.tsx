import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import { privateKey } from '../../app/queryClient'
import { Dialog } from '../../components/ui/Dialog'
import { errorMessage } from '../../lib/api'
import { getBuiltinEvaluators } from '../experiments/api'
import { positiveId } from '../experiments/routes'
import { createCodeEvaluator, listCodeEvaluators } from './api'
import './evaluators.css'

const builtinNames: Record<string, string> = {
  exact_match: '精确匹配', contains: '包含文本', regex: '正则匹配',
  levenshtein_distance: '编辑距离', json_distance: 'JSON 差异',
}
const codeListUrl = '/evaluators?kind=code'

function useBuiltinCatalog(userId: number) {
  return useQuery({
    queryKey: [...privateKey, userId, 'builtin-evaluators'],
    queryFn: ({ signal }) => getBuiltinEvaluators(signal),
  })
}

export function EvaluatorLibrary({ userId }: { userId: number }) {
  const [params, setParams] = useSearchParams()
  const isCode = params.get('kind') === 'code'
  const page = positiveId(params.get('page')) ?? 1
  const includeArchived = params.get('include_archived') === 'true'
  const [creating, setCreating] = useState(false)

  function changeList(nextPage: number, showArchived: boolean) {
    const next = new URLSearchParams({ kind: 'code' })
    if (nextPage > 1) next.set('page', String(nextPage))
    if (showArchived) next.set('include_archived', 'true')
    setParams(next)
  }

  return <>
    <header className="page-heading">
      <h1>评分器库</h1>
      {isCode && <button className="button button-primary page-action" onClick={() => setCreating(true)}>新建评分器</button>}
    </header>
    <nav className="evaluator-tabs" aria-label="评分器分类">
      <Link to="/evaluators" aria-current={isCode ? undefined : 'page'}>系统评分器</Link>
      <Link to={codeListUrl} aria-current={isCode ? 'page' : undefined}>自定义评分器</Link>
    </nav>
    {isCode ? <>
      <label className="evaluator-archive-filter">
        <input type="checkbox" checked={includeArchived}
          onChange={(event) => changeList(1, event.target.checked)} />
        包含已归档
      </label>
      <CodeCatalog userId={userId} page={page} includeArchived={includeArchived}
        onPage={(next) => changeList(next, includeArchived)} />
    </> : <BuiltinCatalog userId={userId} />}
    {isCode && creating && <CreateEvaluatorDialog userId={userId} onClose={() => setCreating(false)} />}
  </>
}

function BuiltinCatalog({ userId }: { userId: number }) {
  const catalog = useBuiltinCatalog(userId)
  if (catalog.isPending) return <p className="panel-message" role="status">正在加载系统评分器…</p>
  if (catalog.isError) return <p className="panel-message" role="alert">{errorMessage(catalog.error)}{' '}
    <button className="button" onClick={() => void catalog.refetch()}>重试</button></p>
  if (!catalog.data.length) return <p className="panel-message">暂无系统评分器。</p>
  return <div className="table-scroll"><table className="experiment-table evaluator-table">
    <thead><tr><th scope="col">名称</th><th scope="col">说明</th></tr></thead>
    <tbody>{catalog.data.map((rule) => <tr key={rule.key}>
      <th scope="row"><Link to={`/evaluators/builtin/${encodeURIComponent(rule.key)}`}>{builtinNames[rule.key] ?? rule.key}</Link>
        <small>{rule.key}</small></th><td>{rule.description}</td>
    </tr>)}</tbody>
  </table></div>
}

export function BuiltinEvaluatorPage({ userId }: { userId: number }) {
  const { key } = useParams()
  const catalog = useBuiltinCatalog(userId)
  const rule = catalog.data?.find((item) => item.key === key)
  return <>
    <header className="page-heading">
      <h1>{rule ? builtinNames[rule.key] ?? rule.key : '系统评分器'}</h1>
      {rule && <p>{rule.description}</p>}
      <Link className="button page-action" to="/evaluators">返回评分器库</Link>
    </header>
    {catalog.isPending && <p role="status">正在加载规则…</p>}
    {catalog.isError && <p className="error-message" role="alert">{errorMessage(catalog.error)}{' '}
      <button className="button" onClick={() => void catalog.refetch()}>重试</button></p>}
    {catalog.isSuccess && !rule && <p className="panel-message">未找到该系统评分器。</p>}
    {rule && <div className="evaluator-schema">
      <section className="detail-panel"><h2>输入定义</h2><pre>{JSON.stringify(rule.input_schema, null, 2)}</pre></section>
      <section className="detail-panel"><h2>输出定义</h2><pre>{JSON.stringify(rule.output_configs, null, 2)}</pre></section>
    </div>}
  </>
}

function CodeCatalog({ userId, page, includeArchived, onPage }: {
  userId: number; page: number; includeArchived: boolean; onPage: (page: number) => void
}) {
  const list = useQuery({
    queryKey: [...privateKey, userId, 'code-evaluators', page, includeArchived],
    queryFn: ({ signal }) => listCodeEvaluators(page, includeArchived, signal),
  })
  if (list.isPending) return <p className="panel-message" role="status">正在加载自定义评分器…</p>
  if (list.isError) return <p className="panel-message" role="alert">{errorMessage(list.error)}{' '}
    <button className="button" onClick={() => void list.refetch()}>重试</button></p>
  const { items, total, pagesize } = list.data
  const lastPage = Math.max(1, Math.ceil(total / pagesize))
  return <>
    {items.length > 0 ? <div className="table-scroll"><table className="experiment-table evaluator-table">
      <thead><tr><th scope="col">名称</th><th scope="col">说明</th><th scope="col">最新发布</th></tr></thead>
      <tbody>{items.map((item) => <tr key={item.id}>
        <th scope="row"><Link to={`/evaluators/code/${item.id}`}>{item.name}</Link>
          {item.archived_at && <small>已归档</small>}</th>
        <td>{item.description || '—'}</td>
        <td>{item.latest_version ? `v${item.latest_version.version_number}` : '未发布'}</td>
      </tr>)}</tbody>
    </table></div> : <p className="panel-message">{total === 0 ? '暂无自定义评分器。' : '当前页没有评分器。'}</p>}
    {(page > 1 || lastPage > 1) && <div className="list-footer">
      <nav className="pagination" aria-label="评分器分页">
        <button className="button button-quiet" disabled={page <= 1} onClick={() => onPage(Math.min(page - 1, lastPage))}>上一页</button>
        <span>第 {page} / {lastPage} 页</span>
        <button className="button button-quiet" disabled={page >= lastPage} onClick={() => onPage(page + 1)}>下一页</button>
      </nav>
      <span>共 {total} 个评分器</span>
    </div>}
  </>
}

function CreateEvaluatorDialog({ userId, onClose }: { userId: number; onClose: () => void }) {
  const navigate = useNavigate()
  const client = useQueryClient()
  const [name, setName] = useState('')
  const create = useMutation({
    mutationFn: createCodeEvaluator,
    onSuccess: (item) => {
      client.setQueryData([...privateKey, userId, 'code-evaluator', item.id], item)
      void client.invalidateQueries({ queryKey: [...privateKey, userId, 'code-evaluators'] })
      onClose()
      navigate(`/evaluators/code/${item.id}`)
    },
  })

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!name.trim() || create.isPending) return
    const fields = new FormData(event.currentTarget)
    create.mutate({ name: name.trim(), description: String(fields.get('description')).trim() || null })
  }

  return <Dialog title="新建自定义评分器" busy={create.isPending} onClose={onClose}>
    <form className="dialog-body dialog-form" onSubmit={submit}>
      <label className="field">名称<input autoFocus value={name} maxLength={100} required
        onChange={(event) => setName(event.target.value)} /></label>
      <label className="field">说明（可选）<textarea name="description" rows={3} maxLength={2000} /></label>
      {create.isError && <p className="error-message" role="alert">{errorMessage(create.error)}</p>}
      <div className="dialog-actions">
        <button type="button" className="button" disabled={create.isPending} onClick={onClose}>取消</button>
        <button className="button button-primary" disabled={!name.trim() || create.isPending}>
          {create.isPending ? '创建中…' : '创建'}
        </button>
      </div>
    </form>
  </Dialog>
}
