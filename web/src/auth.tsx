import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { ApiError, get, getApiKey, setApiKey, type Tenant } from './api'

interface AuthState {
  tenant: Tenant | null
  loading: boolean
  error: string | null
  signIn: (key: string) => Promise<void>
  signOut: () => void
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [tenant, setTenant] = useState<Tenant | null>(null)
  const [loading, setLoading] = useState<boolean>(Boolean(getApiKey()))
  const [error, setError] = useState<string | null>(null)

  const verify = useCallback(async () => {
    try {
      const me = await get<{ tenant: Tenant }>('/api/v1/me')
      setTenant(me.tenant)
      setError(null)
    } catch (err) {
      setTenant(null)
      setError(err instanceof ApiError ? err.message : 'Could not reach the API.')
      if (err instanceof ApiError && err.status === 401) setApiKey('')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (getApiKey()) void verify()
  }, [verify])

  const signIn = useCallback(
    async (key: string) => {
      setApiKey(key.trim())
      setLoading(true)
      await verify()
    },
    [verify],
  )

  const signOut = useCallback(() => {
    setApiKey('')
    setTenant(null)
  }, [])

  return <AuthContext.Provider value={{ tenant, loading, error, signIn, signOut }}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth outside AuthProvider')
  return ctx
}

export function SignIn() {
  const { signIn, loading, error } = useAuth()
  const [key, setKey] = useState('')
  return (
    <div className="signin">
      <form
        className="card"
        onSubmit={(e) => {
          e.preventDefault()
          void signIn(key)
        }}
      >
        <h1>News Generator</h1>
        <p className="muted">Enter a tenant API key. It is checked against the API and kept in this browser only.</p>
        <label>
          API key
          <input type="password" value={key} onChange={(e) => setKey(e.target.value)} autoFocus placeholder="ng_..." />
        </label>
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={loading || !key.trim()}>
          {loading ? 'Checking…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
