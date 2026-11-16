import { useEffect, useRef } from 'react'
import type { ReactNode } from 'react'

export function Dialog({ title, children, onClose, busy = false }: {
  title: string
  children: ReactNode
  onClose: () => void
  busy?: boolean
}) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current
    dialog?.showModal()
    return () => { if (dialog?.open) dialog.close() }
  }, [])
  return (
    <dialog ref={ref} className="dialog" aria-labelledby="dialog-title"
      onCancel={(event) => { event.preventDefault(); if (!busy) onClose() }}>
      <header className="dialog-header">
        <h2 id="dialog-title">{title}</h2>
        <button className="button button-quiet" type="button" onClick={onClose} disabled={busy} aria-label="关闭">关闭</button>
      </header>
      {children}
    </dialog>
  )
}
