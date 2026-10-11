import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, fmtDate, plural, runTone, timeAgo, TONE_LABEL,
  type Issue, type Overview, type ProjectDetail, type Run, type Severity, type RulesDraft, type ReviewStatus, type ColumnChart } from './api'
import type { Route } from './App'
import { BeforeAfter, NullRates } from './charts'
import { ruleName } from './flow'
import { SourceNote, SourcesTab, UnifiedNote } from './sources'
import { Icon } from './icons'
import { CodeBlock, copyText, Dropzone, Empty, Menu, Spinner, useAction, useNotify } from './ui'

export const TABS = ['sources', 'checks', 'settings'] as const
export type Tab = typeof TABS[number]
const TAB_LABEL: Record<Tab, string> = { sources: 'Sources', checks: 'Results', settings: 'Settings' }
const FAIL_ON: Record<string, string> = {
  critical: 'Fails on critical issues', warning: 'Fails on warnings or worse', info: 'Fails on any issue', never: 'Never fails',
}

type Props = { route: Route & { id: string }; go: (r: Route) => void; onChanged: () => Promise<unknown>; ai: boolean }

export function ProjectView({ route, go, onChanged, ai }: Props) {
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
    try { await api.remove(detail.id); await onChanged(); go({ tab: 'sources' }); notify.ok(`Deleted ${detail.name}`) } catch (e) { notify.fail(e) }
  }

  return <div className="project">
    <header className="project-head">
      <div>
        <h1>{detail.name}</h1>
        <p className="status-line">
          {!detail.has_baseline ? <><i className="dot" /> Waiting for a standard: pick a source on the Sources tab</>
            : latest ? <><i className={`dot ${latest.status}`} /> {TONE_LABEL[runTone(latest)]} · last checked {timeAgo(latest.created_at)}</>
            : <><i className="dot" /> Ready for its first check</>}
        </p>
      </div>
      <Menu label="Project actions" items={[{ label: 'Delete project', onClick: remove, danger: true }]} />
    </header>

    <nav className="tabs" role="tablist">
      {TABS.map(t => <a key={t} role="tab" aria-selected={route.tab === t} href={`#/p/${detail.id}/${t}`} className={route.tab === t ? 'active' : ''}>
        {TAB_LABEL[t]}{t === 'checks' && detail.runs.length > 0 && <span className="count">{detail.runs.length}</span>}
        {t === 'settings' && dirty && <span className="unsaved" title="Unsaved changes" />}
      </a>)}
    </nav>

    {route.tab === 'checks' && <ChecksTab detail={detail} runId={route.run} select={run => go({ ...route, run })} changed={changed} ai={ai} />}
    {route.tab === 'sources' && <SourcesTab detail={detail} changed={changed} ai={ai} openRun={run => go({ id: detail.id, tab: 'checks', run })} />}
    {route.tab === 'settings' && <>
      <h2 className="section-title">Standard format</h2>
      <BaselineTab detail={detail} changed={changed} />
      <h2 className="section-title">Rules</h2>
      <RulesTab detail={detail} draft={draft} setDraft={setDraft} dirty={dirty} changed={changed} ai={ai} />
    </>}
  </div>
}

/* ---------- checks ---------- */

/** Results: every check (uploads, pulls, unifies) with its report and AI overview. Data comes in on Sources. */
function ChecksTab({ detail, runId, select, changed, ai }: { detail: ProjectDetail; runId?: string; select: (id?: string) => void; changed: () => Promise<unknown>; ai: boolean }) {
  const viewed = detail.runs.find(r => r.id === runId) || detail.runs[0]
  if (!viewed) return <Empty icon="activity" title="No results yet">
    <span>{detail.has_baseline ? 'Pull a source, unify your sources, or quick-check a file. Every result appears here.'
      : 'Start on Sources: connect your data and pick the standard format.'}</span>
    <a className="button primary" href={`#/p/${detail.id}/sources`}>Go to Sources</a>
  </Empty>
  return <div className="checks-layout">
    <RunHistory runs={detail.runs} selected={viewed.id} select={select} />
    <RunReport key={viewed.id} run={viewed} projectId={detail.id} failOn={detail.fail_on} ai={ai} changed={changed} />
  </div>
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
        <span><b>{r.report.data_file || 'Upload'}</b><small>{fmtDate(r.created_at)}{r.issue_count > 0 && r.review.status !== 'open' && ` · ${REVIEW[r.review.status]}`}</small></span>
        <em className={runTone(r)}>{r.issue_count || '✓'}</em>
      </button></li>)}
    </ul>
  </aside>
}

