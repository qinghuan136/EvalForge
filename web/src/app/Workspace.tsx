import { useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router'
import { Brand } from '../components/ui/Brand'
import { errorMessage } from '../lib/api'
import { logout } from '../features/auth/api'
import type { Session } from '../features/auth/api'
import { AssistantController } from '../features/assistant/AssistantController'
import { AssistantPageContextProvider } from '../features/assistant/AssistantPageContext'
import { configListPath, runListPath } from '../features/experiments/routes'
import { updateSession } from './queryClient'

const navigation = [
  { to: configListPath, label: '实验配置' },
  { to: runListPath, label: '实验 Run' },
  { to: '/evaluators', label: '评分器' },
  { to: '/traces', label: '调用链' },
  { to: '/datasets', label: '数据集' },
  { to: '/runners', label: 'Runners' },
]

export function Workspace({ user }: { user: Session['user'] }) {
  const location = useLocation()
  const current = navigation.find(({ to }) => location.pathname === to || location.pathname.startsWith(to + '/'))
  const [leaving, setLeaving] = useState(false)
  const [logoutError, setLogoutError] = useState('')

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

  return <AssistantPageContextProvider>
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
    <AssistantController key={user.id} userId={user.id} />
  </AssistantPageContextProvider>
}
