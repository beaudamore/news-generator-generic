import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { SignIn, useAuth } from './auth'
import Dashboard from './pages/Dashboard'
import Runs from './pages/Runs'
import RunDetail from './pages/RunDetail'
import Profiles from './pages/Profiles'
import Schedules from './pages/Schedules'
import Prompts from './pages/Prompts'
import Avatars from './pages/Avatars'
import Voices from './pages/Voices'
import Tenants from './pages/Tenants'

const NAV = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/runs', label: 'Runs' },
  { to: '/profiles', label: 'Profiles' },
  { to: '/schedules', label: 'Schedules' },
  { to: '/avatars', label: 'Avatars' },
  { to: '/voices', label: 'Voices' },
]

export default function App() {
  const { tenant, loading, signOut } = useAuth()
  if (loading) return <div className="signin"><p className="muted">Checking key…</p></div>
  if (!tenant) return <SignIn />
  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">News Generator</div>
        <nav>
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => (isActive ? 'active' : '')}>
              {item.label}
            </NavLink>
          ))}
          {tenant.is_admin && (
            <NavLink to="/prompts" className={({ isActive }) => (isActive ? 'active' : '')}>
              Prompts
            </NavLink>
          )}
          {tenant.is_admin && (
            <NavLink to="/tenants" className={({ isActive }) => (isActive ? 'active' : '')}>
              Tenants
            </NavLink>
          )}
        </nav>
        <div className="sidebar-footer">
          <div className="muted small">
            {tenant.name} <span className="pill">{tenant.slug}</span>
            {tenant.is_admin && <span className="pill">administrator</span>}
          </div>
          <button className="link" onClick={signOut}>
            Sign out
          </button>
        </div>
      </aside>
      <main className="content">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runs/:id" element={<RunDetail />} />
          <Route path="/profiles" element={<Profiles />} />
          <Route path="/schedules" element={<Schedules />} />
          <Route path="/prompts" element={tenant.is_admin ? <Prompts /> : <Navigate to="/" replace />} />
          <Route path="/avatars" element={<Avatars />} />
          <Route path="/voices" element={<Voices />} />
          <Route path="/tenants" element={tenant.is_admin ? <Tenants /> : <Navigate to="/" replace />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}
