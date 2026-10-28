import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { del, get, post, put, type ApiKeyInfo, type Tenant } from '../api'
import { Confirm, ErrorText, fmtTime, useToast } from '../components'

export default function Tenants() {
  const client = useQueryClient()
  const [toast, notify] = useToast()
  const [slug, setSlug] = useState('')
  const [name, setName] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [label, setLabel] = useState('')
  const [revealed, setRevealed] = useState<{ tenant: string; key: string } | null>(null)

  const tenants = useQuery({ queryKey: ['tenants'], queryFn: () => get<Tenant[]>('/api/v1/tenants') })
  const keys = useQuery({ queryKey: ['tenant-keys', selected], queryFn: () => get<ApiKeyInfo[]>(`/api/v1/tenants/${selected}/keys`), enabled: Boolean(selected) })
  const invalidate = () => { void client.invalidateQueries({ queryKey: ['tenants'] }); void client.invalidateQueries({ queryKey: ['tenant-keys'] }) }

  const create = useMutation({
    mutationFn: () => post<{ tenant: Tenant; api_key: string }>('/api/v1/tenants', { slug, name }),
    onSuccess: (r) => { setRevealed({ tenant: r.tenant.slug, key: r.api_key }); setSlug(''); setName(''); setSelected(r.tenant.slug); invalidate() },
  })
  const newKey = useMutation({
    mutationFn: () => post<ApiKeyInfo>(`/api/v1/tenants/${selected}/keys`, { label }),
    onSuccess: (k) => { setRevealed({ tenant: selected!, key: k.api_key! }); setLabel(''); invalidate() },
  })
  const revoke = useMutation({ mutationFn: (id: string) => del(`/api/v1/tenants/${selected}/keys/${id}`), onSuccess: () => { notify('Revoked'); invalidate() } })
  const setActive = useMutation({ mutationFn: (t: Tenant) => put<Tenant>(`/api/v1/tenants/${t.slug}/active`, { active: !t.active }), onSuccess: invalidate })

  return (
    <div>
      {toast}
      <h1>Tenants</h1>
      <ErrorText error={tenants.error} />
      <ErrorText error={setActive.error} />

      {revealed && (
        <div className="card" style={{ borderColor: 'var(--warn)' }}>
          <strong>New API key for {revealed.tenant}</strong>
          <p className="muted small">Shown once. Copy it now; it is stored hashed and cannot be retrieved later.</p>
          <pre style={{ maxHeight: 'none' }}>{revealed.key}</pre>
          <button onClick={() => setRevealed(null)}>I have copied it</button>
        </div>
      )}

      <form className="card inline" onSubmit={(e) => { e.preventDefault(); create.mutate() }}>
        <label>Slug<input value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="acme" required /></label>
        <label>Name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="Acme Corp" required /></label>
        <div className="row"><button className="primary" type="submit" disabled={create.isPending || !slug || !name}>Create tenant</button><ErrorText error={create.error} /></div>
      </form>

      <div className="card">
        <table>
          <thead><tr><th>Slug</th><th>Name</th><th>Active</th><th>Admin</th><th>Created</th><th></th></tr></thead>
          <tbody>
            {tenants.data?.map((t) => (
              <tr key={t.slug} className="clickable" onClick={() => setSelected(t.slug)}>
                <td className="mono">{t.slug}</td>
                <td>{t.name}</td>
                <td>{t.active ? 'yes' : <span className="error">no</span>}</td>
                <td>{t.is_admin ? 'yes' : ''}</td>
                <td className="muted small">{fmtTime(t.created_at)}</td>
                <td>{!t.is_admin && <button onClick={(e) => { e.stopPropagation(); setActive.mutate(t) }}>{t.active ? 'Deactivate' : 'Activate'}</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selected && (
        <div className="card">
          <div className="row between">
            <h2 style={{ margin: 0 }}>API keys for {selected}</h2>
            <form className="row" onSubmit={(e) => { e.preventDefault(); newKey.mutate() }}>
              <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="label (optional)" />
              <button className="primary" type="submit" disabled={newKey.isPending}>Issue key</button>
            </form>
          </div>
          <ErrorText error={keys.error} />
          <ErrorText error={newKey.error} />
          <ErrorText error={revoke.error} />
          <table>
            <thead><tr><th>Id</th><th>Prefix</th><th>Label</th><th>Created</th><th>Revoked</th><th></th></tr></thead>
            <tbody>
              {keys.data?.map((k) => (
                <tr key={k.id}>
                  <td>{k.id}</td>
                  <td className="mono">{k.prefix}…</td>
                  <td>{k.label}</td>
                  <td className="muted small">{fmtTime(k.created_at)}</td>
                  <td className="muted small">{k.revoked_at ? fmtTime(k.revoked_at) : ''}</td>
                  <td>{!k.revoked_at && <Confirm label="Revoke" onConfirm={() => revoke.mutate(k.id)} />}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
