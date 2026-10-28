import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { del, get, mediaUrl, postForm, type Page, type Voice } from '../api'
import { Confirm, ErrorText, fmtTime, useToast } from '../components'

export default function Voices() {
  const client = useQueryClient()
  const [toast, notify] = useToast()
  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [language, setLanguage] = useState('en')
  const [file, setFile] = useState<File | null>(null)

  const voices = useQuery({ queryKey: ['voices'], queryFn: () => get<Page<Voice>>('/api/v1/voices?limit=200') })
  const library = useQuery({ queryKey: ['library-voices'], queryFn: () => get<{ voices: { name: string }[] }>('/voices') })
  const invalidate = () => { void client.invalidateQueries({ queryKey: ['voices'] }); void client.invalidateQueries({ queryKey: ['library-voices'] }) }

  const upload = useMutation({
    mutationFn: () => { const f = new FormData(); f.set('name', name); f.set('language', language); f.set('sample', file!); return postForm<Voice>('/api/v1/voices', f) },
    onSuccess: (v) => { notify(`Uploaded ${v.name}`); setShowForm(false); setName(''); setFile(null); invalidate() },
  })
  const remove = useMutation({ mutationFn: (n: string) => del(`/api/v1/voices/${n}`), onSuccess: () => { notify('Deleted'); invalidate() } })
  const tenantNames = new Set(voices.data?.items.map((v) => v.library_name ?? v.name) ?? [])
  const shared = library.data?.voices.filter((v) => !tenantNames.has(v.name)) ?? []

  return (
    <div>
      {toast}
      <div className="row between">
        <h1>Voices</h1>
        <button className="primary" onClick={() => setShowForm(!showForm)}>Upload voice</button>
      </div>
      <ErrorText error={voices.error} />
      <ErrorText error={remove.error} />

      {showForm && (
        <form className="card inline" onSubmit={(e) => { e.preventDefault(); upload.mutate() }}>
          <label>Name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="anchor-jane" required /></label>
          <label>Language<input value={language} onChange={(e) => setLanguage(e.target.value)} /></label>
          <label>Sample (WAV, about 5 to 15 seconds of clean speech)<input type="file" accept="audio/wav,.wav" onChange={(e) => setFile(e.target.files?.[0] ?? null)} required /></label>
          <div className="row full">
            <button className="primary" type="submit" disabled={upload.isPending || !name || !file}>{upload.isPending ? 'Uploading…' : 'Upload'}</button>
            <button type="button" onClick={() => setShowForm(false)}>Cancel</button>
            <ErrorText error={upload.error} />
          </div>
        </form>
      )}

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Tenant voices</h2>
        <table>
          <thead><tr><th>Name</th><th>Language</th><th>Sample</th><th>Created</th><th></th></tr></thead>
          <tbody>
            {voices.data?.items.map((v) => (
              <tr key={v.name}>
                <td>{v.name}<div className="muted small mono">{v.library_name}</div></td>
                <td>{v.language}</td>
                <td>{v.sample_url ? <audio controls preload="none" src={mediaUrl(v.sample_url)} style={{ height: 32, maxWidth: 260 }} /> : '—'}</td>
                <td className="muted small">{fmtTime(v.created_at)}</td>
                <td><Confirm label="Delete" onConfirm={() => remove.mutate(v.name)} /></td>
              </tr>
            ))}
            {voices.data?.items.length === 0 && <tr><td colSpan={5} className="muted">No tenant voices yet. Upload a sample, or use a shared library voice below by name.</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Shared TTS library</h2>
        <p className="muted small">Voices that exist on the TTS server outside this tenant. Any of these names can be used in a profile.</p>
        <ErrorText error={library.error} />
        <table>
          <thead><tr><th>Name</th><th>Sample</th></tr></thead>
          <tbody>
            {shared.map((v) => (
              <tr key={v.name}>
                <td>{v.name}</td>
                <td><audio controls preload="none" src={mediaUrl(`/api/v1/library-voices/${encodeURIComponent(v.name)}/sample`)} style={{ height: 32, maxWidth: 260 }} /></td>
              </tr>
            ))}
            {shared.length === 0 && <tr><td colSpan={2} className="muted">none</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}
