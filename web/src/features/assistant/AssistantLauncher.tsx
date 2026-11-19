import type { RefObject } from 'react'
import type { AssistantLayoutController } from './useAssistantLayout'

export function AssistantLauncher({ open, busy, layout, buttonRef, onToggle }: {
  open: boolean; busy: boolean; layout: AssistantLayoutController
  buttonRef: RefObject<HTMLButtonElement | null>; onToggle: () => void
}) {
  return <button ref={buttonRef} className="assistant-launcher" type="button" data-open={open}
    style={{ left: layout.position.x, top: layout.position.y }} {...layout.dragHandlers}
    aria-label={open ? '收起评测助手' : '打开评测助手'} aria-expanded={open} aria-controls="assistant-panel"
    title="评测助手 · 可拖动，方向键可移动"
    onClick={() => { if (layout.consumeClick()) onToggle() }}>
    <svg width="25" height="25" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M19 4H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h3l4 3v-3h7a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2Z" />
      <path d="M7 9h10M7 13h6" />
    </svg>
    {busy ? <span className="assistant-busy-dot" aria-label="助手正在回答" /> : null}
  </button>
}
