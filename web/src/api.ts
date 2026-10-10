export const API = import.meta.env.VITE_API_URL || ''

export type Severity = 'critical' | 'warning' | 'info'
export type IssueConsumer = { name: string; type: string; owner: string; columns: string[]; dataset_wide: boolean }
export type Issue = {
  check: string; severity: Severity; title: string; columns: string[]; measured_summary: string;
  suggested_explanation: string; ai_explanation?: string | null; examples: string[]; consumers: IssueConsumer[]
}
export type Report = {
  status: 'passed' | 'failed'; fail_on: string; issues: Issue[]; rows: { baseline: number; current: number };
  counts: Record<Severity, number>; passed_checks: string[]; affected_consumers: string[]; schema_changed: boolean;
  ai_note?: string | null; markdown?: string
}
export type Run = { id: string; status: string; issue_count: number; created_at: string; report: Report }
export type Project = { id: string; name: string; has_baseline: boolean; run_count: number; latest_status?: string | null }
export type Consumer = { name: string; type: string; owner: string; columns: string[] }
export type ColumnStats = { kind: string; dtype: string; null_rate: number | null; unique_count: number | null }
export type ProjectDetail = {
  id: string; name: string; created_at?: string; has_baseline: boolean; config_yaml: string; fail_on: string; consumers: Consumer[];
  baseline?: { baseline_version: number; rows: number; source?: string; created_at?: string; columns: Record<string, ColumnStats> } | null;
  datasets: Array<{ id: string; name: string; role: string; row_count: number; columns: string[]; created_at: string }>;
  runs: Run[];
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, init)
  if (!response.ok) {
    let message = `Request failed (${response.status})`
    try {
      const body = await response.json()
      // FastAPI validation errors arrive as a list of {msg} objects
      message = typeof body.detail === 'string' ? body.detail : Array.isArray(body.detail) ? body.detail.map((d: { msg: string }) => d.msg).join('; ') : message
    } catch { /* keep status message */ }
    throw new Error(message)
  }
  return response.json()
}

export function fmtDate(value?: string) {
  return value ? new Date(value).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '—'
}

export function timeAgo(value?: string) {
  if (!value) return '—'
  const seconds = Math.round((Date.now() - new Date(value).getTime()) / 1000)
  if (seconds < 45) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  return days < 7 ? `${days}d ago` : fmtDate(value)
}
