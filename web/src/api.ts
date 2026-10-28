// Thin client for the /api/v1 routes. One error shape everywhere: {error: {code, stage, message}}.

export interface ApiErrorBody {
  code: string
  stage: string | null
  message: string
}

export class ApiError extends Error {
  code: string
  stage: string | null
  status: number
  constructor(status: number, body: ApiErrorBody) {
    super(body.message)
    this.status = status
    this.code = body.code
    this.stage = body.stage
  }
}

const KEY_STORAGE = 'news-generator.apiKey'

export function getApiKey(): string {
  try {
    return localStorage.getItem(KEY_STORAGE) ?? ''
  } catch {
    return ''
  }
}

export function setApiKey(key: string): void {
  try {
    if (key) localStorage.setItem(KEY_STORAGE, key)
    else localStorage.removeItem(KEY_STORAGE)
  } catch {
    /* storage unavailable: the key lives only for this page load */
  }
}

async function parseError(response: Response): Promise<ApiError> {
  let body: ApiErrorBody = { code: 'unknown', stage: null, message: `${response.status} ${response.statusText}` }
  try {
    const json = await response.json()
    if (json && json.error) body = json.error
    else if (json && json.detail) body = { code: 'invalid', stage: null, message: typeof json.detail === 'string' ? json.detail : JSON.stringify(json.detail) }
  } catch {
    /* non-JSON error body */
  }
  return new ApiError(response.status, body)
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  const key = getApiKey()
  if (key) headers.set('X-API-Key', key)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(path, { ...init, headers })
  if (!response.ok) throw await parseError(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const get = <T>(path: string) => api<T>(path)
export const post = <T>(path: string, body?: unknown) => api<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
export const put = <T>(path: string, body?: unknown) => api<T>(path, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body) })
export const del = <T>(path: string) => api<T>(path, { method: 'DELETE' })
export const postForm = <T>(path: string, form: FormData) => api<T>(path, { method: 'POST', body: form })
export const putForm = <T>(path: string, form: FormData) => api<T>(path, { method: 'PUT', body: form })

/** URL for media the browser loads directly (video, audio, images); the key travels as a query parameter. */
export function mediaUrl(path: string): string {
  const key = getApiKey()
  return key ? `${path}${path.includes('?') ? '&' : '?'}api_key=${encodeURIComponent(key)}` : path
}

// ----------------------------------------------------------------------------- types

export interface Tenant {
  slug: string
  name: string
  active: boolean
  is_admin: boolean
  created_at: string
}

export interface Check {
  service: string
  status: 'ok' | 'warn' | 'fail'
  detail: string
  latency_ms: number
}

export interface Preflight {
  ok: boolean
  checks: Check[]
  checked_at: string
  duration_ms: number
  cached: boolean
}

export interface SystemInfo {
  version: string
  tenant: string
  video: { width: number; height: number; fps: number; workflow: string | null; prompt_enhance: boolean }
  llm_model: string | null
  logging: { file: string; seq: { enabled: boolean; queued?: number; dropped?: number; failures?: number } }
  services: { name: string; configured: boolean; url?: string | null }[]
  storage: { data_dir: string; output_dir: string; free_bytes: number }
}

export interface Artifact {
  url: string
  size: number
  content_type: string
}

export interface Run {
  id: string
  profile_id: string | null
  status: string
  stage: string | null
  priority: number
  search_text: string | null
  queued_at: string | null
  started_at: string | null
  finished_at: string | null
  error_stage: string | null
  error_message: string | null
  retry_of: string | null
  cancel_requested: boolean
  preflight: Preflight | null
  artifacts: Record<string, Artifact | null>
  settings?: Record<string, unknown>
  metrics?: Record<string, number | null>
  selected_sources?: { title: string; url: string; source: string; outlet: string; published_at: string; source_rank_score: number | null; extraction?: { status: string; words: number } | null }[]
}

export interface Page<T> {
  items: T[]
  next_cursor: string | null
}

export interface Profile {
  id: string
  name: string
  search_text: string
  categories: string | null
  language: string | null
  safesearch: string | null
  max_articles: number | null
  max_age_days: number | null
  rank_query: string | null
  rank_candidates: number | null
  voice: string | null
  avatar: string | null
  intended_video_max_seconds: number | null
  wpm: number | null
  summarize_prompt: string | null
  video_visual_prompt: string | null
  video_sound_prompt: string | null
  hero_prompt: string | null
  hero_workflow: string | null
  created_at: string
  updated_at: string
}

export interface Schedule {
  id: string
  name: string | null
  profile_id: string
  cron: string
  timezone: string
  enabled: boolean
  last_run_id: string | null
  last_run_at: string | null
  next_run_at: string | null
  last_error: string | null
}

export interface Prompt {
  kind: string
  body: string
  path: string
  editable: boolean
  variables: string[]
  updated_at: string | null
}

export interface Avatar {
  name: string
  image_url: string
  prompt?: string | null
  negative_prompt?: string | null
  seed?: string | null
  width: number
  height: number
  workflow?: string | null
  source: string
  created_at: string
}

export interface Voice {
  name: string
  language: string
  library_name?: string
  sample_url: string | null
  created_at: string
}

export interface ApiKeyInfo {
  id: string
  prefix: string
  label: string
  created_at: string
  revoked_at: string | null
  api_key?: string
}

export type AvatarOptions = {
  workflows: { name: string; seed_max: number | string; default: boolean }[]
  default_workflow: string | null
  hero_workflow: string | null
  negative_prompt: string
  width: number
  height: number
}