function RunReport({ run, projectId, failOn, ai, changed }: { run: Run; projectId: string; failOn: string; ai: boolean; changed: () => Promise<unknown> }) {
  const notify = useNotify()
  const { report } = run
  const tone = runTone(run)
  const blocks = useMemo(() => blocking(run, failOn), [run, failOn])
  const [rule, setRule] = useState('')
  const [query, setQuery] = useState('')
  const narrow = (next: string) => {
    setRule(next); setQuery('')
    document.getElementById('issues')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return <section className="card report">
    <div className={`verdict ${tone}`}>
      <ToneIcon tone={tone} big />
      <div>
        <h2>{tone === 'failed' ? 'This data would break things' : tone === 'issues' ? 'Passed, nothing blocking' : 'Everything looks normal'}</h2>
        <p>{report.data_file} · {fmtDate(run.created_at)} · {report.rows.current.toLocaleString()} rows{run.created_by && ` · checked by ${run.created_by}`}</p>
      </div>
      {report.markdown && <button className="button ghost" onClick={() => copyText(report.markdown!, notify, 'Markdown report copied')}>
        <Icon name="copy" size={14} /> Copy report</button>}
    </div>

    {report.issues.length > 0 && <OverviewCard run={run} projectId={projectId} blocks={blocks} ai={ai} changed={changed} narrow={narrow} />}
    {report.issues.length > 0 && <ReviewBar run={run} projectId={projectId} changed={changed} />}
    {report.source && <SourceNote projectId={projectId} run={run} />}
    {report.unified && <UnifiedNote projectId={projectId} run={run} changed={changed} />}
    {report.issues.length > 0 && <IssueList issues={report.issues} charts={report.charts} blocks={blocks}
      rule={rule} setRule={setRule} query={query} setQuery={setQuery} />}
    {report.ai_note && <p className="note"><Icon name="sparkle" size={13} /> {report.ai_note}</p>}
    {report.passed_checks.length > 0 && <p className="passed"><Icon name="check" size={14} /> Passed: {report.passed_checks.map(ruleName).join(', ')}</p>}
  </section>
}

const PAGE = 25
const RANK: Record<Severity, number> = { critical: 0, warning: 1, info: 2 }
const FAIL_RANK: Record<string, number> = { critical: 0, warning: 1, info: 2, never: -1 }

/** Whether an issue is one of the reasons this check failed, under the project's fail_on setting. */
const blocking = (run: Run, failOn: string) => (issue: Issue) =>
  run.status === 'failed' && RANK[issue.severity] <= (FAIL_RANK[failOn] ?? 0)

/** How many issues fall under each key (a rule, or each of an issue's columns), most first. */
function tally(issues: Issue[], keys: (issue: Issue) => string[]) {
  const counts = new Map<string, number>()
  issues.forEach(i => keys(i).forEach(k => counts.set(k, (counts.get(k) || 0) + 1)))
  return [...counts].sort((a, b) => b[1] - a[1])
}

const overviewRequests = new Map<string, Promise<Overview>>() // one request per run, even when effects run twice

/** What's going on at a glance: an AI summary (written once per run) over counts by rule. */
function OverviewCard({ run, projectId, blocks, ai, changed, narrow }: { run: Run; projectId: string; blocks: (issue: Issue) => boolean
  ai: boolean; changed: () => Promise<unknown>; narrow: (rule: string) => void }) {
  const { issues } = run.report
  const [overview, setOverview] = useState(run.report.overview || null)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    if (overview || !ai) return
    let live = true
    const key = `${run.id}:${attempt}`
    if (!overviewRequests.has(key)) overviewRequests.set(key, api.overview(projectId, run.id))
    overviewRequests.get(key)!.then(o => { if (live) { setOverview(o); changed() } }, e => { if (live) setError((e as Error).message) })
    return () => { live = false }
  }, [run.id, projectId, ai, attempt, overview])

  const rules = tally(issues, i => [i.check])
  const columns = tally(issues, i => i.columns)
  const blocked = issues.filter(blocks).length
  const glance = `${plural(issues.length, 'issue')}${blocked ? `, ${blocked} blocking` : ''}. Mostly ${ruleName(rules[0][0])} (${rules[0][1]})`
    + (rules[1] ? ` and ${ruleName(rules[1][0])} (${rules[1][1]})` : '') + (columns.length ? `, mainly in ${columns.slice(0, 3).map(([c]) => c).join(', ')}.` : '.')

  return <section className="overview" aria-label="Overview">
    <header>
      <span className="overview-icon"><Icon name="sparkle" size={15} /></span>
      <b>Overview</b>
      {overview && <small className="muted">AI summary from statistics only; check before acting</small>}
    </header>
    {overview ? <p className="overview-summary">{overview.summary}</p>
      : ai && !error ? <p className="muted"><Spinner text="Writing a summary…" /></p>
      : <p className="overview-summary">{glance}</p>}
    {error && <p className="muted small">AI summary unavailable: {error}{' '}
      <button className="link" onClick={() => { setError(null); setAttempt(a => a + 1) }}>Try again</button></p>}
    {overview && overview.focus.length > 0 && <ol className="overview-focus">{overview.focus.map((f, i) =>
      <li key={i}><b>{f.title}</b><span>{f.detail}</span></li>)}</ol>}
    {issues.length > 3 && <div className="overview-facts">
      <div><small>By rule</small>{rules.slice(0, 5).map(([r, n]) =>
        <button key={r} className="chip-btn" onClick={() => narrow(r)}>{ruleName(r)} <b>{n}</b></button>)}</div>
    </div>}
  </section>
}

