import { useEffect, useState } from 'react'
import { DIALOG_LAYER_EVENT } from '../../components/ui/Dialog'

/** One stable portal container: moving it preserves drafts, cards and component state. */
export function useAssistantLayer() {
  const [host] = useState(() => {
    const element = document.createElement('div')
    element.className = 'assistant-layer'
    element.setAttribute('popover', 'manual')
    return element
  })
  useEffect(() => {
    function place() {
      // A popover outside showModal() remains inert. Put the host inside the active modal.
      const modal = [...document.querySelectorAll<HTMLDialogElement>('dialog[open]')].filter((dialog) => dialog.matches(':modal')).at(-1)
      const parent = modal ?? document.body
      if (host.parentElement === parent && host.matches(':popover-open')) return
      if (host.matches(':popover-open')) host.hidePopover()
      parent.append(host)
      host.showPopover()
    }
    document.addEventListener(DIALOG_LAYER_EVENT, place)
    place()
    return () => {
      document.removeEventListener(DIALOG_LAYER_EVENT, place)
      if (host.matches(':popover-open')) host.hidePopover()
      host.remove()
    }
  }, [host])
  return host
}
