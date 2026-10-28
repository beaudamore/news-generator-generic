import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { get, postForm, putForm, type Prompt } from '../api'
import { ErrorText, fmtTime, useToast } from '../components'

const LABELS: Record<string, string> = {
  summarize: 'Summarize (script writer)',
  video_visual: 'Video visual prompt',
  video_sound: 'Video sound prompt',
}

interface TestResult { run_id: string; script: string; word_count: number; target_words: number | null; sources_used: { title: string; source: string }[] }

export default function Prompts() {
  const client = useQueryClient()
  const [toast, notify] = useToast()
  const [kind, setKind] = useState('summarize')
  const [body, setBody] = useState('')
  const [testText, setTestText] = useState('latest AI news')
  const [testResult, setTestResult] = useState<TestResult | null>(null)

  const list = useQuery({ queryKey: ['prompts'], queryFn: () => get<Prompt[]>('/api/v1/prompts') })
  const prompt = list.data?.find((p) => p.kind === kind)
  useEffect(() => { if (prompt) setBody(prompt.body) }, [prompt])
  const dirty = prompt ? body !== prompt.body : false
  const invalidate = () => void client.invalidateQueries({ queryKey: ['prompts'] })

  const save = useMutation({
    mutationFn: () => { const f = new FormData(); f.set('body', body); return putForm<Prompt>(`/api/v1/prompts/${kind}`, f) },
    onSuccess: () => { notify('Default saved'); invalidate() },
  })
  const test = useMutation({
    mutationFn: () => { const f = new FormData(); f.set('search_text', testText); f.set('max_articles', '2'); return postForm<TestResult>(`/api/v1/prompts/summarize/test`, f) },
    onSuccess: (r) => setTestResult(r),
  })

  return (
    <div>
      {toast}
      <h1>Default prompts</h1>
      <p className="muted small">These are the global defaults, stored as files in the repository. A new profile is filled from them; every profile then keeps its own copy, edited on the Profiles screen. Changing a default here does not change existing profiles.</p>
      <div className="row" style={{ marginBottom: 12 }}>
        {list.data?.map((p) => (
          <button key={p.kind} className={p.kind === kind ? 'primary' : ''} onClick={() => { setKind(p.kind); setTestResult(null) }}>
            {LABELS[p.kind] ?? p.kind}
          </button>
        ))}
      </div>
      <ErrorText error={list.error} />

      {prompt && (
        <div className="card">
          <div className="row between">
            <div className="muted small">
              <code>{prompt.path}</code>{prompt.updated_at && <> · updated {fmtTime(prompt.updated_at)}</>}
              {!prompt.editable && <> · set inline in .env, not editable here</>}
              {prompt.variables.length > 0 && <> · variables: {prompt.variables.map((v) => <code key={v}>{`{{${v}}}`} </code>)}</>}
            </div>
            <div className="row">
              <button className="primary" onClick={() => save.mutate()} disabled={!dirty || !prompt.editable || save.isPending}>Save default</button>
            </div>
          </div>
          <ErrorText error={save.error} />
          <label style={{ marginTop: 10, display: 'block' }}>
            {LABELS[kind] ?? kind}{dirty && <span className="pill">unsaved</span>}
            <textarea value={body} onChange={(e) => setBody(e.target.value)} readOnly={!prompt.editable} style={{ minHeight: kind === 'summarize' ? 420 : 160 }} />
          </label>
        </div>
      )}

      {kind === 'summarize' && (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Test the saved default summarize prompt</h2>
          <p className="muted small">Runs search and the script stage only, with two articles. Takes 20 to 40 seconds. Save first: the test uses the saved file.</p>
          <form className="row" onSubmit={(e) => { e.preventDefault(); test.mutate() }}>
            <input value={testText} onChange={(e) => setTestText(e.target.value)} style={{ minWidth: 320 }} />
            <button className="primary" type="submit" disabled={test.isPending || !testText.trim()}>{test.isPending ? 'Running…' : 'Run test'}</button>
          </form>
          <ErrorText error={test.error} />
          {testResult && (
            <div style={{ marginTop: 10 }}>
              <div className="muted small">{testResult.word_count} words{testResult.target_words ? ` (target ${testResult.target_words})` : ''} · sources: {testResult.sources_used.map((s) => s.source).join(', ')} · run {testResult.run_id}</div>
              <pre style={{ whiteSpace: 'pre-wrap' }}>{testResult.script}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
