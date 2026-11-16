import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, Navigate, useSearchParams } from 'react-router'
import { privateKey } from '../../app/queryClient'
import { Dialog } from '../../components/ui/Dialog'
import { errorMessage } from '../../lib/api'
import { getDataset, getDatasets, getExamples } from './api'
import { ImportDialog } from './ImportDialog'

export function DatasetDetail({ userId, id, onClose, versionId }: { userId: number; id: number; onClose: () => void; versionId?: number }) {
  const [page, setPage] = useState(1)
  const detail = useQuery({ queryKey: [...privateKey, userId, 'dataset', id], queryFn: ({ signal }) => getDataset(id, signal) })
  const examples = useQuery({ queryKey: [...privateKey, userId, 'examples', id, page, versionId], queryFn: ({ signal }) => getExamples(id, page, signal, versionId) })
  const lastPage = examples.data ? Math.max(1, Math.ceil(examples.data.total / examples.data.pagesize)) : 1
  return (
    <Dialog title={detail.data?.name ?? '数据集详情'} onClose={onClose}>
      <div className="dialog-body">
        {detail.isPending ? <p role="status">正在加载数据集…</p> : null}
        {detail.isError ? <><p className="error-message" role="alert">{errorMessage(detail.error)}</p><button className="button" onClick={() => void detail.refetch()}>重试</button></> : null}
        {detail.data ? <><p className="muted">{detail.data.description || '无描述'}</p><p>{versionId ? `实验版本 #${versionId}` : detail.data.latest_version_id === null ? '暂无版本' : `最新版本 #${detail.data.latest_version_id}`}，共 {examples.data?.total ?? '…'} 条样本</p></> : null}
        <h3>样本</h3>
        {examples.isPending ? <p role="status">正在加载样本…</p> : null}
        {examples.isError ? <><p className="error-message" role="alert">{errorMessage(examples.error)}</p><button className="button" onClick={() => void examples.refetch()}>重试</button></> : null}
        {examples.data?.items.length === 0 ? <p className="muted">此版本暂无样本。</p> : null}
        {examples.data?.items.map((example) => (
          <article className="example" key={example.example_id}>
            <h4>样本 #{example.example_id}</h4>
            {([['请求输入', example.input], ['参考输出', example.output], ['样本元数据', example.metadata]] as const).map(([label, value]) => (
              <section key={label}><h5>{label}</h5><pre>{JSON.stringify(value, null, 2)}</pre></section>
            ))}
          </article>
        ))}
        {examples.data && lastPage > 1 ? <nav className="pagination" aria-label="样本分页"><button className="button" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button><span>第 {page} / {lastPage} 页</span><button className="button" disabled={page >= lastPage} onClick={() => setPage(page + 1)}>下一页</button></nav> : null}
      </div>
    </Dialog>
  )
}

export function DatasetsPage({ userId }: { userId: number }) {
  const [params] = useSearchParams()
  const raw = params.get('page')
  const parsed = Number(raw)
  const page = raw && /^[1-9]\d*$/.test(raw) && parsed <= 2147483647 ? parsed : 1
  const [dialog, setDialog] = useState<'import' | number | null>(null)
  const datasets = useQuery({ queryKey: [...privateKey, userId, 'datasets', page], queryFn: ({ signal }) => getDatasets(page, signal) })
  const data = datasets.data
  const lastPage = data ? Math.max(1, Math.ceil(data.total / data.pagesize)) : 1
  function pageLink(target: number) {
    const next = new URLSearchParams(params)
    if (target === 1) next.delete('page')
    else next.set('page', String(target))
    return { pathname: '/datasets', search: next.toString() }
  }
  if (data && page > lastPage) return <Navigate replace to={pageLink(lastPage)} />
  return (
    <>
      <header className="page-heading">
        <h1>数据集</h1><p>导入评测样本，查看最新版本的输入和参考输出。</p>
        <button className="button button-primary page-action" onClick={() => setDialog('import')}>导入数据集</button>
      </header>
      <section className="runner-panel" aria-label="数据集列表" aria-busy={datasets.isFetching}>
        {datasets.isPending ? <p className="panel-message" role="status">正在加载数据集…</p> : null}
        {datasets.isError ? <div className="panel-message"><p className="error-message" role="alert">{errorMessage(datasets.error)}</p><button className="button" onClick={() => void datasets.refetch()}>重试</button></div> : null}
        {data?.items.length ? <ul className="dataset-list">{data.items.map((dataset) => (
          <li key={dataset.dataset_id}><button className="dataset-row" onClick={() => setDialog(dataset.dataset_id)}>
            <span><strong>{dataset.name}</strong><small>{dataset.description || '无描述'}</small></span>
            <span className="muted">{dataset.latest_version_id === null ? '暂无版本' : `版本 #${dataset.latest_version_id}`}</span>
          </button></li>
        ))}</ul> : data ? <p className="panel-message">还没有数据集。</p> : null}
        {data ? <div className="list-footer">
          {lastPage > 1 ? <nav className="pagination" aria-label="数据集分页">
            {page > 1 ? <Link className="button button-quiet" to={pageLink(page - 1)}>上一页</Link> : <button className="button button-quiet" disabled>上一页</button>}
            <span>第 {page} / {lastPage} 页</span>
            {page < lastPage ? <Link className="button button-quiet" to={pageLink(page + 1)}>下一页</Link> : <button className="button button-quiet" disabled>下一页</button>}
          </nav> : null}<span className="runner-total">共 {data.total} 个数据集</span>
        </div> : null}
      </section>
      {dialog === 'import' ? <ImportDialog userId={userId} onClose={() => setDialog(null)} onImported={(id) => setDialog(id)} /> : null}
      {typeof dialog === 'number' ? <DatasetDetail userId={userId} id={dialog} onClose={() => setDialog(null)} /> : null}
    </>
  )
}
