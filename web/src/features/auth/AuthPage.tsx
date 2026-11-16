import { useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { updateSession } from '../../app/queryClient'
import { Brand } from '../../components/ui/Brand'
import { errorMessage } from '../../lib/api'
import { login, register } from './api'

type AuthLocation = { returnTo?: string; registeredUsername?: string }

export function AuthPage({ mode }: { mode: 'login' | 'register' }) {
  const registering = mode === 'register'
  const navigate = useNavigate()
  const location = useLocation()
  const state = location.state as AuthLocation | null
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (pending) return
    const form = new FormData(event.currentTarget)
    const username = String(form.get('username')).trim().toLowerCase()
    const password = String(form.get('password'))
    const displayName = String(form.get('display_name') ?? '').trim()
    const passwordLength = Array.from(password).length

    if (!/^[a-z0-9][a-z0-9_.-]{2,63}$/.test(username)) {
      setError('用户名须为 3–64 个字符，以字母或数字开头，可包含下划线、点和连字符。')
      return
    }
    const minPasswordLength = registering ? 15 : 1
    if (passwordLength < minPasswordLength || passwordLength > 128) {
      setError(`密码长度须为 ${minPasswordLength}–128 个字符。`)
      return
    }
    if (Array.from(displayName).length > 100) {
      setError('显示名称最多 100 个字符。')
      return
    }

    setError('')
    setPending(true)
    try {
      if (registering) {
        await register({ username, password, display_name: displayName || undefined })
        navigate('/login', {
          replace: true,
          state: { returnTo: state?.returnTo, registeredUsername: username },
        })
      } else {
        updateSession(await login({ username, password }))
      }
    } catch (error) {
      setError(errorMessage(error))
    } finally {
      setPending(false)
    }
  }

  return (
    <main className="auth-layout">
      <section className="auth-card" aria-labelledby="auth-title">
        <Brand />
        <h1 id="auth-title">{registering ? '注册账号' : '登录'}</h1>
        {!registering && state?.registeredUsername
          ? <p className="auth-notice" role="status">账号已注册，请登录。</p>
          : null}
        <form onSubmit={submit} className="auth-form">
          <fieldset disabled={pending}>
            <label className="field">
              用户名
              <input name="username" autoComplete="username" autoCapitalize="none" spellCheck={false}
                defaultValue={registering ? '' : state?.registeredUsername} required />
            </label>
            {registering ? (
              <label className="field">
                <span>显示名称 <span className="muted">（选填）</span></span>
                <input name="display_name" autoComplete="nickname" />
              </label>
            ) : null}
            <label className="field">
              密码
              <input name="password" type="password" autoComplete={registering ? 'new-password' : 'current-password'}
                aria-describedby={registering ? 'password-hint' : undefined} required />
              {registering ? <small id="password-hint">15–128 个字符</small> : null}
            </label>
            {error ? <p className="error-message" role="alert">{error}</p> : null}
            <button className="button button-primary" type="submit">
              {registering ? (pending ? '注册中…' : '注册') : (pending ? '登录中…' : '登录')}
            </button>
          </fieldset>
        </form>
        <p className="auth-switch">
          {registering ? '已有账号？' : '还没有账号？'}
          {pending ? <span>{registering ? '去登录' : '注册账号'}</span> : (
            <Link to={registering ? '/login' : '/register'} state={{ returnTo: state?.returnTo }}>
              {registering ? '去登录' : '注册账号'}
            </Link>
          )}
        </p>
      </section>
    </main>
  )
}
