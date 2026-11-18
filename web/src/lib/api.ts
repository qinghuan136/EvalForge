export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export function errorMessage(error: unknown) {
  return error instanceof ApiError ? error.message : '操作失败，请重试。'
}

export function isSessionMissing(error: unknown) {
  return error instanceof ApiError && error.status === 401 && error.code === 'not_authenticated'
}

type ApiOptions = RequestInit & { json?: unknown }

let csrfToken: Promise<string> | undefined

export function setCsrfToken(token?: string) {
  csrfToken = token === undefined ? undefined : Promise.resolve(token)
}

async function fetchResponse(path: string, options: ApiOptions): Promise<Response> {
  const { json, ...init } = options
  const headers = new Headers(init.headers)
  if (!headers.has('Accept')) headers.set('Accept', 'application/json')
  if (json !== undefined) headers.set('Content-Type', 'application/json')

  let response: Response
  try {
    response = await fetch('/api/v1' + path, {
      ...init,
      headers,
      credentials: 'include',
      body: json === undefined ? init.body : JSON.stringify(json),
    })
  } catch (error) {
    if (init.signal?.aborted) throw error
    throw new ApiError(0, 'network_error', '无法连接服务，请稍后重试。')
  }

  if (!response.ok) {
    const data = await response.json().catch(() => undefined)
    const code = typeof data?.error?.code === 'string' ? data.error.code : 'http_error'
    const message = typeof data?.error?.message === 'string'
      ? data.error.message
      : `请求失败（HTTP ${response.status}），请稍后重试。`
    throw new ApiError(response.status, code, message)
  }
  return response
}

async function send<T>(path: string, options: ApiOptions): Promise<T> {
  const response = await fetchResponse(path, options)
  if (response.status === 204) return undefined as T
  const data = await response.json().catch(() => undefined)
  if (data === undefined) {
    throw new ApiError(response.status, 'invalid_response', '服务返回了无法读取的响应。')
  }
  return data as T
}

function getCsrfToken() {
  if (csrfToken) return csrfToken

  const pending = send<{ csrf_token: string }>('/auth/csrf', {})
    .then(({ csrf_token }) => csrf_token)
    .catch((error: unknown) => {
      if (csrfToken === pending) setCsrfToken()
      throw error
    })
  csrfToken = pending
  return pending
}

async function withCsrf<T>(path: string, options: ApiOptions, sendRequest: (path: string, options: ApiOptions) => Promise<T>): Promise<T> {
  const method = options.method?.toUpperCase() ?? 'GET'
  if (method === 'GET' || method === 'HEAD') return sendRequest(path, options)

  const headers = new Headers(options.headers)
  const token = getCsrfToken()
  headers.set('X-CSRF-Token', await token)
  try {
    return await sendRequest(path, { ...options, headers })
  } catch (error) {
    if (!(error instanceof ApiError) || error.code !== 'csrf_invalid') throw error
    // The server checks CSRF before running the write. Retry once with a fresh token.
    if (csrfToken === token) setCsrfToken()
    headers.set('X-CSRF-Token', await getCsrfToken())
    return sendRequest(path, { ...options, headers })
  }
}

export function apiRequest<T>(path: string, options: ApiOptions = {}): Promise<T> {
  return withCsrf(path, options, send<T>)
}

export function apiStream(path: string, options: ApiOptions): Promise<Response> {
  const headers = new Headers(options.headers)
  headers.set('Accept', 'text/event-stream')
  return withCsrf(path, { ...options, headers }, fetchResponse)
}
