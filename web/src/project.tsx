import { useCallback, useEffect, useState } from 'react'
import { api, API_ORIGIN, fmtDate, plural, runTone, timeAgo, SEVERITIES, TONE_LABEL,
  type Issue, type ProjectDetail, type Run, type Severity } from './api'
import type { Route } from './App'
import { Icon, type IconName } from './icons'
import { CodeBlock, copyText, Dropzone, Empty, Spinner, useAction, useNotify } from './ui'

export const TABS = ['checks', 'baseline', 'rules', 'api'] as const
export type Tab = typeof TABS[number]
const TAB_LABEL: Record<Tab, string> = { checks: 'Checks', baseline: 'Baseline', rules: 'Rules', api: 'Send data' }
const SEVERITY_ICON: Record<Severity, IconName> = { critical: 'alert', warning: 'flag', info: 'info' }
const FAIL_ON: Record<string, string> = {
  critical: 'Fails on critical issues', warning: 'Fails on warnings or worse', info: 'Fails on any issue', never: 'Never fails',
}

type Props = { route: Route & { id: string }; go: (r: Route) => void; onChanged: () => Promise<unknown>; apiKey?: string; dismissKey: () => void }

export function ProjectView({ route, go, onChanged, apiKey, dismissKey }: Props) {
  const notify = useNotify()
  const [detail, setDetail] = useState<ProjectDetail | null>(null)
  const [draft, setDraft] = useState('') // rules editor text survives tab switches
  const reload = useCallback(() => api.project(route.id).then(d => { setDetail(d); return d }), [route.id])
  useEffect(() => { reload().then(d => setDraft(d.config_yaml), notify.fail) }, [reload, notify])

  const dirty = !!detail && draft !== detail.config_yaml
  useEffect(() => {
    if (!dirty) return
    const warn = (e: BeforeUnloadEvent) => e.preventDefault()
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  if (!detail) return <div className="loading"><span className="spinner" /> Loading project…</div>

  const latest = detail.runs[0]
  const changed = () => reload().then(onChanged)
  const remove = async () => {
    if (!window.confirm(`Delete "${detail.name}" with its baseline and ${plural(detail.runs.length, 'check')}? This can't be undone.`)) return
    try { await api.remove(detail.id); await onChanged(); go({ tab: 'checks' }); notify.ok(`Deleted ${detail.name}`) } catch (e) { notify.fail(e) }
  }

  return <div className="project">
    <header className="project-head">
      <div>
        <h1>{detail.name}</h1>
        <p className="status-line">
          {!detail.has_baseline ? <><i className="dot" /> Waiting for a baseline</>
            : latest ? <><i className={`dot ${latest.status}`} /> {TONE_LABEL[runTone(latest)]} · last checked {timeAgo(latest.created_at)}</>
            : <><i className="dot" /> Ready for its first check</>}
          <span className="sep">·</span>{FAIL_ON[detail.fail_on]}
        </p>
      </div>
      <button className="button ghost danger" onClick={remove}><Icon name="trash" size={15} /> Delete</button>
    </header>

    {apiKey && <div className="banner">
      <Icon name="shield" size={18} />
      <div><b>Copy this project's API key now. It won't be shown again.</b><code>{apiKey}</code></div>
      <button className="button outline" onClick={() => copyText(apiKey, notify, 'API key copied')}><Icon name="copy" size={14} /> Copy</button>
      <button className="icon-btn" aria-label="Dismiss" onClick={dismissKey}><Icon name="x" /></button>
    </div>}

    <nav className="tabs" role="tablist">
      {TABS.map(t => <a key={t} role="tab" aria-selected={route.tab === t} href={`#/p/${detail.id}/${t}`} className={route.tab === t ? 'active' : ''}>
        {TAB_LABEL[t]}{t === 'checks' && detail.runs.length > 0 && <span className="count">{detail.runs.length}</span>}
        {t === 'rules' && dirty && <span className="unsaved" title="Unsaved changes" />}
      </a>)}
    </nav>

    {route.tab === 'checks' && <ChecksTab detail={detail} runId={route.run} select={run => go({ ...route, run })} changed={changed} />}
    {route.tab === 'baseline' && <BaselineTab detail={detail} changed={changed} />}
    {route.tab === 'rules' && <RulesTab detail={detail} draft={draft} setDraft={setDraft} dirty={dirty} changed={changed} />}
    {route.tab === 'api' && <ApiTab detail={detail} apiKey={apiKey} />}
  </div>
}

/* ---------- checks ---------- */

function ChecksTab({ detail, runId, select, changed }: { detail: ProjectDetail; runId?: string; select: (id?: string) => void; changed: () => Promise<unknown> }) {
  const notify = useNotify()
  const [busy, run] = useAction()
  const [useAi, setUseAi] = useState(false)

  if (!detail.has_baseline) return <div className="card setup">
    <span className="step">Step 1 of 2</span>
    <h2>Start with a file you trust</h2>
    <p className="muted">DataPulse profiles it (columns, types, null rates, value ranges) and compares every future upload against that profile.</p>
    <Dropzone title="Drop your known-good data file" hint="CSV, JSON, JSONL or Parquet · up to 25 MB" busy={busy && 'Profiling your data…'}
      onFile={f => run('baseline', async () => { await api.upload(detail.id, 'baseline', f); await changed(); notify.ok('Baseline saved. Now upload a new version to check it.') })} />
  </div>

  const viewed = detail.runs.find(r => r.id === runId) || detail.runs[0]
  const check = (file: File) => run('check', async () => {
    const report = await api.upload(detail.id, 'runs', file, useAi)
    await changed()
    select(report.run_id)
    notify.ok(report.status === 'failed' ? `Check failed: ${plural(report.issues.length, 'issue')} to review` : 'Check passed')
  })

  return <>
    <div className="card">
      <Dropzone title="Drop a new version of the data to check it" hint={`Compared against baseline v${detail.baseline?.baseline_version} · CSV, JSON, JSONL or Parquet`}
        busy={busy && 'Checking your data…'} onFile={check} />
      <label className="toggle"><input type="checkbox" checked={useAi} onChange={e => setUseAi(e.target.checked)} />
        Add AI explanations <small>(sends aggregate statistics only, never rows; needs ANTHROPIC_API_KEY on the server)</small></label>
    </div>
    {viewed ? <div className="checks-layout">
      <RunHistory runs={detail.runs} selected={viewed.id} select={select} />
      <RunReport key={viewed.id} run={viewed} isLatest={viewed.id === detail.runs[0].id} />
    </div> : <Empty icon="activity" title="No checks yet"><span>Drop a file above. Results and history appear here.</span></Empty>}
  </>
}

function RunHistory({ runs, selected, select }: { runs: Run[]; selected: string; select: (id: string) => void }) {
  const max = Math.max(1, ...runs.map(r => r.issue_count))
  return <aside className="card history">
    <h3>History</h3>
    <div className="bars" aria-hidden="true">
      {[...runs].reverse().map(r => <i key={r.id} className={`${runTone(r)} ${r.id === selected ? 'on' : ''}`}
        style={{ height: `${12 + 88 * r.issue_count / max}%` }} title={`${fmtDate(r.created_at)}: ${plural(r.issue_count, 'issue')}`}
        onClick={() => select(r.id)} />)}
    </div>
    <ul>
      {runs.map(r => <li key={r.id}><button className={r.id === selected ? 'on' : ''} onClick={() => select(r.id)}>
        <ToneIcon tone={runTone(r)} />
        <span><b>{r.report.data_file || 'Upload'}</b><small>{fmtDate(r.created_at)}</small></span>
        <em className={runTone(r)}>{r.issue_count || '✓'}</em>
      </button></li>)}
    </ul>
  </aside>
}

function RunReport({ run, isLatest }: { run: Run; isLatest: boolean }) {
  const notify = useNotify()
  const [filter, setFilter] = useState<Severity | null>(null)
  const { report } = run
  const tone = runTone(run)
  const counts = SEVERITIES.map(s => [s, report.issues.filter(i => i.severity === s).length] as const).filter(([, n]) => n)
  const shown = report.issues.filter(i => !filter || i.severity === filter)

  return <section className="card report">
    <div className={`verdict ${tone}`}>
      <ToneIcon tone={tone} big />
      <div>
        <h2>{tone === 'failed' ? 'This data would break things' : tone === 'issues' ? 'Passed, with warnings' : 'Everything looks normal'}</h2>
        <p>{report.data_file} · {fmtDate(run.created_at)}{!isLatest && ' · earlier check'} · {report.rows.current.toLocaleString()} rows
          ({report.rows.current >= report.rows.baseline ? '+' : ''}{(report.rows.current - report.rows.baseline).toLocaleString()} vs baseline)</p>
      </div>
      {report.markdown && <button className="button ghost" onClick={() => copyText(report.markdown!, notify, 'Markdown report copied')}>
        <Icon name="copy" size={14} /> Copy report</button>}
    </div>

    {counts.length > 1 && <div className="filters">
      <button className={!filter ? 'on' : ''} onClick={() => setFilter(null)}>All {report.issues.length}</button>
      {counts.map(([s, n]) => <button key={s} className={`${s} ${filter === s ? 'on' : ''}`} onClick={() => setFilter(s)}>
        <Icon name={SEVERITY_ICON[s]} size={13} /> {n} {s}</button>)}
    </div>}

    {shown.map((issue, i) => <IssueCard key={`${issue.check}-${issue.columns}-${i}`} issue={issue} />)}
    {report.ai_note && <p className="note"><Icon name="sparkle" size={13} /> {report.ai_note}</p>}
    {report.passed_checks.length > 0 && <p className="passed"><Icon name="check" size={14} /> Passed: {report.passed_checks.map(c => c.replace('_', ' ')).join(', ')}</p>}
  </section>
}

function IssueCard({ issue }: { issue: Issue }) {
  const measured = Object.entries(issue.measured || {})
  return <article className={`issue ${issue.severity}`}>
    <header>
      <span className={`pill ${issue.severity}`}><Icon name={SEVERITY_ICON[issue.severity]} size={12} /> {issue.severity}</span>
      <h3>{issue.title}</h3>
    </header>
    <p>{issue.measured_summary}</p>
    <p className="cause"><b>Possible cause</b> <small>(suggestion, not verified)</small> {issue.suggested_explanation}</p>
    {issue.ai_explanation && <p className="cause ai"><b><Icon name="sparkle" size={12} /> AI suggestion</b> <small>(unverified)</small> {issue.ai_explanation}</p>}
    {issue.consumers.length > 0 && <p className="affects"><b>May affect</b>{issue.consumers.map(c =>
      <span key={c.name} className="chip" title={c.dataset_wide ? 'Reads this dataset' : `Uses ${c.columns.join(', ')}`}>{c.name}</span>)}</p>}
    {(measured.length > 0 || issue.examples.length > 0) && <details>
      <summary>Details</summary>
      <dl>{measured.map(([k, v]) => <div key={k}><dt>{k.replace(/_/g, ' ')}</dt><dd>{fmtValue(v)}</dd></div>)}</dl>
      {issue.examples.length > 0 && <p className="examples">Examples: {issue.examples.join(' · ')}</p>}
      <small className="muted">Check: <code>{issue.check}</code></small>
    </details>}
  </article>
}

const fmtValue = (v: unknown) => v === null || v === undefined ? 'n/a' : typeof v === 'boolean' ? (v ? 'yes' : 'no')
  : typeof v === 'number' ? v.toLocaleString(undefined, { maximumFractionDigits: 4 }) : String(v)

function ToneIcon({ tone, big }: { tone: ReturnType<typeof runTone>; big?: boolean }) {
  return <span className={`tone ${tone} ${big ? 'big' : ''}`}><Icon name={tone === 'failed' ? 'alert' : tone === 'issues' ? 'flag' : 'check'} size={big ? 20 : 13} /></span>
}

/* ---------- baseline ---------- */

function BaselineTab({ detail, changed }: { detail: ProjectDetail; changed: () => Promise<unknown> }) {
  const notify = useNotify()
  const [busy, run] = useAction()
  const [query, setQuery] = useState('')
  const b = detail.baseline
  if (!b) return <Empty icon="database" title="No baseline yet"><a className="button primary" href={`#/p/${detail.id}/checks`}>Add one</a></Empty>
  const columns = Object.entries(b.columns).filter(([name]) => name.toLowerCase().includes(query.trim().toLowerCase()))

  const replace = (file: File) => {
    if (!window.confirm(`Replace baseline v${b.baseline_version} with ${file.name}? Future checks will compare against the new file.`)) return
    run('replace', async () => { await api.upload(detail.id, 'baseline', file); await changed(); notify.ok('Baseline replaced') })
  }

  return <>
    <div className="card facts">
      <div><small>Version</small><b>v{b.baseline_version}</b></div>
      <div><small>Rows</small><b>{b.rows.toLocaleString()}</b></div>
      <div><small>Columns</small><b>{Object.keys(b.columns).length}</b></div>
      <div><small>From file</small><b title={b.source}>{b.source || '—'}</b></div>
      <div><small>Saved</small><b>{fmtDate(b.created_at)}</b></div>
    </div>
    <div className="card">
      <div className="card-head"><h2>Columns</h2>
        {Object.keys(b.columns).length > 8 && <input className="search" placeholder="Filter columns…" value={query} onChange={e => setQuery(e.target.value)} aria-label="Filter columns" />}</div>
      <div className="table-wrap"><table>
        <thead><tr><th>Column</th><th>Type</th><th>Empty values</th><th className="num">Distinct values</th></tr></thead>
        <tbody>{columns.map(([name, s]) => <tr key={name}>
          <td><code>{name}</code></td>
          <td>{s.kind} <small className="muted">{s.dtype}</small></td>
          <td><span className="meter"><i style={{ width: `${(s.null_rate ?? 0) * 100}%` }} /></span> {((s.null_rate ?? 0) * 100).toFixed(1)}%</td>
          <td className="num">{s.unique_count?.toLocaleString() ?? '—'}</td>
        </tr>)}</tbody>
      </table>{!columns.length && <p className="muted pad">No columns match "{query}".</p>}</div>
    </div>
    <div className="card">
      <h2>Replace the baseline</h2>
      <p className="muted">Do this after an intentional change to the data. The version number goes up and older checks keep their results.</p>
      <Dropzone title="Drop the new known-good file" hint="You'll be asked to confirm" busy={busy && 'Profiling…'} onFile={replace} />
    </div>
  </>
}

/* ---------- rules ---------- */

const CHEATSHEET: Array<[string, string]> = [
  ['fail_on', 'Which severity fails a check: critical, warning, info or never'],
  ['schema', 'Missing columns, new columns, type changes'],
  ['row_count', 'Row count change beyond max_change_pct, or below min_rows'],
  ['null_rate', 'Empty values rising by more than max_increase (0.05 = 5 points)'],
  ['categorical', 'Text values never seen in the baseline'],
  ['duplicates', 'Repeated key columns or whole rows'],
  ['distribution', 'Numbers shifting (PSI and mean shift)'],
  ['consumers', 'Dashboards, models or services and the columns they read'],
]

function RulesTab({ detail, draft, setDraft, dirty, changed }:
  { detail: ProjectDetail; draft: string; setDraft: (s: string) => void; dirty: boolean; changed: () => Promise<unknown> }) {
  const notify = useNotify()
  const [busy, run] = useAction()
  const [suggested, setSuggested] = useState('')
  const save = () => run('save', async () => { await api.saveRules(detail.id, draft); await changed(); notify.ok('Rules saved') })

  return <div className="rules-layout">
    <div className="card">
      <div className="card-head">
        <h2>datapulse.yml</h2>
        <span className="muted">{dirty ? 'Unsaved changes' : 'Saved'}</span>
      </div>
      <textarea className="editor" value={draft} onChange={e => setDraft(e.target.value)} spellCheck={false} aria-label="Rules (YAML)"
        onKeyDown={e => { if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); if (dirty) save() } }} />
      <div className="actions">
        <button className="button primary" onClick={save} disabled={!dirty || !!busy}>{busy === 'save' ? <Spinner text="Saving…" /> : 'Save rules'}</button>
        <button className="button ghost" onClick={() => setDraft(detail.config_yaml)} disabled={!dirty}>Discard changes</button>
        <span className="muted hint">Ctrl/⌘ + S saves · validated exactly like the CLI</span>
      </div>
      {suggested && <><h3>Suggested from your baseline</h3><p className="muted">Nothing was applied. Copy the parts you want into the editor.</p><CodeBlock code={suggested} /></>}
    </div>
    <aside>
      <div className="card">
        <h3>Who uses this data</h3>
        {detail.consumers.length ? <ul className="consumers">{detail.consumers.map(c => <li key={c.name}>
          <b>{c.name}</b><small>{c.type}{c.owner && ` · ${c.owner}`}</small><span>{c.columns.join(', ')}</span>
        </li>)}</ul> : <p className="muted">None yet. Add a <code>consumers:</code> section so each issue shows what it may break.</p>}
      </div>
      <div className="card">
        <h3>Quick reference</h3>
        <dl className="cheats">{CHEATSHEET.map(([k, v]) => <div key={k}><dt><code>{k}</code></dt><dd>{v}</dd></div>)}</dl>
        {detail.has_baseline && <button className="button outline full" disabled={!!busy}
          onClick={() => run('suggest', async () => setSuggested((await api.suggestions(detail.id)).yaml))}>
          {busy === 'suggest' ? <Spinner text="Thinking…" /> : <><Icon name="sparkle" size={14} /> Suggest rules from baseline</>}</button>}
      </div>
    </aside>
  </div>
}

