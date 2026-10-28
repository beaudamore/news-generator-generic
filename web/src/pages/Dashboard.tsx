import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get, type Page, type Preflight, type Run, type SystemInfo } from '../api'
import { ErrorText, Status, fmtBytes, fmtTime } from '../components'

export default function Dashboard() {
  const client = useQueryClient()
  const health = useQuery({
    queryKey: ['health-deep'],
    // /health/deep answers 200 or 503 with the same body, so read it directly instead of through api().
    queryFn: async () => {
      const response = await fetch('/health/deep')
      if (response.status !== 200 && response.status !== 503) throw new Error(`${response.status} ${response.statusText}`)
      return (await response.json()) as Preflight
    },
    refetchInterval: 60_000,
  })
  const system = useQuery({ queryKey: ['system'], queryFn: () => get<SystemInfo>('/api/v1/system') })
  const recent = useQuery({ queryKey: ['runs', 'recent'], queryFn: () => get<Page<Run>>('/api/v1/runs?limit=8'), refetchInterval: 10_000 })

  return (
    <div>
      <div className="row between">
        <h1>Dashboard</h1>
        <button onClick={() => void client.invalidateQueries({ queryKey: ['health-deep'] })}>Re-check services</button>
      </div>

      <div className="grid two">
        <section className="card">
          <div className="row between">
            <h2 style={{ margin: 0 }}>Services</h2>
            {health.data && <Status value={health.data.ok ? 'ok' : 'fail'} />}
          </div>
          <ErrorText error={health.error} />
          {health.data && (
            <table>
              <tbody>
                {health.data.checks.map((c) => (
                  <tr key={c.service}>
                    <td className="mono">{c.service}</td>
                    <td><Status value={c.status} /></td>
                    <td className="muted small">{c.detail}</td>
                    <td className="muted small">{c.latency_ms} ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {health.data && <p className="muted small">Checked {fmtTime(health.data.checked_at)}{health.data.cached ? ' (cached)' : ''}</p>}
        </section>

        <section className="card">
          <h2 style={{ marginTop: 0 }}>System</h2>
          <ErrorText error={system.error} />
          {system.data && (
            <dl className="kv">
              <dt>Version</dt><dd>{system.data.version}</dd>
              <dt>Tenant</dt><dd>{system.data.tenant}</dd>
              <dt>LLM</dt><dd>{system.data.llm_model ?? '—'}</dd>
              <dt>Video</dt><dd>{system.data.video.width}×{system.data.video.height} @ {system.data.video.fps} fps · {system.data.video.workflow ?? '—'}</dd>
              <dt>Prompt enhance</dt><dd>{system.data.video.prompt_enhance ? 'on' : 'off'}</dd>
              <dt>Free disk</dt><dd>{fmtBytes(system.data.storage.free_bytes)}</dd>
              <dt>Seq</dt><dd>{system.data.logging.seq.enabled ? `on · queued ${system.data.logging.seq.queued} · failures ${system.data.logging.seq.failures}` : 'off'}</dd>
              {system.data.services.some((s) => s.url !== undefined) && (
                <>
                  <dt>Endpoints</dt>
                  <dd>{system.data.services.map((s) => <div key={s.name} className="mono small">{s.name}: {s.url ?? <span className="muted">not set</span>}</div>)}</dd>
                </>
              )}
            </dl>
          )}
        </section>
      </div>

      <section className="card">
        <div className="row between">
          <h2 style={{ margin: 0 }}>Recent runs</h2>
          <Link to="/runs">All runs</Link>
        </div>
        <ErrorText error={recent.error} />
        {recent.data && (
          <table>
            <thead><tr><th>Run</th><th>Status</th><th>Stage</th><th>Subject</th><th>Queued</th></tr></thead>
            <tbody>
              {recent.data.items.map((r) => (
                <tr key={r.id}>
                  <td><Link to={`/runs/${r.id}`} className="mono">{r.id.slice(-8)}</Link></td>
                  <td><Status value={r.status} /></td>
                  <td>{r.stage ?? '—'}</td>
                  <td>{r.search_text}</td>
                  <td className="muted small">{fmtTime(r.queued_at)}</td>
                </tr>
              ))}
              {recent.data.items.length === 0 && <tr><td colSpan={5} className="muted">No runs yet.</td></tr>}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}
