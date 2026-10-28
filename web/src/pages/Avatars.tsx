import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { del, get, mediaUrl, postForm, type Avatar, type AvatarOptions, type Page } from '../api'
import { Confirm, ErrorText, fmtTime, useToast } from '../components'

export default function Avatars() {
  const client = useQueryClient()
  const [toast, notify] = useToast()
  const [mode, setMode] = useState<'generate' | 'upload' | null>(null)
  const [name, setName] = useState('')
  const [prompt, setPrompt] = useState('professional news anchor seated at a modern newsroom desk, medium close-up, neutral expression, soft studio lighting, blurred newsroom background')
  const [negative, setNegative] = useState('')
  const [workflow, setWorkflow] = useState('')
  const [seed, setSeed] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [selected, setSelected] = useState<string | null>(null)

  const avatars = useQuery({ queryKey: ['avatars'], queryFn: () => get<Page<Avatar>>('/api/v1/avatars?limit=200') })
  const detail = useQuery({ queryKey: ['avatar', selected], queryFn: () => get<Avatar>(`/api/v1/avatars/${selected}`), enabled: Boolean(selected) })
  const options = useQuery({ queryKey: ['avatar-options'], queryFn: () => get<AvatarOptions>('/api/v1/avatars/options'), staleTime: 300_000 })
  const invalidate = () => void client.invalidateQueries({ queryKey: ['avatars'] })
  const seedMax = options.data?.workflows.find((w) => w.name === workflow)?.seed_max ?? '18446744073709551615'
  const randomSeed = () => {
    const max = BigInt(seedMax)
    const bytes = new Uint8Array(8); crypto.getRandomValues(bytes)
    let value = 0n; for (const b of bytes) value = (value << 8n) | BigInt(b)
    setSeed((value % (max + 1n)).toString())
  }
  // Every field starts with a real value: the default template, the system negative prompt, a random seed.
  useEffect(() => {
    if (mode !== 'generate' || !options.data) return
    if (!workflow) setWorkflow(options.data.default_workflow ?? '')
    if (!negative) setNegative(options.data.negative_prompt)
    if (!seed) randomSeed()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, options.data])

  const generate = useMutation({
    mutationFn: () => {
      const f = new FormData(); f.set('name', name); f.set('prompt', prompt); if (negative) f.set('negative_prompt', negative)
      if (workflow) f.set('workflow', workflow); if (seed) f.set('seed', seed)
      return postForm<Avatar>('/api/v1/avatars/generate', f)
    },
    onSuccess: (a) => { notify(`Generated ${a.name}`); setMode(null); invalidate() },
  })
  const upload = useMutation({
    mutationFn: () => { const f = new FormData(); f.set('name', name); f.set('image', file!); return postForm<Avatar>('/api/v1/avatars/upload', f) },
    onSuccess: (a) => { notify(`Uploaded ${a.name}`); setMode(null); invalidate() },
  })
  const remove = useMutation({ mutationFn: (n: string) => del(`/api/v1/avatars/${n}`), onSuccess: () => { notify('Deleted'); setSelected(null); invalidate() } })

  return (
    <div>
      {toast}
      <div className="row between">
        <h1>Avatars</h1>
        <div className="row">
          <button className="primary" onClick={() => setMode(mode === 'generate' ? null : 'generate')}>Generate</button>
          <button onClick={() => setMode(mode === 'upload' ? null : 'upload')}>Upload</button>
        </div>
      </div>
      <ErrorText error={avatars.error} />
      <ErrorText error={remove.error} />

      {mode === 'generate' && (
        <form className="card inline" onSubmit={(e) => { e.preventDefault(); generate.mutate() }}>
          <label>Name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="anchor-jane" required /></label>
          <label className="full">Prompt<textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} style={{ minHeight: 80 }} required /></label>
          <label className="full">Negative prompt<input value={negative} onChange={(e) => setNegative(e.target.value)} /></label>
          <label>Template<select value={workflow} onChange={(e) => setWorkflow(e.target.value)}>
            {options.data?.workflows.map((w) => <option key={w.name} value={w.name}>{w.name}</option>)}
          </select></label>
          <label>Seed<span className="row"><input value={seed} onChange={(e) => setSeed(e.target.value.replace(/[^0-9]/g, ''))} inputMode="numeric" pattern="[0-9]*" style={{ flex: 1 }} /><button type="button" onClick={randomSeed} title={`Random seed from 0 to ${seedMax}`}>Random</button></span></label>
          <div className="row full">
            <button className="primary" type="submit" disabled={generate.isPending || !name}>{generate.isPending ? 'Generating on ComfyUI…' : 'Generate'}</button>
            <button type="button" onClick={() => setMode(null)}>Cancel</button>
            <span className="muted small">Takes 10 to 60 seconds. Output is 16:9 at the configured avatar size.</span>
            <ErrorText error={generate.error} />
          </div>
        </form>
      )}
      {mode === 'upload' && (
        <form className="card inline" onSubmit={(e) => { e.preventDefault(); upload.mutate() }}>
          <label>Name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="anchor-jane" required /></label>
          <label>Image (PNG or JPEG, 16:9 recommended)<input type="file" accept="image/png,image/jpeg" onChange={(e) => setFile(e.target.files?.[0] ?? null)} required /></label>
          <div className="row full">
            <button className="primary" type="submit" disabled={upload.isPending || !name || !file}>Upload</button>
            <button type="button" onClick={() => setMode(null)}>Cancel</button>
            <ErrorText error={upload.error} />
          </div>
        </form>
      )}

      <div className="grid gallery">
        {avatars.data?.items.map((a) => (
          <div key={a.name} className="card" style={{ marginBottom: 0, cursor: 'pointer', outline: selected === a.name ? '2px solid var(--accent)' : 'none' }} onClick={() => setSelected(a.name)}>
            <img className="avatar" src={mediaUrl(a.image_url)} alt={a.name} loading="lazy" />
            <div className="row between" style={{ marginTop: 8 }}>
              <strong>{a.name}</strong>
              <span className="muted small">{a.width}×{a.height} · {a.source}</span>
            </div>
          </div>
        ))}
        {avatars.data?.items.length === 0 && <p className="muted">No avatars yet. Generate one or upload an image.</p>}
      </div>

      {detail.data && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="row between">
            <h2 style={{ margin: 0 }}>{detail.data.name}</h2>
            <div className="row">
              <a href={mediaUrl(detail.data.image_url)} target="_blank" rel="noreferrer">Open image</a>
              <Confirm label="Delete" onConfirm={() => remove.mutate(detail.data!.name)} />
            </div>
          </div>
          <dl className="kv" style={{ marginTop: 8 }}>
            <dt>Size</dt><dd>{detail.data.width}×{detail.data.height}</dd>
            <dt>Source</dt><dd>{detail.data.source}</dd>
            <dt>Created</dt><dd>{fmtTime(detail.data.created_at)}</dd>
            {detail.data.prompt && <><dt>Prompt</dt><dd>{detail.data.prompt}</dd></>}
            {detail.data.negative_prompt && <><dt>Negative</dt><dd>{detail.data.negative_prompt}</dd></>}
            {detail.data.seed != null && <><dt>Seed</dt><dd className="mono">{detail.data.seed}</dd></>}
            {detail.data.workflow && <><dt>Workflow</dt><dd className="mono small">{detail.data.workflow}</dd></>}
          </dl>
        </div>
      )}
    </div>
  )
}
