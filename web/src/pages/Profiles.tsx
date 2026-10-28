import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { del, get, post, put, type Avatar, type AvatarOptions, type Page, type Profile, type Prompt, type Run, type Voice } from '../api'
import { Confirm, ErrorText, Help, fmtTime, useToast } from '../components'

type Draft = Partial<Record<keyof Profile, string>>
const FIELDS: (keyof Profile)[] = ['name', 'search_text', 'max_articles', 'max_age_days', 'voice', 'avatar', 'intended_video_max_seconds', 'wpm', 'rank_query', 'categories', 'language', 'summarize_prompt', 'video_visual_prompt', 'video_sound_prompt', 'hero_prompt', 'hero_workflow']
const NUMERIC = new Set(['max_articles', 'max_age_days', 'rank_candidates', 'intended_video_max_seconds', 'wpm'])

interface Options {
  categories: string[]
  defaults: { max_articles: number; max_age_days: number; categories: string[]; language: string; intended_video_max_seconds: number; wpm: number; rank_candidates: number; rank_enabled: boolean }
}
interface PreviewItem { title: string; url: string; source: string; outlet: string; published_at: string | null; score: number | null; snippet: string; engines: string[]; extraction?: { status: string; words: number; error?: string | null } | null }
interface Preview {
  search_text: string
  request_url: string
  raw_count: number
  dated_count: number
  candidate_count: number
  selected: PreviewItem[]
  rank: { enabled?: boolean; query?: string; model?: string; degraded?: boolean; error?: string }
  unresponsive_engines: string[][]
  effective: Record<string, string | number>
}

// Field help: shown under each input and as a hover tooltip.
const HELP: Record<string, { hint: string; placeholder?: string }> = {
  name: { hint: 'Your label for this profile. Shown in run lists and schedules.', placeholder: 'AI daily brief' },
  search_text: { hint: 'Your search terms, sent to SearXNG as the query. Any topic works: "NASA Artemis launch", "Detroit housing market", "latest AI news".', placeholder: 'NASA Artemis launch' },
  max_articles: { hint: 'How many distinct stories the script is written from. Further articles on a story already picked are folded into it and credited, not counted. 1 to 3 keeps runs short.' },
  max_age_days: { hint: 'Only articles published within this many days are used. Undated pages are always dropped.' },
  voice: { hint: 'Chatterbox voice for the narration. Tenant voices first, then the shared library.' },
  avatar: { hint: 'The anchor image the video is rendered from. Create one on the Avatars screen.' },
  intended_video_max_seconds: { hint: 'Target length of the spoken script in seconds. Sets the word cap the writer must respect.' },
  wpm: { hint: 'Speaking rate used to turn seconds into the word cap.' },
  rank_query: { hint: 'Optional. What "most relevant" means when ranking candidate articles. Blank ranks by your search terms, which is right for most profiles.', placeholder: 'same as search terms' },
  categories: { hint: 'Which SearXNG engine groups to query. These are not keywords; put those in Search terms.' },
  language: { hint: 'SearXNG language code.', placeholder: 'en' },
  summarize_prompt: { hint: 'The script writer prompt for this profile. Filled from the default file when the profile is created; edit freely, it is saved with the profile. Clear it to refill from the default on save.' },
  video_visual_prompt: { hint: 'What the video model is told about the scene, the anchor, and the camera. Saved with the profile.' },
  video_sound_prompt: { hint: 'What the video model is told about the soundtrack. Saved with the profile.' },
  hero_prompt: { hint: 'Instruction that turns each segment into an image prompt for the hero shown on the anchor\'s green monitor. Saved with the profile.' },
  hero_workflow: { hint: 'Image template used to render the segment heroes. Sized automatically to the anchor\'s green panel.' },
}

function toBody(draft: Draft): Record<string, unknown> {
  const body: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(draft)) {
    if (value === undefined) continue
    if (value === '') { body[key] = null; continue }
    body[key] = NUMERIC.has(key) ? Number(value) : value
  }
  return body
}

function fromProfile(p: Profile): Draft {
  const draft: Draft = {}
  for (const key of FIELDS) draft[key] = p[key] == null ? '' : String(p[key])
  return draft
}

