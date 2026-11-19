import { useEffect, useId, useRef } from 'react'
import type { ReactNode } from 'react'

export const DIALOG_LAYER_EVENT = 'evalforge:dialog-layer'

export function Dialog({ title, children, onClose, busy = false }: {
  title: string
  children: ReactNode
  onClose: () => void
  busy?: boolean
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  useEffect(() => {
    const dialog = ref.current
    dialog?.showModal()
    document.dispatchEvent(new Event(DIALOG_LAYER_EVENT))
    return () => {
      if (dialog?.open) dialog.close()
      document.dispatchEvent(new Event(DIALOG_LAYER_EVENT))
    }
  }, [])
  return (
    <dialog ref={ref} className="dialog" aria-labelledby={titleId}
      onCancel={(event) => { event.preventDefault(); if (!busy) onClose() }}>
      <header className="dialog-header">
        <h2 id={titleId}>{title}</h2>
        <button className="button button-quiet" type="button" onClick={onClose} disabled={busy} aria-label="关闭">关闭</button>
      </header>
      {children}
    </dialog>
  )
}