/* ---------- send data from other services ---------- */

function ApiTab({ detail, apiKey }: { detail: ProjectDetail; apiKey?: string }) {
  const url = `${API_ORIGIN}/api/projects/${detail.id}/ingest`
  const curl = `curl -X POST ${url} \\
  -H 'Content-Type: application/json' \\
  -H 'X-DataPulse-Key: ${apiKey || 'YOUR_PROJECT_API_KEY'}' \\
  -d '{"dataset_name": "orders", "role": "candidate",
       "rows": [{"order_id": 1, "amount": 22.5, "status": "paid"}]}'`
  return <div className="card prose">
    <h2>Send rows from any service</h2>
    <p>Any language that can make an HTTP request can run a check. The response is the same JSON report the CLI writes with <code>--json</code>.</p>
    <CodeBlock code={curl} label="Copy command" />
    <ul>
      <li><code>"role": "baseline"</code> replaces the baseline; <code>"candidate"</code> (the default) runs a check.</li>
      <li>Up to 50,000 rows per request. For bigger files, upload them on the Checks tab or use the CLI / GitHub Action.</li>
      <li>The API key was shown once, when the project was created. Only its hash is stored.</li>
    </ul>
    <a className="link" href={`${API_ORIGIN}/docs`} target="_blank" rel="noreferrer">Interactive API docs <Icon name="external" size={13} /></a>
  </div>
}
