import { createContext, useContext } from 'react'
import type { PageContext } from './pageContext'

export const OpenAssistantContext = createContext<((context?: PageContext) => void) | null>(null)

export function AskAssistantButton({ pageContext }: { pageContext?: PageContext }) {
  const open = useContext(OpenAssistantContext)
  return (
    <button className="button" type="button" onClick={() => open?.(pageContext)}>
      询问评测助手
    </button>
  )
}
