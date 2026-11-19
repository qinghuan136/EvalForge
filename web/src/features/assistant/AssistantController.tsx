import { useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AssistantLauncher } from './AssistantLauncher'
import { AssistantPanel } from './AssistantPanel'
import { useCurrentAssistantContext } from './AssistantPageContext'
import { useAssistantConversation } from './useAssistantConversation'
import { useAssistantLayer } from './useAssistantLayer'
import { useAssistantLayout } from './useAssistantLayout'
import './assistant.css'

/** This workspace child stays mounted when the dialog is hidden or routes change. */
export function AssistantController({ userId }: { userId: number }) {
  const [open, setOpen] = useState(false)
  const { pageContext, label } = useCurrentAssistantContext()
  const conversation = useAssistantConversation(userId, open, pageContext)
  const layout = useAssistantLayout(userId)
  const host = useAssistantLayer()
  const launcher = useRef<HTMLButtonElement>(null)
  function close() { setOpen(false); requestAnimationFrame(() => launcher.current?.focus({ preventScroll: true })) }
  return createPortal(<>
    <AssistantLauncher open={open} busy={conversation.busy} layout={layout} buttonRef={launcher}
      onToggle={() => { if (open) close(); else setOpen(true) }} />
    <AssistantPanel open={open} pageContext={pageContext} contextLabel={label} onClose={close} conversation={conversation} layout={layout} />
  </>, host)
}
