import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { privateKey } from '../../app/queryClient'
import { jobEventsUrl } from './api'

const maxTextLength = 256 * 1024
const retryDelay = 4000

type Preview = {
  attempt_id: string
  text: string
  truncated: boolean
  has_gap: boolean
}

export function JobOutputPreview({ userId, experimentId, jobId }: {
  userId: number; experimentId: number; jobId: string
}) {
  const client = useQueryClient()
  const [preview, setPreview] = useState<Preview>()
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    let source: EventSource | undefined
    let retry: number | undefined
    function refreshJob() {
      void client.invalidateQueries({ queryKey: [...privateKey, userId, 'job', experimentId, jobId] })
    }
    function connect() {
      const stream = new EventSource(jobEventsUrl(experimentId, jobId))
      source = stream
      stream.addEventListener('snapshot', (event: MessageEvent<string>) => {
        setPreview(JSON.parse(event.data) as Preview)
        setConnected(true)
      })
      stream.addEventListener('delta', (event: MessageEvent<string>) => {
        const delta = JSON.parse(event.data) as Preview
        setPreview((previous) => {
          const text = previous?.attempt_id === delta.attempt_id ? previous.text + delta.text : delta.text
          return { ...delta, text: text.slice(-maxTextLength), truncated: delta.truncated || text.length > maxTextLength }
        })
      })
      stream.addEventListener('done', () => {
        stream.close()
        refreshJob()
      })
      function reconnect() {
        stream.close()
        setConnected(false)
        refreshJob()
        retry = window.setTimeout(connect, retryDelay)
      }
      stream.addEventListener('interrupted', reconnect)
      stream.onerror = reconnect
    }
    connect()
    return () => {
      source?.close()
      window.clearTimeout(retry)
    }
  }, [client, userId, experimentId, jobId])

  return <section className="example">
    <h3>实时输出</h3>
    {!connected ? <p className="muted" role="status">正在连接实时预览…</p> : null}
    {preview?.truncated ? <p className="muted">较早的预览片段已省略。</p> : null}
    {preview?.has_gap ? <p className="muted">实时预览中有片段缺失。</p> : null}
    {preview?.text ? <pre>{preview.text}</pre> : connected ? <p className="muted">等待 Agent 输出…</p> : null}
  </section>
}
