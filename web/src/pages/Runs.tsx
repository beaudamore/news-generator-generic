import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { del, get, post, type Page, type Profile, type Run } from '../api'
import { Confirm, ErrorText, Status, fmtTime, useToast } from '../components'

const STATUSES = ['', 'queued', 'running', 'done', 'failed', 'cancelled']

export default function Runs() {
  const navigate = useNavigate()
  const client = useQueryClient()
  const [toast, notify] = useToast()
  const [status, setStatus] = useState('')
  const [cursor, setCursor] = useState<string | null>(null)
  const [profileId, setProfileId] = useState('')

  const runs = useQuery({
    queryKey: ['runs', status, cursor],
    queryFn: () => get<Page<Run>>(`/api/v1/runs?limit=25${status ? `&status=${status}` : ''}${cursor ? `&cursor=${cursor}` : ''}`),
    refetchInterval: 10_000,
  })
  const profiles = useQuery({ queryKey: ['profiles'], queryFn: () => get<Page<Profile>>('/api/v1/profiles?limit=200') })
  const enqueue = useMutation({
    mutationFn: (pid: string) => post<Run>('/api/v1/runs', { profile_id: pid }),
    onSuccess: (run) => {
      notify(`Queued run ${run.id.slice(-8)}`)
      void client.invalidateQueries({ queryKey: ['runs'] })
    },
  })
  const remove = useMutation({
    mutationFn: (id: string) => del(`/api/v1/runs/${id}`),
    onSuccess: () => { notify('Run deleted'); void client.invalidateQueries({ queryKey: ['runs'] }) },
  })

  return (
    <div>
      {toast}
      <div className="row between">
        <h1>Runs</h1>
        <form
          className="row"
          onSubmit={(e) => {
            e.preventDefault()
            if (profileId) enqueue.mutate(profileId)
          }}
        >
          <select value={profileId} onChange={(e) => setProfileId(e.target.value)}>
            <option value="">Choose a profile…</option>
            {profiles.data?.items.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <button className="primary" type="submit" disabled={!profileId || enqueue.isPending}>Run now</button>
        </form>
      </div>
      <ErrorText error={enqueue.error} />
      <div className="card">
        <div className="row">
          {STATUSES.map((s) => (
            <button key={s} className={status === s ? 'primary' : ''} onClick={() => { setStatus(s); setCursor(null) }}>{s || 'all'}</button>
          ))}
        </div>
      </div>
      <ErrorText error={runs.error} />
      <ErrorText error={remove.error} />
      <div className="card">
        <table>
          <thead><tr><th>Run</th><th>Status</th><th>Stage</th><th>Subject</th><th>Queued</th><th>Finished</th><th>Error</th><th></th></tr></thead>
          <tbody>
            {runs.data?.items.map((r) => (
              <tr key={r.id} className="clickable" onClick={() => navigate(`/runs/${r.id}`)}>
                <td><Link to={`/runs/${r.id}`} className="mono">{r.id.slice(-8)}</Link>{r.retry_of && <span className="pill">retry</span>}</td>
                <td><Status value={r.status} /></td>
                <td>{r.stage ?? '—'}</td>
                <td>{r.search_text}</td>
                <td className="muted small">{fmtTime(r.queued_at)}</td>
                <td className="muted small">{fmtTime(r.finished_at)}</td>
                <td className="small error">{r.error_message ? `${r.error_stage}: ${r.error_message.slice(0, 80)}` : ''}</td>
                <td onClick={(e) => e.stopPropagation()}>{['done', 'failed', 'cancelled'].includes(r.status) && <Confirm label="Delete" onConfirm={() => remove.mutate(r.id)} />}</td>
              </tr>
            ))}
            {runs.data?.items.length === 0 && <tr><td colSpan={8} className="muted">No runs.</td></tr>}
          </tbody>
        </table>
        <div className="row" style={{ marginTop: 10 }}>
          {cursor && <button onClick={() => setCursor(null)}>First page</button>}
          {runs.data?.next_cursor && <button onClick={() => setCursor(runs.data!.next_cursor)}>Next page</button>}
        </div>
      </div>
    </div>
  )
}
