import { apiRequest, isSessionMissing, setCsrfToken } from '../../lib/api'

export type Session = {
  user: { id: number; username: string; display_name: string | null }
  session_expires_at: string
}

type Credentials = { username: string; password: string }
type Registration = Credentials & { display_name?: string }

export async function getSession(signal: AbortSignal): Promise<Session | null> {
  try {
    return await apiRequest<Session>('/auth/me', { signal })
  } catch (error) {
    if (!isSessionMissing(error)) throw error
    return null
  }
}

export function register(data: Registration) {
  return apiRequest<{ user: Session['user'] }>('/auth/register', { method: 'POST', json: data })
}

export async function login(data: Credentials): Promise<Session> {
  const { csrf_token, ...session } = await apiRequest<Session & { csrf_token: string }>(
    '/auth/login', { method: 'POST', json: data },
  )
  setCsrfToken(csrf_token)
  return session
}

export async function logout() {
  await apiRequest<void>('/auth/logout', { method: 'POST' })
  setCsrfToken()
}