export default function Profiles() {
  const client = useQueryClient()
  const navigate = useNavigate()
  const [toast, notify] = useToast()
  const [editing, setEditing] = useState<string | 'new' | null>(null)
  const [draft, setDraft] = useState<Draft>({})
  const [preview, setPreview] = useState<Preview | null>(null)
  const [previewExtract, setPreviewExtract] = useState(false)

  const profiles = useQuery({ queryKey: ['profiles'], queryFn: () => get<Page<Profile>>('/api/v1/profiles?limit=200') })
  const voices = useQuery({ queryKey: ['voices'], queryFn: () => get<Page<Voice>>('/api/v1/voices?limit=200') })
  const avatars = useQuery({ queryKey: ['avatars'], queryFn: () => get<Page<Avatar>>('/api/v1/avatars?limit=200') })
  const libraryVoices = useQuery({ queryKey: ['library-voices'], queryFn: () => get<{ voices: { name: string }[] }>('/voices') })
  const options = useQuery({ queryKey: ['search-options'], queryFn: () => get<Options>('/api/v1/search/options'), staleTime: 300_000 })
  const promptDefaults = useQuery({ queryKey: ['prompts'], queryFn: () => get<Prompt[]>('/api/v1/prompts'), staleTime: 300_000 })
  const promptDefault = (kind: string) => promptDefaults.data?.find((p) => p.kind === kind)?.body ?? ''
  const avatarOptionsQuery = useQuery({ queryKey: ['avatar-options'], queryFn: () => get<AvatarOptions>('/api/v1/avatars/options'), staleTime: 300_000 })

  const voiceOptions = Array.from(new Set([...(voices.data?.items.map((v) => v.name) ?? []), ...(libraryVoices.data?.voices.map((v) => v.name) ?? [])])).sort()
  const avatarOptions = avatars.data?.items.map((a) => a.name) ?? []
  const knownCategories = options.data?.categories ?? []

  // Every input starts with a real value: .env defaults, the default categories, the first voice and avatar.
  const defaultDraft = (): Draft => {
    const d = options.data?.defaults
    return {
      name: '',
      search_text: '',
      max_articles: String(d?.max_articles ?? 5),
      max_age_days: String(d?.max_age_days ?? 1),
      voice: voiceOptions[0] ?? '',
      avatar: avatarOptions[0] ?? '',
      intended_video_max_seconds: String(d?.intended_video_max_seconds ?? 30),
      wpm: String(d?.wpm ?? 145),
      rank_query: '',
      categories: (d?.categories ?? ['news', 'general']).join(','),
      language: d?.language ?? 'en',
      summarize_prompt: promptDefault('summarize'),
      video_visual_prompt: promptDefault('video_visual'),
      video_sound_prompt: promptDefault('video_sound'),
      hero_prompt: promptDefault('hero_image'),
      hero_workflow: avatarOptionsQuery.data?.hero_workflow ?? '',
    }
  }
  useEffect(() => {
    if (editing) setDraft((current) => ({ ...defaultDraft(), ...Object.fromEntries(Object.entries(current).filter(([, v]) => v)) }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing, options.data, promptDefaults.data, avatarOptionsQuery.data, voiceOptions.length, avatarOptions.length])

  const invalidate = () => void client.invalidateQueries({ queryKey: ['profiles'] })
  const save = useMutation({
    mutationFn: () => {
      // Categories are exactly the checked boxes, every save. Anything else in the draft is discarded.
      const clean: Draft = { ...draft, categories: selectedCategories.filter((c) => knownCategories.includes(c)).join(',') }
      return editing === 'new' ? post<Profile>('/api/v1/profiles', toBody(clean)) : put<Profile>(`/api/v1/profiles/${editing}`, toBody(clean))
    },
    onSuccess: (p) => { notify(`Saved ${p.name}`); setEditing(null); setPreview(null); invalidate() },
  })
  const remove = useMutation({ mutationFn: (id: string) => del(`/api/v1/profiles/${id}`), onSuccess: () => { notify('Deleted'); invalidate() } })
  const runNow = useMutation({
    mutationFn: (id: string) => post<Run>('/api/v1/runs', { profile_id: id }),
    onSuccess: (run) => { notify(`Queued run ${run.id.slice(-8)}`); navigate(`/runs/${run.id}`) },
  })
  const previewSearch = useMutation({
    mutationFn: () => post<Preview>('/api/v1/search/preview', {
      search_text: draft.search_text, categories: draft.categories || null, language: draft.language || null,
      max_articles: draft.max_articles ? Number(draft.max_articles) : null, max_age_days: draft.max_age_days ? Number(draft.max_age_days) : null,
      rank_query: draft.rank_query || null, extract: previewExtract,
    }),
    onSuccess: setPreview,
  })

  const selectedCategories = (draft.categories ?? '').split(',').map((c) => c.trim()).filter(Boolean)
  const unknownCategories = selectedCategories.filter((c) => knownCategories.length > 0 && !knownCategories.includes(c))
  const toggleCategory = (c: string) => {
    const next = selectedCategories.includes(c) ? selectedCategories.filter((x) => x !== c) : [...selectedCategories, c]
    setDraft({ ...draft, categories: next.join(',') })
  }

  const field = (key: keyof Profile, label: string, kind: 'text' | 'number' | 'voice' | 'avatar' | 'textarea' = 'text', full = false, rows = 4) => {
    const help = HELP[key]
    return (
      <label className={full ? 'full' : ''}>
        <span className="label-row">{label}{help && <Help text={help.hint} />}</span>
        {kind === 'textarea' ? (
          <textarea value={draft[key] ?? ''} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })} rows={rows} style={{ fontFamily: 'inherit' }} />
        ) : kind === 'voice' || kind === 'avatar' ? (
          <select value={draft[key] ?? ''} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}>
            <option value="">—</option>
            {(kind === 'voice' ? voiceOptions : avatarOptions).map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        ) : (
          <input type={kind} value={draft[key] ?? ''} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })} placeholder={help?.placeholder} />
        )}
      </label>
    )
  }

  return (
    <div>
      {toast}
      <div className="row between">
        <h1>Search profiles</h1>
        <button className="primary" onClick={() => { setEditing('new'); setDraft(defaultDraft()); setPreview(null) }}>New profile</button>
      </div>
      <p className="muted small">A profile is everything one run needs: search terms, how many articles, which voice and avatar, and how long the script should be. Preview the search to see which articles it would pick, adjust, then save and run it from here, from the Runs screen, or on a schedule.</p>
      <ErrorText error={profiles.error} />
      <ErrorText error={remove.error} />
      <ErrorText error={runNow.error} />

      {editing && (
        <form className="inline" onSubmit={(e) => { e.preventDefault(); save.mutate() }}>
          {field('name', 'Name')}
          {field('search_text', 'Search terms', 'text', true)}
          {field('max_articles', 'Max articles', 'number')}
          {field('max_age_days', 'Max age (days)', 'number')}
          {field('voice', 'Voice', 'voice')}
          {field('avatar', 'Avatar', 'avatar')}
          {field('intended_video_max_seconds', 'Target seconds', 'number')}
          {field('wpm', 'Words per minute', 'number')}
          {field('rank_query', 'Rank query', 'text', true)}
          <div className="full">
            <label><span className="label-row">SearXNG categories<Help text={HELP.categories.hint} /></span></label>
            <div className="row" style={{ marginTop: 4 }}>
              {knownCategories.map((c) => (
                <label key={c} className="check" title={`Query the SearXNG "${c}" engine group`}>
                  <input type="checkbox" checked={selectedCategories.includes(c)} onChange={() => toggleCategory(c)} /> {c}
                  {options.data?.defaults.categories.includes(c) && <span className="pill">default</span>}
                </label>
              ))}
              {options.isLoading && <span className="muted small">Loading categories from SearXNG…</span>}
              {options.error && <span className="error small">Could not load categories from SearXNG.</span>}
            </div>
            {unknownCategories.length > 0 && <span className="error small">Ignored (not SearXNG categories, not saved): {unknownCategories.join(', ')}</span>}
          </div>
          {field('language', 'Language')}
          {field('summarize_prompt', 'Summarize prompt (script writer)', 'textarea', true, 14)}
          {field('video_visual_prompt', 'Video visual prompt', 'textarea', true, 6)}
          {field('video_sound_prompt', 'Video sound prompt', 'textarea', true, 2)}
          {field('hero_prompt', 'Hero image prompt (per segment)', 'textarea', true, 6)}
          <label><span className="label-row">Hero image template<Help text={HELP.hero_workflow.hint} /></span>
            <select value={draft.hero_workflow ?? ''} onChange={(e) => setDraft({ ...draft, hero_workflow: e.target.value })}>
              {avatarOptionsQuery.data?.workflows.map((w) => <option key={w.name} value={w.name}>{w.name}</option>)}
            </select>
          </label>

          <div className="row full">
            <button type="button" onClick={() => previewSearch.mutate()} disabled={previewSearch.isPending || !draft.search_text?.trim()} title="Run the search and ranking with these values. No script, no video, no run.">
              {previewSearch.isPending ? 'Searching…' : 'Preview search'}
            </button>
            <label className="check" title="Also fetch each selected article through crawl4ai and show how much text it yields. Adds a few seconds per article."><input type="checkbox" checked={previewExtract} onChange={(e) => setPreviewExtract(e.target.checked)} /> test article extraction</label>
            <button className="primary" type="submit" disabled={save.isPending}>{editing === 'new' ? 'Create' : 'Save'}</button>
            <button type="button" onClick={() => { setEditing(null); setPreview(null) }}>Cancel</button>
            <ErrorText error={save.error} />
            <ErrorText error={previewSearch.error} />
          </div>

          {preview && (
            <div className="full" style={{ marginTop: 8 }}>
              <div className="row between">
                <strong>Search preview</strong>
                <span className="muted small">{preview.raw_count} raw · {preview.dated_count} dated · {preview.candidate_count} within {preview.effective.max_age_days} day(s) · ranked by "{preview.effective.rank_query}"{preview.rank.degraded ? ' · ranking degraded' : ''}</span>
              </div>
              {preview.unresponsive_engines.length > 0 && <p className="muted small">Engines not answering right now: {preview.unresponsive_engines.map((e) => e[0]).join(', ')}</p>}
              {preview.selected.length === 0 ? (
                <p className="error">No usable articles. A run with these settings would fail at the search stage. Broaden the terms, raise max age, or add categories.</p>
              ) : (
                <table>
                  <thead><tr><th>#</th><th>Outlet</th><th>Article</th><th>Published</th><th>Score</th>{preview.selected.some((s) => s.extraction) && <th>Extraction</th>}</tr></thead>
                  <tbody>
                    {preview.selected.map((s, i) => (
                      <tr key={s.url}>
                        <td>{i + 1}</td>
                        <td>{s.outlet}</td>
                        <td><a href={s.url} target="_blank" rel="noreferrer">{s.title}</a><div className="muted small">{s.snippet}</div></td>
                        <td className="muted small">{fmtTime(s.published_at)}</td>
                        <td className="mono">{s.score?.toFixed(3) ?? '—'}</td>
                        {preview.selected.some((x) => x.extraction) && <td className="small">{s.extraction ? <span className={`status ${s.extraction.status === 'extracted' ? 'ok' : 'warn'}`}>{s.extraction.status}{s.extraction.words ? ` · ${s.extraction.words} words` : ''}</span> : '—'}{s.extraction?.error && <div className="muted">{s.extraction.error}</div>}</td>}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </form>
      )}

      {!editing && (
      <div>
        <table>
          <thead><tr><th>Name</th><th>Search terms</th><th>Articles</th><th>Age</th><th>Categories</th><th>Voice</th><th>Avatar</th><th>Updated</th><th></th></tr></thead>
          <tbody>
            {profiles.data?.items.map((p) => {
              const bad = (p.categories ?? '').split(',').map((c) => c.trim()).filter((c) => c && knownCategories.length > 0 && !knownCategories.includes(c))
              return (
                <tr key={p.id}>
                  <td>{p.name}</td>
                  <td>{p.search_text}</td>
                  <td>{p.max_articles ?? '—'}</td>
                  <td>{p.max_age_days ?? '—'}</td>
                  <td>{p.categories ?? <span className="muted">default</span>}{bad.length > 0 && <div className="error small" title="Not SearXNG categories; edit the profile">invalid: {bad.join(', ')}</div>}</td>
                  <td>{p.voice ?? <span className="error">none</span>}</td>
                  <td>{p.avatar ?? <span className="error">none</span>}</td>
                  <td className="muted small">{fmtTime(p.updated_at)}</td>
                  <td className="row">
                    <button className="primary" onClick={() => runNow.mutate(p.id)} disabled={runNow.isPending || !p.voice || !p.avatar} title={!p.voice || !p.avatar ? 'Set a voice and an avatar first' : 'Queue a run of this profile now'}>Run now</button>
                    <button onClick={() => { setEditing(p.id); setDraft(fromProfile(p)); setPreview(null) }}>Edit</button>
                    <Confirm label="Delete" onConfirm={() => remove.mutate(p.id)} />
                  </td>
                </tr>
              )
            })}
            {profiles.data?.items.length === 0 && <tr><td colSpan={9} className="muted">No profiles yet.</td></tr>}
          </tbody>
        </table>
      </div>
      )}
    </div>
  )
}
