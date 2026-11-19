import { createContext, useCallback, useContext, useEffect, useId, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { useLocation } from 'react-router'
import { derivePageContext } from './pageContext'
import type { PageContext } from './pageContext'

type Entry = { locationKey: string; context?: PageContext; label?: string; priority: 'page' | 'detail' }
type CurrentContext = { pageContext?: PageContext; label?: string }
const Registry = createContext<((id: string, entry?: Entry) => void) | null>(null)
const Current = createContext<CurrentContext>({})

export function AssistantPageContextProvider({ children }: { children: ReactNode }) {
  const location = useLocation()
  const [entries, setEntries] = useState(new Map<string, Entry>())
  const update = useCallback((id: string, entry?: Entry) => {
    setEntries((previous) => {
      const next = new Map(previous)
      if (entry) next.set(id, entry) // Updating a label must not change the registration order.
      else next.delete(id)
      return next
    })
  }, [])
  const current = useMemo(() => {
    let selected: Entry | undefined
    for (const entry of entries.values()) {
      if (entry.locationKey !== location.key) continue
      if (!selected || entry.priority === 'detail' || selected.priority === 'page') selected = entry
    }
    return selected ? { pageContext: selected.context, label: selected.label }
      : { pageContext: derivePageContext(location.pathname) }
  }, [entries, location.key, location.pathname])
  return <Registry.Provider value={update}><Current.Provider value={current}>{children}</Current.Provider></Registry.Provider>
}

/** Detail components register while mounted; closing them restores the page underneath. */
export function useAssistantPageContext(context: PageContext | undefined, label?: string, priority: Entry['priority'] = 'detail') {
  const update = useContext(Registry)
  const location = useLocation()
  const id = useId()
  const serialized = JSON.stringify(context ?? null)
  useEffect(() => {
    update?.(id, { locationKey: location.key, context: JSON.parse(serialized) ?? undefined, label, priority })
  }, [update, id, location.key, serialized, label, priority])
  useEffect(() => () => update?.(id), [update, id])
}

export function useCurrentAssistantContext() { return useContext(Current) }
