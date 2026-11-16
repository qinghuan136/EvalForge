import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useLocation, useParams, useSearchParams } from 'react-router'
import { privateKey } from '../../app/queryClient'
import { Dialog } from '../../components/ui/Dialog'
import { errorMessage } from '../../lib/api'
import { DatasetDetail } from '../datasets/DatasetsPage'
import { cancelExperiment, getExperiment, getJob, getJobs, processed, statusLabels } from './api'
import { JobOutputPreview } from './JobOutputPreview'

const jobStatuses = ['queued', 'assigned', 'running', 'succeeded', 'failed', 'cancelled', 'unknown']
const active = (status?: string) => status === 'pending' || status === 'running' || status === 'cancelling'

function JobDialog({ userId, experimentId, jobId, running, streamOutput, onClose }: {
  userId: number; experimentId: number; jobId: string; running: boolean; streamOutput: boolean; onClose: () => void
}) {
  const job = useQuery({
    queryKey: [...privateKey, userId, 'job', experimentId, jobId],
    queryFn: ({ signal }) => getJob(experimentId, jobId, signal),
    refetchInterval: running ? 4000 : false,
  })
  return <Dialog title="Job 详情" onClose={onClose}><div className="dialog-body">
    {job.isPending ? <p role="status">正在加载 Job…</p> : null}
    {job.isError ? <><p className="error-message" role="alert">{errorMessage(job.error)}</p><button className="button" onClick={() => void job.refetch()}>重试</button></> : null}
    {job.data ? <>
      <p>样本 #{job.data.example_id}，第 {job.data.repetition_number} 次，{statusLabels[job.data.status] ?? job.data.status}</p>
      {streamOutput && (job.data.status === 'assigned' || job.data.status === 'running') ?
        <JobOutputPreview key={jobId} userId={userId} experimentId={experimentId} jobId={jobId} /> : null}
      <dl className="detail-list">
        <div><dt>开始时间</dt><dd>{job.data.started_at ? new Date(job.data.started_at).toLocaleString() : '暂无'}</dd></div>
        <div><dt>结束时间</dt><dd>{job.data.ended_at ? new Date(job.data.ended_at).toLocaleString() : '暂无'}</dd></div>
        <div><dt>耗时</dt><dd>{job.data.duration_ms === null ? '暂无' : `${job.data.duration_ms} ms`}</dd></div>
        <div><dt>HTTP 状态</dt><dd>{job.data.http_status ?? '暂无'}</dd></div>
      </dl>
      {job.data.error ? <p className="error-message" role="alert">{job.data.error.code}：{job.data.error.message}</p> : null}
      <div className="example">{([['请求输入', job.data.input], ['参考输出', job.data.reference], ['Agent 实际输出', job.data.output], ['样本元数据', job.data.metadata]] as const).map(([label, value]) => <section key={label}><h5>{label}</h5><pre>{JSON.stringify(value, null, 2)}</pre></section>)}</div>
      <h3>评分结果</h3>
      {job.data.evaluations.length ? job.data.evaluations.map((item, index) => <div className="example" key={index}><h4>{item.name}</h4><p>分数 {item.score ?? '暂无'}，标签 {item.label ?? '暂无'}</p>{item.explanation ? <p>{item.explanation}</p> : null}{item.error ? <p className="error-message">{item.error}</p> : null}</div>) : <p className="muted">暂无已保存的评分。</p>}
    </> : null}
  </div></Dialog>
}

