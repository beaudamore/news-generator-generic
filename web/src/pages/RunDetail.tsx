import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { get, mediaUrl, post, type Run } from '../api'
import { Confirm, ErrorText, Status, fmtBytes, fmtSeconds, fmtTime, useToast } from '../components'

const ACTIVE = new Set(['queued', 'preflight', 'running'])

export default function RunDetail() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const client = useQueryClient()
  const [toast, notify] = useToast()
  const run = useQuery({
    queryKey: ['run', id],
    queryFn: () => get<Run>(`/api/v1/runs/${id}`),
    refetchInterval: (query) => (query.state.data && ACTIVE.has(query.state.data.status) ? 5_000 : false),
  })
  const log = useQuery({
    queryKey: ['run-log', id, run.data?.status, run.data?.stage],
    queryFn: () => get<{ content: string }>(`/api/v1/runs/${id}/files/log/info?inline=true`).then((r) => r.content),
    enabled: Boolean(run.data?.artifacts.log),
  })
  const chunks = useQuery({
    queryKey: ['run-chunks', id],
    queryFn: () => get<{ content: string }>(`/api/v1/runs/${id}/files/chunks/info?inline=true`).then((r) => JSON.parse(r.content) as { chunks: { index: number; words: number; audio_seconds: number | null; video_seconds: number | null; status: string; hero_status?: string; hero_prompt?: string; hero_error?: string }[] }),
    enabled: Boolean(run.data?.artifacts.chunks),
  })
  const narration = useQuery({
    queryKey: ['run-narration', id],
    queryFn: () => get<{ content: string }>(`/api/v1/runs/${id}/files/narration/info?inline=true`).then((r) => r.content),
    enabled: Boolean(run.data?.artifacts.narration),
  })
  const cancel = useMutation({
    mutationFn: () => post<Run>(`/api/v1/runs/${id}/cancel`),
    onSuccess: () => { notify('Cancel requested'); void client.invalidateQueries({ queryKey: ['run', id] }) },
  })
  const retry = useMutation({
    mutationFn: () => post<Run>(`/api/v1/runs/${id}/retry`),
    onSuccess: (r) => { notify(`Queued retry ${r.id.slice(-8)}`); void client.invalidateQueries({ queryKey: ['runs'] }); navigate(`/runs/${r.id}`) },
  })

  if (run.error) return <ErrorText error={run.error} />
  if (!run.data) return <p className="muted">Loading…</p>
  const r = run.data
  const m = r.metrics ?? {}

  return (
    <div>
      {toast}
      <p className="small"><Link to="/runs">← Runs</Link></p>
      <div className="row between">
        <h1 className="mono" style={{ fontSize: 16 }}>{r.id}</h1>
        <div className="row">
          <Status value={r.status} />
          {ACTIVE.has(r.status) && <Confirm label="Cancel" onConfirm={() => cancel.mutate()} />}
          {(r.status === 'failed' || r.status === 'cancelled') && <button className="primary" onClick={() => retry.mutate()} disabled={retry.isPending}>Retry from {r.error_stage ?? r.stage ?? 'start'}</button>}
        </div>
      </div>
      <ErrorText error={cancel.error} />
      <ErrorText error={retry.error} />
      {r.error_message && <div className="card"><p className="error"><strong>{r.error_stage}</strong>: {r.error_message}</p></div>}

      <div className="grid two">
        <section className="card">
          <h2 style={{ marginTop: 0 }}>Summary</h2>
          <dl className="kv">
            <dt>Subject</dt><dd>{r.search_text}</dd>
            <dt>Stage</dt><dd>{r.stage ?? '—'}{r.cancel_requested ? ' (cancel requested)' : ''}</dd>
            <dt>Profile</dt><dd>{r.profile_id ? <Link to="/profiles">#{r.profile_id}</Link> : '—'}</dd>
            {r.retry_of && <><dt>Retry of</dt><dd><Link to={`/runs/${r.retry_of}`} className="mono">{r.retry_of.slice(-8)}</Link></dd></>}
            <dt>Queued</dt><dd>{fmtTime(r.queued_at)}</dd>
            <dt>Started</dt><dd>{fmtTime(r.started_at)}</dd>
            <dt>Finished</dt><dd>{fmtTime(r.finished_at)}</dd>
            <dt>Words</dt><dd>{m.word_count ?? '—'}{m.target_words ? ` / ${m.target_words} target` : ''}</dd>
            <dt>Audio</dt><dd>{fmtSeconds(m.audio_seconds)}</dd>
            <dt>Video</dt><dd>{fmtSeconds(m.video_seconds)}{m.video_width ? ` · ${m.video_width}×${m.video_height}` : ''}{m.chunks ? ` · ${m.chunks} chunk${m.chunks === 1 ? '' : 's'}` : ''}</dd>
            <dt>Voice</dt><dd>{String(r.settings?.voice ?? '—')}</dd>
            <dt>Avatar</dt><dd>{String(r.settings?.avatar ?? '—')}</dd>
          </dl>
        </section>

        <section className="card">
          <h2 style={{ marginTop: 0 }}>Video</h2>
          {r.artifacts.video ? (
            <>
              <video controls preload="metadata" src={mediaUrl(r.artifacts.video.url)} />
              <p className="small"><a href={mediaUrl(r.artifacts.video.url)}>Download</a> · {fmtBytes(r.artifacts.video.size)}</p>
            </>
          ) : <p className="muted">No video yet.</p>}
          {r.artifacts.audio && (
            <>
              <h2>Narration</h2>
              <audio controls preload="none" src={mediaUrl(r.artifacts.audio.url)} />
            </>
          )}
        </section>
      </div>

      {chunks.data && chunks.data.chunks.length > 0 && (
        <section className="card">
          <h2 style={{ marginTop: 0 }}>Segments</h2>
          <div className="row" style={{ alignItems: 'flex-start' }}>
            {chunks.data.chunks.map((c) => (
              <div key={c.index} style={{ width: 220 }}>
                {(c.hero_status === 'generated' || c.hero_status === 'card')
                  ? <img src={mediaUrl(`/api/v1/runs/${id}/chunks/${c.index}/hero/image`)} alt={`hero ${c.index}`} style={{ width: '100%', borderRadius: 6 }} title={c.hero_prompt} />
                  : <div className="muted small" style={{ height: 120, display: 'flex', alignItems: 'center', justifyContent: 'center', border: '1px dashed var(--border)', borderRadius: 6 }}>{c.hero_status === 'failed' ? `hero failed: ${c.hero_error ?? ''}` : 'no hero'}</div>}
                <div className="small">Segment {c.index} · {c.words} words{c.audio_seconds ? ` · ${fmtSeconds(c.audio_seconds)}` : ''} · {c.status}{c.hero_status === 'card' ? ' · headline card' : ''}</div>
              </div>
            ))}
          </div>
        </section>
      )}

      {narration.data && (
        <section className="card">
          <h2 style={{ marginTop: 0 }}>Script</h2>
          <p>{narration.data}</p>
        </section>
      )}

      {r.selected_sources && r.selected_sources.length > 0 && (
        <section className="card">
          <h2 style={{ marginTop: 0 }}>Sources</h2>
          <table>
            <thead><tr><th>Outlet</th><th>Title</th><th>Published</th><th>Score</th><th>Text</th></tr></thead>
            <tbody>
              {r.selected_sources.map((s) => (
                <tr key={s.url}>
                  <td>{s.outlet}</td>
                  <td><a href={s.url} target="_blank" rel="noreferrer">{s.title}</a></td>
                  <td className="muted small">{fmtTime(s.published_at)}</td>
                  <td className="mono">{s.source_rank_score?.toFixed(3) ?? '—'}</td>
                  <td className="small">{s.extraction ? `${s.extraction.status}${s.extraction.words ? ` · ${s.extraction.words} words` : ''}` : 'snippet'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {r.preflight && (
        <section className="card">
          <h2 style={{ marginTop: 0 }}>Preflight</h2>
          <table>
            <tbody>
              {r.preflight.checks.map((c) => (
                <tr key={c.service}><td className="mono">{c.service}</td><td><Status value={c.status} /></td><td className="muted small">{c.detail}</td></tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="card">
        <div className="row between">
          <h2 style={{ marginTop: 0 }}>Run log</h2>
          <span className="row small">
            {Object.entries(r.artifacts).filter(([, a]) => a).map(([name, a]) => (
              <a key={name} href={mediaUrl(a!.url)} target="_blank" rel="noreferrer">{name}</a>
            ))}
          </span>
        </div>
        {log.data ? <pre>{log.data}</pre> : <p className="muted">No log yet.</p>}
      </section>
    </div>
  )
}
