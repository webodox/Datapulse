export const API = import.meta.env.VITE_API_URL || ''
// Where external tools should send requests (the Vite dev server proxies /api, but curl can't use it).
export const API_ORIGIN = API || (window.location.port === '5173' ? 'http://localhost:8000' : window.location.origin)

export type Severity = 'critical' | 'warning' | 'info'
export const SEVERITIES: Severity[] = ['critical', 'warning', 'info']

export type Issue = {
  check: string; severity: Severity; title: string; columns: string[]; measured_summary: string
  measured: Record<string, unknown>; examples: string[]; suggested_explanation: string; ai_explanation?: string | null
  consumers: Array<{ name: string; type: string; owner: string; columns: string[]; dataset_wide: boolean }>
}
export type Report = {
  status: 'passed' | 'failed'; issues: Issue[]; rows: { baseline: number; current: number }
  passed_checks: string[]; affected_consumers: string[]; schema_changed: boolean; data_file: string
  ai_note?: string | null; markdown?: string; triage?: Triage | null
  charts?: Record<string, ColumnChart>; fix?: Fix | null
}
export type ColumnChart = {
  null_rate: [number, number | null]
  hist?: { edges: number[]; baseline: number[]; current: number[] }
}
type Counts = { total: number; critical: number; warning: number; info: number }
export type Fix = {
  summary: string; not_fixed: Array<{ issue: string; why: string }>; warnings: string[]
  operations: Array<{ operation: string; args: Record<string, unknown>; reason: string; effect: string }>
  before: Counts; after: Counts & { remaining: string[] }; rows: { before: number; after: number }
  file_name: string; by?: string; at?: string
}
export type Triage = {
  headline: string; verdict: 'block' | 'investigate' | 'safe_to_ship'; verdict_reason: string
  root_causes: Array<{ hypothesis: string; confidence: 'low' | 'medium' | 'high'; evidence: string }>
  actions: Array<{ step: string; owner: string }>; notify: Array<{ who: string; why: string }>; message: string
  steps: Array<{ tool: string; input: Record<string, unknown> }>; model: string; by?: string; at?: string
}
export type RulesDraft = { config_yaml: string; summary: string; not_supported: string[] }
export type ReviewStatus = 'open' | 'acknowledged' | 'resolved'
export type Review = { status: ReviewStatus; by?: string | null; at?: string | null }
export type Run = {
  id: string; status: string; issue_count: number; created_at: string; report: Report
  created_by?: string | null; review: Review
}
export type User = { id: string; name: string; email: string; role: 'admin' | 'member' }
export type Project = { id: string; name: string; has_baseline: boolean; run_count: number; latest_status?: string | null }
export type ColumnStats = { kind: string; dtype: string; null_rate: number | null; unique_count: number | null }
export type Baseline = { baseline_version: number; rows: number; source?: string; created_at?: string; columns: Record<string, ColumnStats> }
export type ProjectDetail = {
  id: string; name: string; has_baseline: boolean; config_yaml: string; fail_on: string; baseline: Baseline | null
  consumers: Array<{ name: string; type: string; owner: string; columns: string[] }>
  runs: Run[]
}
export type Created = { id: string; api_key: string }

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method }
  if (body instanceof FormData) init.body = body
  else if (body !== undefined) Object.assign(init, { body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } })
  const response = await fetch(`${API}${path}`, { ...init, credentials: 'include' })
  // A session that expired or was revoked sends everyone back to the sign-in screen.
  if (response.status === 401 && !path.startsWith('/api/auth/')) window.dispatchEvent(new Event('datapulse:signed-out'))
  if (!response.ok) {
    const detail = await response.json().then(b => b.detail, () => null)
    // FastAPI validation errors arrive as a list of {msg} objects
    throw new Error(typeof detail === 'string' ? detail
      : Array.isArray(detail) ? detail.map((d: { msg: string }) => d.msg).join('; ')
      : `Request failed (${response.status})`)
  }
  return response.json()
}

export const api = {
  authStatus: () => request<{ setup_needed: boolean; user: User | null }>('GET', '/api/auth/status'),
  setup: (name: string, email: string, password: string) => request<User>('POST', '/api/auth/setup', { name, email, password }),
  login: (email: string, password: string) => request<User>('POST', '/api/auth/login', { email, password }),
  logout: () => request('POST', '/api/auth/logout'),
  changePassword: (current_password: string, new_password: string) =>
    request('PUT', '/api/auth/password', { current_password, new_password }),
  users: () => request<User[]>('GET', '/api/users'),
  addUser: (u: { name: string; email: string; password: string; role: User['role'] }) => request<User>('POST', '/api/users', u),
  removeUser: (id: string) => request('DELETE', `/api/users/${id}`),
  fix: (id: string, run: string) => request<Fix>('POST', `/api/projects/${id}/runs/${run}/fix`),
  checkFixed: (id: string, run: string) => request<Report & { run_id: string }>('POST', `/api/projects/${id}/runs/${run}/fixed/check`),
  fixedUrl: (id: string, run: string) => `${API}/api/projects/${id}/runs/${run}/fixed`,
  review: (id: string, run: string, status: ReviewStatus) => request<Review>('PUT', `/api/projects/${id}/runs/${run}/review`, { status }),
  health: () => request<{ version: string; ai: boolean }>('GET', '/api/health'),
  projects: () => request<Project[]>('GET', '/api/projects'),
  project: (id: string) => request<ProjectDetail>('GET', `/api/projects/${id}`),
  create: (name: string) => request<Created>('POST', '/api/projects', { name }),
  seed: () => request<Created>('POST', '/api/demo/seed'),
  remove: (id: string) => request('DELETE', `/api/projects/${id}`),
  saveRules: (id: string, config_yaml: string) => request('PUT', `/api/projects/${id}/config`, { config_yaml }),
  triage: (id: string, run: string) => request<Triage>('POST', `/api/projects/${id}/runs/${run}/triage`),
  draftRules: (id: string, req: string, config_yaml: string) =>
    request<RulesDraft>('POST', `/api/projects/${id}/rules/draft`, { request: req, config_yaml }),
  suggestions: (id: string) => request<{ yaml: string }>('GET', `/api/projects/${id}/suggestions`),
  upload(id: string, kind: 'baseline' | 'runs', file: File, useAi = false) {
    const form = new FormData()
    form.append('file', file)
    if (kind === 'runs') form.append('use_ai', String(useAi))
    return request<Report & { run_id?: string }>('POST', `/api/projects/${id}/${kind}`, form)
  },
}

export const plural = (n: number, word: string) => `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`

export function fmtDate(value?: string) {
  return value ? new Date(value).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '—'
}

export function timeAgo(value?: string) {
  if (!value) return '—'
  const minutes = (Date.now() - new Date(value).getTime()) / 60000
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${Math.round(minutes)}m ago`
  if (minutes < 1440) return `${Math.round(minutes / 60)}h ago`
  return minutes < 10080 ? `${Math.round(minutes / 1440)}d ago` : fmtDate(value)
}

/** Overall state of a run, used for colour and wording everywhere. */
export type Tone = 'failed' | 'issues' | 'clean'
export const runTone = (run: Pick<Run, 'status' | 'issue_count'>): Tone =>
  run.status === 'failed' ? 'failed' : run.issue_count ? 'issues' : 'clean'
export const TONE_LABEL: Record<Tone, string> = { failed: 'Failed', issues: 'Passed with warnings', clean: 'All clear' }
