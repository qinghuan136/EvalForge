import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { privateKey } from '../../app/queryClient'
import { Dialog } from '../../components/ui/Dialog'
import { errorMessage } from '../../lib/api'
import { importDataset } from './api'

function csvHeader(text: string) {
  const columns: string[] = []
  let value = ''
  let quoted = false
  let closed = false
  for (let i = 0; i < text.length; i++) {
    const char = text[i]
    if (quoted) {
      if (char === '"' && text[i + 1] === '"') { value += '"'; i++ }
      else if (char === '"') { quoted = false; closed = true }
      else value += char
    } else if (char === ',' || char === '\n' || char === '\r') {
      columns.push(value)
      value = ''
      closed = false
      if (char !== ',') break
    } else if (char === '"' && !value && !closed) quoted = true
    else if (closed || char === '"') throw new Error('CSV 表头引号格式不正确。')
    else value += char
    if (i === text.length - 1) columns.push(value)
  }
  if (quoted || !columns.length || columns.length > 256 || columns.some((column) => !column.trim() || column.includes('\0')) || new Set(columns).size !== columns.length) {
    throw new Error('CSV 表头须包含 1–256 个不重复的非空列名。')
  }
  return columns
}

export function ImportDialog({ userId, onClose, onImported }: {
  userId: number; onClose: () => void; onImported: (id: number) => void
}) {
  const client = useQueryClient()
  const [format, setFormat] = useState<'csv' | 'jsonl'>('csv')
  const [file, setFile] = useState<File | null>(null)
  const [columns, setColumns] = useState<string[]>([])
  const [groups, setGroups] = useState<Record<string, string>>({})
  const [localError, setLocalError] = useState('')
  const [reading, setReading] = useState(false)
  const readVersion = useRef(0)
  const mutation = useMutation({
    mutationFn: importDataset,
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: [...privateKey, userId, 'datasets'] })
      onImported(result.dataset_id)
    },
  })

  async function chooseFile(selected: File | null) {
    const version = ++readVersion.current
    setFile(selected)
    setColumns([])
    setGroups({})
    setLocalError('')
    setReading(false)
    if (!selected) return
    if (selected.size > 5 * 1024 * 1024) { setLocalError('文件超过 5 MiB。'); return }
    if (format === 'csv') {
      setReading(true)
      try {
        const text = new TextDecoder('utf-8', { fatal: true }).decode(await selected.arrayBuffer()).replace(/^\uFEFF/, '')
        const header = csvHeader(text)
        if (version === readVersion.current) setColumns(header)
      } catch (error) {
        if (version === readVersion.current) setLocalError(error instanceof TypeError ? '无法读取文件，请确认编码是 UTF-8。' : error instanceof Error ? error.message : '无法读取文件。')
      } finally { if (version === readVersion.current) setReading(false) }
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!file || localError || reading) return
    if (format === 'csv' && !columns.some((column) => groups[column] === 'input')) {
      setLocalError('至少选一列作为请求输入。')
      return
    }
    const form = new FormData(event.currentTarget)
    form.set('file', file)
    for (const column of columns) {
      const group = groups[column]
      if (group) form.append(`${group}_keys`, column)
    }
    mutation.mutate(form)
  }

  return (
    <Dialog title="导入数据集" onClose={onClose} busy={mutation.isPending || reading}>
      <form className="dialog-body dialog-form" onSubmit={submit}>
        <p className="muted">支持 UTF-8 的 CSV 和 JSONL，最大 5 MiB，最多 10,000 条样本。</p>
        <label className="field">数据集名称<input name="name" maxLength={100} required autoFocus /></label>
        <label className="field">描述（选填）<textarea name="description" maxLength={2000} rows={2} /></label>
        <label className="field">文件格式<select name="format" value={format} onChange={(event) => {
          readVersion.current++; setReading(false); setFormat(event.target.value as 'csv' | 'jsonl'); setFile(null); setColumns([]); setGroups({}); setLocalError('')
        }}><option value="csv">CSV</option><option value="jsonl">JSONL</option></select></label>
        <label className="field">数据文件<input key={format} type="file" accept={format === 'csv' ? '.csv' : '.jsonl'} required onChange={(event) => void chooseFile(event.target.files?.[0] ?? null)} /></label>
        {format === 'csv' ? <p className="muted">将 CSV 列分配给请求输入、参考输出或元数据，至少选择一列作为请求输入。</p> : <p className="muted">每行一个 JSON 对象，input 必填，output 和 metadata 可省略。</p>}
        {reading ? <p role="status">正在读取表头…</p> : null}
        {columns.map((column) => (
          <label className="field" key={column}>{column}
            <select value={groups[column] ?? ''} onChange={(event) => { setGroups({ ...groups, [column]: event.target.value }); setLocalError('') }}>
              <option value="">忽略</option><option value="input">请求输入</option><option value="output">参考输出</option><option value="metadata">元数据</option>
            </select>
          </label>
        ))}
        {localError || mutation.error ? <p className="error-message" role="alert">{localError || errorMessage(mutation.error)}</p> : null}
        <div className="dialog-actions"><button className="button" type="button" onClick={onClose} disabled={mutation.isPending}>取消</button><button className="button button-primary" disabled={!file || reading || mutation.isPending}>{mutation.isPending ? '导入中…' : '导入'}</button></div>
      </form>
    </Dialog>
  )
}
