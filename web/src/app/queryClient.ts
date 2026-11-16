import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query'
import { ApiError, isSessionMissing, setCsrfToken } from '../lib/api'
import type { Session } from '../features/auth/api'

export const sessionKey = ['session'] as const
export const privateKey = ['private'] as const

export const queryClient = new QueryClient({
  mutationCache: new MutationCache({
    onError: (error) => {
      if (isSessionMissing(error)) {
        setCsrfToken()
        updateSession(null)
      }
    },
  }),
  defaultOptions: {
    queries: {
      retry: (count, error) =>
        count < 1 && error instanceof ApiError && (error.status === 0 || error.status >= 500),
    },
  },
  queryCache: new QueryCache({
    onSuccess: (data, query) => {
      if (query.queryKey[0] === sessionKey[0] && data === null) {
        setCsrfToken()
        clearPrivateQueries()
      }
    },
    onError: (error) => {
      if (isSessionMissing(error)) {
        setCsrfToken()
        updateSession(null)
      }
    },
  }),
})

function clearPrivateQueries() {
  queryClient.removeQueries({ queryKey: privateKey })
}

export function updateSession(session: Session | null) {
  void queryClient.cancelQueries({ queryKey: sessionKey, exact: true })
  clearPrivateQueries()
  queryClient.setQueryData(sessionKey, session)
}