/** Every issue as one line, blocking ones first; search, filter by rule, page through them, and open one at a time. */
function IssueList({ issues, charts, blocks, rule, setRule, query, setQuery }: { issues: Issue[]; charts?: Record<string, ColumnChart>
  blocks: (issue: Issue) => boolean; rule: string; setRule: (r: string) => void; query: string; setQuery: (q: string) => void }) {
  const [onlyBlocking, setOnlyBlocking] = useState(false)
  const [limit, setLimit] = useState(PAGE)
  const [opened, setOpened] = useState<number | null>(null)
  useEffect(() => { setLimit(PAGE); setOpened(null) }, [rule, query, onlyBlocking])
  const sorted = useMemo(() => issues.map((issue, i) => ({ issue, i, blocking: blocks(issue) }))
    .sort((a, b) => Number(b.blocking) - Number(a.blocking) || RANK[a.issue.severity] - RANK[b.issue.severity]), [issues, blocks])
  const blocked = sorted.filter(s => s.blocking).length
  const rules = useMemo(() => tally(issues, i => [i.check]), [issues])
  const q = query.trim().toLowerCase()
  const matches = sorted.filter(({ issue, blocking }) => (!onlyBlocking || blocking) && (!rule || issue.check === rule)
    && (!q || `${issue.title} ${issue.columns.join(' ')} ${issue.measured_summary}`.toLowerCase().includes(q)))
  const refine = (change: () => void) => { change(); setLimit(PAGE); setOpened(null) }

  return <section className="issue-list" id="issues" aria-label="Issues">
    <div className="issues-toolbar">
      <div className="issue-count">
        <b>{plural(issues.length, 'issue')}</b>
        {blocked > 0 && <span className="blocking-count"><i className="sev blocking" /> {blocked} blocking</span>}
        {blocked > 0 && blocked < issues.length && <label className="toggle inline">
          <input type="checkbox" checked={onlyBlocking} onChange={e => setOnlyBlocking(e.target.checked)} /> Only blocking</label>}
      </div>
      {(rules.length > 1 || issues.length > 10) && <div className="issue-find">
        {rules.length > 1 && <select className="search" value={rule} onChange={e => refine(() => setRule(e.target.value))} aria-label="Filter by rule">
          <option value="">All rules</option>
          {rules.map(([r, n]) => <option key={r} value={r}>{ruleName(r)} ({n})</option>)}
        </select>}
        {issues.length > 10 && <input className="search" value={query} onChange={e => refine(() => setQuery(e.target.value))}
          placeholder="Search issues or columns…" aria-label="Search issues" />}
      </div>}
    </div>

    {matches.length ? <ul className="issue-rows">{matches.slice(0, limit).map(({ issue, i, blocking }) =>
      <IssueRow key={i} issue={issue} blocking={blocking} charts={charts} open={opened === i} toggle={() => setOpened(o => o === i ? null : i)} />)}</ul>
      : <p className="muted pad">No issues match these filters.</p>}
    {matches.length > limit && <div className="issue-more">
      <span className="muted">Showing {limit} of {matches.length}</span>
      <button className="button ghost" onClick={() => setLimit(l => l + 100)}>Show {Math.min(100, matches.length - limit)} more</button>
    </div>}
  </section>
}