export function ExperimentDetail({ userId }: { userId: number }) {
  const { experimentId } = useParams()
  const id = Number(experimentId)
  const validId = /^[1-9]\d*$/.test(experimentId ?? '') && id <= 2147483647
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  const [jobId, setJobId] = useState<string | null>(null)
  const [showSamples, setShowSamples] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const client = useQueryClient()
  const detail = useQuery({
    queryKey: [...privateKey, userId, 'experiment', id],
    queryFn: ({ signal }) => getExperiment(id, signal), enabled: validId,
    refetchInterval: (query) => active(query.state.data?.status) ? 4000 : false,
  })
  const rawStatus = params.get('status') ?? ''
  const status = jobStatuses.includes(rawStatus) ? rawStatus : ''
  const rawPage = params.get('page')
  const page = rawPage && /^[1-9]\d*$/.test(rawPage) && Number(rawPage) <= 2147483647 ? Number(rawPage) : 1
  const jobs = useQuery({
    queryKey: [...privateKey, userId, 'jobs', id, page, status],
    queryFn: ({ signal }) => getJobs(id, page, status, signal), enabled: validId && !!detail.data,
    refetchInterval: active(detail.data?.status) ? 4000 : false,
  })
  useEffect(() => {
    if (detail.data && !active(detail.data.status)) {
      void client.invalidateQueries({ queryKey: [...privateKey, userId, 'jobs', id] })
      void client.invalidateQueries({ queryKey: [...privateKey, userId, 'job', id] })
    }
  }, [client, detail.data?.status, id, userId])
  const cancel = useMutation({
    mutationFn: () => cancelExperiment(id),
    onSuccess: (result) => {
      client.setQueryData([...privateKey, userId, 'experiment', id], result)
      void client.invalidateQueries({ queryKey: [...privateKey, userId, 'jobs', id] })
      void client.invalidateQueries({ queryKey: [...privateKey, userId, 'experiments', result.dataset_id] })
      setConfirm(false)
    },
  })
  const item = detail.data
  const lastPage = jobs.data ? Math.max(1, Math.ceil(jobs.data.total / jobs.data.pagesize)) : 1
  function update(key: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value); else next.delete(key)
    if (key === 'status') next.delete('page')
    setParams(next)
  }
  const back = (location.state as { returnTo?: string } | null)?.returnTo
  const returnTo = back?.startsWith('/experiments?') ? back : item ? `/experiments?dataset=${item.dataset_id}` : '/experiments'
  if (!validId) return <p className="error-message">实验 ID 无效。</p>
  return <>
    <header className="page-heading"><h1>{item?.name ?? '实验详情'}</h1>{item ? <p>EXP-{item.experiment_id}　数据集 #{item.dataset_id}　版本 #{item.dataset_version_id}　{statusLabels[item.status] ?? item.status}</p> : null}<Link className="button page-action" to={returnTo}>返回实验</Link></header>
    {detail.isPending ? <p role="status">正在加载实验…</p> : null}
    {detail.isError ? <div><p className="error-message" role="alert">{errorMessage(detail.error)}</p><button className="button" onClick={() => void detail.refetch()}>重试</button></div> : null}
    {item ? <>
      <div className="experiment-overview">
        <section className="detail-panel"><h2>执行进度</h2><p className="progress-number">{processed(item.progress)} / {item.progress.job_count}</p><p className="muted">个 Job 已处理</p>
          <dl className="detail-list">{jobStatuses.map((status) => <div key={status}><dt>{statusLabels[status]}</dt><dd>{item.progress[status as keyof typeof item.progress] as number}</dd></div>)}</dl>
          {item.status === 'pending' || item.status === 'running' ? confirm ? <div className="dialog-actions"><span>将停止剩余任务。</span><button className="button" onClick={() => setConfirm(false)} disabled={cancel.isPending}>保留实验</button><button className="button danger" onClick={() => cancel.mutate()} disabled={cancel.isPending}>确认取消</button></div> : <button className="button" onClick={() => setConfirm(true)}>取消实验</button> : null}
          {cancel.error ? <p className="error-message" role="alert">{errorMessage(cancel.error)}</p> : null}
        </section>
        <div className="overview-stack"><section className="detail-panel"><h2>评分进度</h2>{item.progress.evaluations ? <><p>已保存 / 应评 {item.progress.evaluations.completed} / {item.progress.evaluations.expected}</p><p>其中评分错误 {item.progress.evaluations.errors}</p>{item.status === 'cancelled' ? <p className="muted">已保存的评分仍可查看。</p> : null}</> : <p className="muted">暂无评分进度。</p>}</section>
          <section className="detail-panel"><h2>实验配置</h2><dl className="detail-list">
            <div><dt>数据集版本</dt><dd>#{item.dataset_version_id}</dd></div><div><dt>Runner</dt><dd>{item.execution_config?.runner_id ?? '暂无'}</dd></div><div><dt>Agent 目标 ID</dt><dd>{item.execution_config?.agent_target_id ?? '暂无'}</dd></div><div><dt>执行方式</dt><dd>{item.execution_config?.stream_output ? '流式' : '普通'}</dd></div><div><dt>超时</dt><dd>{item.execution_config?.timeout_seconds ?? '暂无'} 秒</dd></div><div><dt>重复次数</dt><dd>{item.repetitions}</dd></div><div><dt>评分配置</dt><dd>{item.evaluators?.map((e) => e.name).join('、') || '未配置'}</dd></div>
          </dl><button className="button" onClick={() => setShowSamples(true)}>查看实验版本样本</button></section></div>
      </div>
      <section className="jobs-section"><div className="jobs-heading"><h2>Job 明细</h2><label className="field">状态<select value={status} onChange={(event) => update('status', event.target.value)}><option value="">全部状态</option>{jobStatuses.map((value) => <option key={value} value={value}>{statusLabels[value]}</option>)}</select></label></div>
        {jobs.isPending ? <p className="panel-message" role="status">正在加载 Job…</p> : null}
        {jobs.isError ? <div className="panel-message"><p className="error-message" role="alert">{errorMessage(jobs.error)}</p><button className="button" onClick={() => void jobs.refetch()}>重试</button></div> : null}
        {jobs.data?.items.length ? <div className="table-scroll"><table className="experiment-table"><thead><tr><th>样本 / 重复</th><th>状态</th><th>耗时</th><th>HTTP</th><th>评分</th></tr></thead><tbody>{jobs.data.items.map((job) => <tr key={job.job_id}><th scope="row"><button className="text-link" onClick={() => setJobId(job.job_id)}>样本 #{job.example_id}</button><small>第 {job.repetition_number} 次</small></th><td>{statusLabels[job.status] ?? job.status}</td><td>{job.duration_ms === null ? '—' : `${job.duration_ms} ms`}</td><td>{job.http_status ?? '—'}</td><td>{job.evaluation_progress.completed} / {job.evaluation_progress.expected}{job.evaluation_progress.errors ? <small>{job.evaluation_progress.errors} 项错误</small> : null}</td></tr>)}</tbody></table></div> : jobs.data ? <p className="panel-message">没有符合此状态的 Job。</p> : null}
        {jobs.data ? <div className="list-footer"><nav className="pagination" aria-label="Job 分页"><button className="button button-quiet" disabled={page <= 1} onClick={() => update('page', String(page - 1))}>上一页</button><span>第 {page} / {lastPage} 页</span><button className="button button-quiet" disabled={page >= lastPage} onClick={() => update('page', String(page + 1))}>下一页</button></nav><span>共 {jobs.data.total} 个 Job</span></div> : null}
      </section>
      {jobId ? <JobDialog userId={userId} experimentId={id} jobId={jobId} running={active(item.status)}
        streamOutput={item.execution_config?.stream_output === true} onClose={() => setJobId(null)} /> : null}
      {showSamples ? <DatasetDetail userId={userId} id={item.dataset_id} versionId={item.dataset_version_id} onClose={() => setShowSamples(false)} /> : null}
    </> : null}
  </>
}
