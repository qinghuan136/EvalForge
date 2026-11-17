import { useQuery } from '@tanstack/react-query'
import { Navigate, Route, Routes, useLocation } from 'react-router'
import { AuthPage } from '../features/auth/AuthPage'
import { getSession } from '../features/auth/api'
import { RunnersPage } from '../features/runners/RunnersPage'
import { DatasetsPage } from '../features/datasets/DatasetsPage'
import { ExperimentsPage } from '../features/experiments/ExperimentsPage'
import { ExperimentDetail } from '../features/experiments/ExperimentDetail'
import { TracesPage } from '../features/traces/TracesPage'
import { TraceDetailPage } from '../features/traces/TraceDetailPage'
import { errorMessage } from '../lib/api'
import { Workspace } from './Workspace'
import { sessionKey } from './queryClient'

export function App() {
  const location = useLocation()
  const session = useQuery({
    queryKey: sessionKey,
    queryFn: ({ signal }) => getSession(signal),
  })

  if (session.isPending) {
    return <main className="screen-message" role="status">正在检查登录状态…</main>
  }
  if (session.data === undefined) {
    return (
      <main className="screen-message">
        <p className="error-message" role="alert">{errorMessage(session.error)}</p>
        <button className="button" disabled={session.isFetching} onClick={() => void session.refetch()}>
          {session.isFetching ? '重试中…' : '重试'}
        </button>
      </main>
    )
  }

  if (session.data === null) {
    return (
      <Routes>
        <Route path="/login" element={<AuthPage key="login" mode="login" />} />
        <Route path="/register" element={<AuthPage key="register" mode="register" />} />
        <Route path="*" element={<Navigate replace to="/login" state={{
          returnTo: location.pathname + location.search + location.hash,
        }} />} />
      </Routes>
    )
  }

  const user = session.data.user
  const returnTo = (location.state as { returnTo?: string } | null)?.returnTo
  const destination = returnTo?.startsWith('/') && !/^\/(?:[/\\]|(?:login|register)(?:[/?#]|$))/i.test(returnTo)
    ? returnTo
    : '/runners'

  return (
    <Routes>
      <Route path="/login" element={<Navigate replace to={destination} />} />
      <Route path="/register" element={<Navigate replace to={destination} />} />
      <Route element={<Workspace user={user} />}>
        <Route path="/runners" element={<RunnersPage userId={user.id} />} />
        <Route path="/experiments" element={<ExperimentsPage userId={user.id} />} />
        <Route path="/experiments/:experimentId" element={<ExperimentDetail userId={user.id} />} />
        <Route path="/traces" element={<TracesPage userId={user.id} />} />
        <Route path="/traces/:traceId" element={<TraceDetailPage userId={user.id} />} />
        <Route path="/datasets" element={<DatasetsPage userId={user.id} />} />
      </Route>
      <Route path="*" element={<Navigate replace to="/runners" />} />
    </Routes>
  )
}