function IssueRow({ issue, blocking, charts, open, toggle }:
  { issue: Issue; blocking: boolean; charts?: Record<string, ColumnChart>; open: boolean; toggle: () => void }) {
  const measured = Object.entries(issue.measured || {})
  const tone = blocking ? 'blocking' : 'minor'
  return <li className={`issue-row ${tone} ${open ? 'open' : ''}`}>
    <button className="issue-line" aria-expanded={open} onClick={toggle}>
      <i className={`sev ${tone}`} title={blocking ? 'Blocking: one of the reasons this check failed' : 'Not blocking'} />
      <b>{issue.title}</b>
      <span className="issue-meta">{issue.measured_summary}</span>
      <Icon name="chevronDown" size={15} className={`issue-chevron ${open ? 'open' : ''}`} />
    </button>

    {open && <div className="issue-body">
      <p className="issue-summary">{blocking && <span className="pill critical">Blocking</span>} {issue.measured_summary}</p>
      {issue.check === 'ml_drift' && <DriftBars issue={issue} />}
      <IssueChart issue={issue} charts={charts} />
      {issue.suggested_explanation && <p className="cause"><b>Possible cause</b> {issue.suggested_explanation}</p>}
      {issue.ai_explanation && <p className="cause ai"><b><Icon name="sparkle" size={12} /> AI suggestion</b> {issue.ai_explanation}</p>}
      {issue.consumers.length > 0 && <p className="affects"><b>May affect</b>{issue.consumers.map(c =>
        <span key={c.name} className="chip" title={c.dataset_wide ? 'Reads this dataset' : `Uses ${c.columns.join(', ')}`}>{c.name}</span>)}</p>}
      {(measured.length > 0 || issue.examples.length > 0) && <details>
        <summary>Details</summary>
        <dl>{measured.map(([k, v]) => <div key={k}><dt>{k.replace(/_/g, ' ')}</dt><dd>{fmtValue(v)}</dd></div>)}</dl>
        {issue.examples.length > 0 && <p className="examples">Examples: {issue.examples.join(' · ')}</p>}
        <small className="muted">Check: <code>{issue.check}</code></small>
      </details>}
    </div>}
  </li>
}

/* ---------- shared issue status ---------- */

const REVIEW: Record<ReviewStatus, string> = { open: 'Open', resolved: 'Resolved' }

function ReviewBar({ run, projectId, changed }: { run: Run; projectId: string; changed: () => Promise<unknown> }) {
  const [busy, act] = useAction()
  const { status, by, at } = run.review
  return <div className="review">
    <div className="segmented" role="group" aria-label="Issue status">
      {(Object.keys(REVIEW) as ReviewStatus[]).map(s => <button key={s} className={`${s} ${status === s ? 'on' : ''}`} disabled={!!busy}
        aria-pressed={status === s} onClick={() => status !== s && act(s, async () => { await api.review(projectId, run.id, s); await changed() })}>
        {REVIEW[s]}</button>)}
    </div>
    <small className="muted">{by ? `${REVIEW[status]} by ${by} · ${timeAgo(at || undefined)}` : 'Nobody has picked this up yet'}</small>
  </div>
}

/** The before/after picture for issues where one exists: value ranges for shifts, empty rates for nulls. */
function IssueChart({ issue, charts }: { issue: Issue; charts?: Record<string, ColumnChart> }) {
  const column = issue.columns[0]
  const chart = column ? charts?.[column] : undefined
  if (!chart) return null
  if ((issue.check === 'distribution' || issue.check === 'ml_drift') && chart.hist) return <BeforeAfter column={column} chart={chart.hist} />
  if (issue.check === 'null_rate') return <NullRates column={column} rates={chart.null_rate} />
  return null
}

