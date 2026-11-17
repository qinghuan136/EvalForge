import { useCallback, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router'
import { Brand } from '../components/ui/Brand'
import { errorMessage } from '../lib/api'
import { logout } from '../features/auth/api'
import type { Session } from '../features/auth/api'
import { AssistantDialog } from '../features/assistant/AssistantDialog'
import { OpenAssistantContext } from '../features/assistant/AskAssistantButton'
import { derivePageContext } from '../features/assistant/pageContext'
import type { PageContext } from '../features/assistant/pageContext'
import { updateSession } from './queryClient'

const navigation = [
  { to: '/experiments', label: '实验' },
  { to: '/traces', label: '调用链' },
  { to: '/datasets', label: '数据集' },
  { to: '/runners', label: 'Runners' },
]

export function Workspace({ user }: { user: Session['user'] }) {
  const location = useLocation()
  const current = navigation.find(({ to }) => location.pathname === to || location.pathname.startsWith(to + '/'))
  const [leaving, setLeaving] = useState(false)
  const [logoutError, setLogoutError] = useState('')
  const [assistant, setAssistant] = useState<{ locationKey: string; context?: PageContext } | null>(null)
  const openAssistant = useCallback((context?: PageContext) => {
    setAssistant({ locationKey: location.key, context })
  }, [location.key])
  const detailContext = assistant?.locationKey === location.key ? assistant.context : undefined
  const pageContext = detailContext ?? derivePageContext(location.pathname)

  async function signOut() {
    setLeaving(true)
    setLogoutError('')
    try {
      await logout()
      updateSession(null)
    } catch (error) {
      setLogoutError(errorMessage(error))
    } finally {
      setLeaving(false)
    }
  }

  return <OpenAssistantContext.Provider value={openAssistant}>
    <div className="workspace">
      <aside className="sidebar">
        <Brand />
        <nav className="navigation" aria-label="主导航">
          {navigation.map(({ to, label }) => <NavLink key={to} to={to}>{label}</NavLink>)}
        </nav>
      </aside>
      <main className="workspace-main">
        <div className="topbar">
          <div className="breadcrumb">
            <span>工作台</span><span aria-hidden="true">/</span><span>{current?.label}</span>
          </div>
          <div className="account">
            <span title={user.username}>{user.display_name || user.username}</span>
            <button className="button button-quiet" type="button" onClick={() => openAssistant()}>评测助手</button>
            <button className="button button-quiet" type="button" disabled={leaving} onClick={() => void signOut()}>
              {leaving ? '退出中…' : '退出登录'}
            </button>
          </div>
        </div>
        <div className="workspace-content">
          {logoutError ? <p className="logout-error error-message" role="alert">{logoutError}</p> : null}
          <Outlet />
        </div>
      </main>
    </div>
    <AssistantDialog key={user.id} userId={user.id} open={assistant !== null}
      pageContext={pageContext} onClose={() => setAssistant(null)} />
  </OpenAssistantContext.Provider>
}
