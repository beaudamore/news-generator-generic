import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { del, get, post, put, type Page, type Profile, type Run, type Schedule } from '../api'
import { Confirm, ErrorText, fmtTime, useToast } from '../components'

const PRESETS: [string, string][] = [
  ['0 7 * * 1-5', 'Weekdays 07:00'],
  ['0 7 * * *', 'Daily 07:00'],
  ['0 7,17 * * *', 'Daily 07:00 and 17:00'],
  ['0 9 * * 1', 'Mondays 09:00'],
]

interface Draft { name: string; profile_id: string; cron: string; timezone: string; enabled: boolean }
const EMPTY: Draft = { name: '', profile_id: '', cron: '0 7 * * 1-5', timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC', enabled: true }

export default function Schedules() {
  const client = useQueryClient()
  const [toast, notify] = useToast()
  const [editing, setEditing] = useState<string | 'new' | null>(null)
  const [draft, setDraft] = useState<Draft>(EMPTY)

  const schedules = useQuery({ queryKey: ['schedules'], queryFn: () => get<Schedule[]>('/api/v1/schedules'), refetchInterval: 30_000 })
  const profiles = useQuery({ queryKey: ['profiles'], queryFn: () => get<Page<Profile>>('/api/v1/profiles?limit=200') })
  const profileName = (id: string) => profiles.data?.items.find((p) => p.id === id)?.name ?? `#${id}`
  const invalidate = () => void client.invalidateQueries({ queryKey: ['schedules'] })

  const save = useMutation({
    mutationFn: () => (editing === 'new' ? post<Schedule>('/api/v1/schedules', draft) : put<Schedule>(`/api/v1/schedules/${editing}`, draft)),
    onSuccess: () => { notify('Saved'); setEditing(null); invalidate() },
  })
  const toggle = useMutation({ mutationFn: (s: Schedule) => put<Schedule>(`/api/v1/schedules/${s.id}`, { enabled: !s.enabled }), onSuccess: invalidate })
  const remove = useMutation({ mutationFn: (id: string) => del(`/api/v1/schedules/${id}`), onSuccess: () => { notify('Deleted'); invalidate() } })
  const runNow = useMutation({ mutationFn: (id: string) => post<Run>(`/api/v1/schedules/${id}/run`), onSuccess: (run) => { notify(`Queued run ${run.id.slice(-8)}`); invalidate() } })

  return (
    <div>
      {toast}
      <div className="row between">
        <h1>Schedules</h1>
        <button className="primary" onClick={() => { setEditing('new'); setDraft(EMPTY) }}>New schedule</button>
      </div>
      <ErrorText error={schedules.error} />
      <ErrorText error={remove.error} />
      <ErrorText error={runNow.error} />
      <ErrorText error={toggle.error} />

      {editing && (
        <form className="card inline" onSubmit={(e) => { e.preventDefault(); save.mutate() }}>
          <label>Name<input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></label>
          <label>
            Profile
            <select value={draft.profile_id} onChange={(e) => setDraft({ ...draft, profile_id: e.target.value })} required>
              <option value="">Choose…</option>
              {profiles.data?.items.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
          <label>
            Cron (minute hour day month weekday)
            <input value={draft.cron} onChange={(e) => setDraft({ ...draft, cron: e.target.value })} className="mono" required />
          </label>
          <label>
            Preset
            <select value="" onChange={(e) => e.target.value && setDraft({ ...draft, cron: e.target.value })}>
              <option value="">Pick a preset…</option>
              {PRESETS.map(([cron, label]) => <option key={cron} value={cron}>{label} · {cron}</option>)}
            </select>
          </label>
          <label>Timezone<input value={draft.timezone} onChange={(e) => setDraft({ ...draft, timezone: e.target.value })} /></label>
          <label className="row" style={{ flexDirection: 'row', alignItems: 'center' }}>
            <input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} /> Enabled
          </label>
          <div className="row full">
            <button className="primary" type="submit" disabled={save.isPending || !draft.profile_id}>{editing === 'new' ? 'Create' : 'Save'}</button>
            <button type="button" onClick={() => setEditing(null)}>Cancel</button>
            <ErrorText error={save.error} />
          </div>
        </form>
      )}

      <div className="card">
        <table>
          <thead><tr><th>Name</th><th>Profile</th><th>Cron</th><th>Timezone</th><th>Enabled</th><th>Next</th><th>Last run</th><th>Last error</th><th></th></tr></thead>
          <tbody>
            {schedules.data?.map((s) => (
              <tr key={s.id}>
                <td>{s.name ?? <span className="muted">—</span>}</td>
                <td>{profileName(s.profile_id)}</td>
                <td className="mono">{s.cron}</td>
                <td>{s.timezone}</td>
                <td><button className="link" onClick={() => toggle.mutate(s)}>{s.enabled ? 'on' : 'off'}</button></td>
                <td className="small">{fmtTime(s.next_run_at)}</td>
                <td className="small">{s.last_run_id ? <Link to={`/runs/${s.last_run_id}`} className="mono">{s.last_run_id.slice(-8)}</Link> : '—'}<div className="muted">{fmtTime(s.last_run_at)}</div></td>
                <td className="small error">{s.last_error ?? ''}</td>
                <td className="row">
                  <button onClick={() => runNow.mutate(s.id)} disabled={runNow.isPending}>Run now</button>
                  <button onClick={() => { setEditing(s.id); setDraft({ name: s.name ?? '', profile_id: s.profile_id, cron: s.cron, timezone: s.timezone, enabled: s.enabled }) }}>Edit</button>
                  <Confirm label="Delete" onConfirm={() => remove.mutate(s.id)} />
                </td>
              </tr>
            ))}
            {schedules.data?.length === 0 && <tr><td colSpan={9} className="muted">No schedules yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}