/** The drift model's column drivers, parsed from "amount (90%), quantity (9%)". */
function DriftBars({ issue }: { issue: Issue }) {
  const drivers = [...String(issue.measured.top_drivers || '').matchAll(/([^,]+?) \((\d+)%\)/g)].map(m => [m[1].trim(), Number(m[2])] as const)
  const auc = Number(issue.measured.drift_auc)
  return <div className="drift">
    <div className="gauge" title="0.5 = the model can't tell the files apart, 1.0 = it always can">
      <small>Drift score</small><b>{auc.toFixed(2)}</b>
      <span className="track"><i style={{ width: `${Math.max(0, (auc - 0.5) / 0.5) * 100}%` }} /></span>
      <small className="muted">0.5 same · 1.0 different</small>
    </div>
    <div className="drivers"><small>What gave it away</small>
      {drivers.map(([col, pct]) => <div key={col} className="driver"><code>{col}</code><span className="track"><i style={{ width: `${pct}%` }} /></span><b>{pct}%</b></div>)}
    </div>
  </div>
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
  if (!b) return <Empty icon="database" title="No standard yet"><span>The standard is the format every source is converted to.</span><a className="button primary" href={`#/p/${detail.id}/sources`}>Pick a source</a></Empty>
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
      <h2>Replace the standard with a file</h2>
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
  ['ml_drift', 'A model learns to tell new rows from baseline rows; max_auc is the limit (0.5 = same)'],
  ['consumers', 'Dashboards, models or services and the columns they read'],
]

function RulesTab({ detail, draft, setDraft, dirty, changed, ai }:
  { detail: ProjectDetail; draft: string; setDraft: (s: string) => void; dirty: boolean; changed: () => Promise<unknown>; ai: boolean }) {
  const notify = useNotify()
  const [busy, run] = useAction()
  const [suggested, setSuggested] = useState('')
  const [ask, setAsk] = useState('')
  const [proposal, setProposal] = useState<RulesDraft | null>(null)
  const save = () => run('save', async () => { await api.saveRules(detail.id, draft); await changed(); notify.ok('Rules saved') })

  const drafting = () => run('draft', async () => setProposal(await api.draftRules(detail.id, ask, draft)))

  return <div className="rules-layout">
    <div className="card">
      <form className="ask" onSubmit={e => { e.preventDefault(); if (ask.trim().length > 2) drafting() }}>
        <label htmlFor="ask"><Icon name="sparkle" size={14} /> Describe a change in plain English</label>
        <div className="row">
          <input id="ask" value={ask} onChange={e => setAsk(e.target.value)} disabled={!ai}
            placeholder={ai ? 'e.g. order_id must be unique, and alert if amount shifts a lot' : 'Set ANTHROPIC_API_KEY on the server to use this'} />
          <button className="button primary" disabled={!ai || ask.trim().length < 3 || !!busy}>{busy === 'draft' ? <Spinner text="Drafting…" /> : 'Draft'}</button>
        </div>
      </form>
      {proposal && <div className="proposal">
        <b>{proposal.summary}</b>
        {proposal.not_supported.map(n => <p key={n} className="muted"><Icon name="info" size={13} /> Not possible as a rule: {n}</p>)}
        <details><summary>Preview the full file</summary><CodeBlock code={proposal.config_yaml} /></details>
        <div className="actions">
          <button className="button primary" onClick={() => { setDraft(proposal.config_yaml); setProposal(null); setAsk('') }}>Put in editor</button>
          <button className="button ghost" onClick={() => setProposal(null)}>Discard</button>
        </div>
      </div>}
      <div className="card-head">
        <h2>datapulse.yml</h2>
        <span className="muted">{FAIL_ON[detail.fail_on]} · {dirty ? 'Unsaved changes' : 'Saved'}</span>
      </div>
      <textarea className="editor" value={draft} onChange={e => setDraft(e.target.value)} spellCheck={false} aria-label="Rules (YAML)"
        onKeyDown={e => { if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); if (dirty) save() } }} />
      <div className="actions">
        <button className="button primary" onClick={save} disabled={!dirty || !!busy}>{busy === 'save' ? <Spinner text="Saving…" /> : 'Save rules'}</button>
        <button className="button ghost" onClick={() => setDraft(detail.config_yaml)} disabled={!dirty}>Discard changes</button>
        <span className="muted hint">Ctrl+S to save</span>
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
