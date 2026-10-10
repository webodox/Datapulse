import { useEffect, useMemo, useState } from 'react'
import { api, plural, runTone, timeAgo, type ColumnSide, type ComparedColumn, type Comparison, type ProjectDetail,
  type Run, type Severity, type Source, type Step, type Tone } from './api'
import { Icon, type IconName } from './icons'
import { Spinner } from './ui'

const pct = (p: number | null | undefined) => p == null ? '—' : `${(p * 100).toFixed(p > 0 && p < 0.1 ? 1 : 0)}%`
const num = (v: number | null | undefined) => v == null ? '—' : Math.abs(v) >= 1000
  ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : v.toLocaleString(undefined, { maximumFractionDigits: 2 })
const countsOf = (run: Run): Record<Severity, number> => run.report.counts ?? {
  critical: run.report.issues.filter(i => i.severity === 'critical').length,
  warning: run.report.issues.filter(i => i.severity === 'warning').length,
  info: run.report.issues.filter(i => i.severity === 'info').length,
}
const RULES: Record<string, string> = {
  schema: 'columns & types', row_count: 'row count', null_rate: 'empty values', categorical: 'allowed values',
  duplicates: 'duplicates', distribution: 'value ranges', ml_drift: 'learned drift',
}
const ruleName = (check: string) => RULES[check] ?? check.replace(/_/g, ' ')

/** The most recent check of one source, from the project's recent runs. */
export const lastCheckOf = (detail: ProjectDetail, source: Source) => detail.runs.find(r => r.report.source?.id === source.id)

/* ---------- health cards ---------- */

export function HealthCards({ detail, sources }: { detail: ProjectDetail; sources: Source[] }) {
  const unified = detail.runs.find(r => r.report.unified)
  const latest = detail.runs[0]
  const recent = detail.runs.slice(0, 10)
  const passing = recent.filter(r => r.status === 'passed').length
  const todo = sources.filter(s => s.status === 'needs_mapping').length
  const failing = sources.filter(s => { const run = lastCheckOf(detail, s); return run && run.status === 'failed' }).length
  const counts = latest ? countsOf(latest) : null
  const cards: Array<{ label: string; value: string; note: string; tone: Tone | 'none'; icon: IconName }> = [
    { label: 'Sources', value: String(sources.length), icon: 'plug',
      tone: !sources.length ? 'none' : todo || failing ? 'issues' : 'clean',
      note: !sources.length ? 'None connected yet' : [`${sources.length - todo} mapped`, todo && `${todo} need mapping`, failing && `${failing} failing`].filter(Boolean).join(' · ') },
    { label: 'Unified table', value: unified ? unified.report.rows.current.toLocaleString() : '—', icon: 'database',
      tone: unified ? runTone(unified) : 'none',
      note: unified ? `rows · ${timeAgo(unified.created_at)} · ${unified.issue_count ? plural(unified.issue_count, 'issue') : 'all clear'}` : 'Not unified yet' },
    { label: 'Checks passing', value: recent.length ? `${passing}/${recent.length}` : '—', icon: 'activity',
      tone: !recent.length ? 'none' : passing === recent.length ? 'clean' : passing >= recent.length / 2 ? 'issues' : 'failed',
      note: recent.length ? `of the last ${plural(recent.length, 'check')}` : 'No checks yet' },
    { label: 'Latest issues', value: counts ? String(counts.critical + counts.warning + counts.info) : '—', icon: 'flag',
      tone: !latest ? 'none' : runTone(latest),
      note: counts ? `${counts.critical} critical · ${counts.warning} warning${latest ? ` · ${timeAgo(latest.created_at)}` : ''}` : 'No checks yet' },
  ]
  return <div className="health">{cards.map(c => <div key={c.label} className={`card health-card ${c.tone}`}>
    <span className="health-icon"><Icon name={c.icon} size={16} /></span>
    <small>{c.label}</small>
    <b>{c.value}</b>
    <span className="muted">{c.note}</span>
  </div>)}</div>
}

/* ---------- sources → standard → unified flow ---------- */

const LANE = 64, GAP = 10

export function FlowDiagram({ detail, sources, icon, openRun, unify, unifying, canUnify }:
  { detail: ProjectDetail; sources: Source[]; icon: (kind: string) => IconName; openRun: (run?: string) => void
    unify: () => void; unifying: boolean; canUnify: boolean }) {
  const standard = sources.find(s => s.status === 'standard')
  const unified = detail.runs.find(r => r.report.unified)
  const height = Math.max(LANE, sources.length * (LANE + GAP) - GAP)
  const W = 120
  const lane = (s: Source) => {
    const run = lastCheckOf(detail, s)
    const state = s.status === 'needs_mapping' ? 'todo' : run?.status === 'failed' ? 'bad' : s.status === 'standard' ? 'std' : 'ok'
    const label = s.status === 'standard' ? 'Standard' : s.status === 'needs_mapping' ? 'Needs mapping'
      : s.recipe.length ? plural(s.recipe.length, 'step') : 'Same format'
    return { run, state, label }
  }
  return <section className="card flow" aria-label="How your sources become one table">
    <div className="card-head"><h3>Sources → unified table</h3>
      <small className="muted">Each source is converted to the standard, then combined and checked.</small></div>
    {!sources.length ? <p className="muted">Add a source to see it here.</p> : <div className="flow-grid">
      <ol className="flow-lanes" style={{ gap: GAP }}>{sources.map(s => {
        const { run, state, label } = lane(s)
        return <li key={s.id} style={{ height: LANE }}>
          <a className={`flow-node ${state}`} href={`#source-${s.id}`}
            onClick={e => { e.preventDefault(); document.getElementById(`source-${s.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }) }}>
            <span className="source-icon"><Icon name={icon(s.kind)} size={16} /></span>
            <span className="flow-text"><b>{s.name}</b>
              <small>{s.type}{run ? ` · ${run.report.rows.current.toLocaleString()} rows` : ''}</small></span>
            <span className={`flow-chip ${state}`}>{label}</span>
            {run && <span className={`dot ${run.status}`} title={`Last check ${run.status}: ${plural(run.issue_count, 'issue')}`} />}
          </a>
        </li>
      })}</ol>
      <svg className="flow-links" viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none" style={{ height }} aria-hidden="true">
        {sources.map((s, i) => {
          const y = i * (LANE + GAP) + LANE / 2, mid = height / 2
          return <path key={s.id} className={lane(s).state} d={`M0,${y} C${W / 2},${y} ${W / 2},${mid} ${W},${mid}`} />
        })}
      </svg>
      <div className={`flow-target ${unified ? runTone(unified) : ''}`} style={{ minHeight: Math.min(height, 220) }}>
        <small className="step">Standard format</small>
        <b>{standard ? standard.name : 'Not chosen yet'}</b>
        <span className="muted">{detail.baseline ? `${plural(Object.keys(detail.baseline.columns).length, 'column')} · v${detail.baseline.baseline_version}` : 'Use one source as the standard'}</span>
        <hr />
        <small className="step">Unified table</small>
        {unified ? <>
          <b>{unified.report.rows.current.toLocaleString()} rows</b>
          <span className={`flow-status ${runTone(unified)}`}>{unified.issue_count ? plural(unified.issue_count, 'issue') : 'All checks passed'} · {timeAgo(unified.created_at)}</span>
          <div className="actions">
            <button className="button ghost small-btn" onClick={() => openRun(unified.id)}>Report</button>
            <a className="button ghost small-btn" href={api.runDataUrl(detail.id, unified.id)}><Icon name="download" size={13} /> Data</a>
          </div>
        </> : <span className="muted">Not unified yet</span>}
        {detail.has_baseline && <button className="button primary small-btn" disabled={!canUnify || unifying} onClick={unify}>
          {unifying ? <Spinner text="Unifying…" /> : unified ? 'Unify again' : 'Unify now'}</button>}
      </div>
    </div>}
  </section>
}

/* ---------- their column → our column ---------- */

const STATUS_LABEL: Record<ComparedColumn['status'], string> = {
  same: 'Same', renamed: 'Renamed', converted: 'Converted', flagged: 'Flagged by a rule', missing: 'Missing',
}
const STEP_LABEL: Record<string, string> = {
  rename_column: 'rename', scale_column: 'scale', convert_to_number: 'to number', standardize_categories: 'fix spelling',
  fill_missing: 'fill empty',
}

/** Kinds that are compatible for the schema check (whole-number floats count as integers). */
const sameKind = (a?: string, b?: string) => a === b || (a === 'float' && b === 'integer')

/** Values differ from the standard though no rule flags them, e.g. ids offset by 50,000. */
function looksDifferent(std: ColumnSide, after: ColumnSide | null) {
  if (!after || std.mean == null || after.mean == null || std.min == null || std.max == null || after.min == null || after.max == null) return false
  const overlap = Math.min(std.max, after.max) - Math.max(std.min, after.min)
  const span = Math.max(std.max - std.min, 1e-9)
  return overlap < 0.25 * span || Math.abs(after.mean - std.mean) > 0.5 * Math.max(Math.abs(std.mean), span)
}

function RangeBar({ std, after }: { std: ColumnSide; after: ColumnSide }) {
  if (std.min == null || std.max == null || after.min == null || after.max == null) return null
  const lo = Math.min(std.min, after.min), hi = Math.max(std.max, after.max), span = hi - lo || 1
  const at = (v: number) => `${((v - lo) / span) * 100}%`
  const width = (a: number, b: number) => `${Math.max(1.5, ((b - a) / span) * 100)}%`
  return <div className="range" title={`Standard ${num(std.min)} – ${num(std.max)}, mapped ${num(after.min)} – ${num(after.max)}`}>
    <span className="range-row"><i className="base" style={{ left: at(std.min), width: width(std.min, std.max) }} />
      {std.mean != null && <em style={{ left: at(std.mean) }} />}</span>
    <span className="range-row"><i className="new" style={{ left: at(after.min), width: width(after.min, after.max) }} />
      {after.mean != null && <em style={{ left: at(after.mean) }} />}</span>
    <small><span>{num(lo)}</span><span>{num(hi)}</span></small>
  </div>
}

function Samples({ values, compare }: { values?: string[]; compare?: string[] }) {
  if (!values?.length) return null
  return <span className="samples">{values.map((v, i) => compare && compare[i] !== undefined && compare[i] !== v
    ? <mark key={i}>{v}</mark> : <span key={i}>{v}</span>)}</span>
}

function CompareRow({ c }: { c: ComparedColumn }) {
  const different = c.status !== 'missing' && looksDifferent(c.standard, c.after)
  const typeChanged = c.after && !sameKind(c.standard.kind, c.after.kind)
  const nullUp = c.after && c.after.null_rate != null && c.standard.null_rate != null && c.after.null_rate > c.standard.null_rate + 0.001
  const tone = c.status === 'flagged' ? (c.issues.some(i => i.severity === 'critical') ? 'critical' : 'warning') : c.status
  return <div className={`cmp-row ${tone}`}>
    <div className="cmp-cell theirs">
      {c.source ? <><code>{c.source}</code><small className="muted">{c.before?.kind}</small>
        <Samples values={c.before?.samples} /></> : <em className="bad">No column from this source</em>}
    </div>
    <div className="cmp-arrow">
      <span className={`pill cmp-status ${tone}`}>{STATUS_LABEL[c.status]}</span>
      {c.steps.length > 0 && <span className="cmp-steps">{c.steps.map((s, i) =>
        <span key={i} className={`step-chip ${s.operation}`} title={s.effect}>{STEP_LABEL[s.operation] || s.operation.replace(/_/g, ' ')}</span>)}</span>}
      <Icon name="arrow" size={18} />
    </div>
    <div className="cmp-cell ours">
      <code>{c.name}</code>
      <small className={typeChanged ? 'hl' : 'muted'}>{c.after ? c.after.kind : c.standard.kind}{typeChanged && ` (standard: ${c.standard.kind})`}</small>
      <Samples values={c.after?.samples} compare={c.steps.some(s => s.operation !== 'rename_column') ? c.before?.samples : undefined} />
    </div>
    <div className="cmp-cell vs">
      {c.after && c.after.min != null && c.standard.min != null ? <RangeBar std={c.standard} after={c.after} />
        : c.standard.top.length ? <span className="tops">{(c.after?.top.length ? c.after.top : c.standard.top).map(v =>
          <span key={v} className={c.after && !c.standard.top.includes(v) ? 'chip hl' : 'chip'}>{v}</span>)}</span> : null}
      <small className={nullUp ? 'hl' : 'muted'}>Empty: {pct(c.standard.null_rate)} → {c.after ? pct(c.after.null_rate) : 'missing'}</small>
      {different && <small className="hl"><Icon name="alert" size={12} /> Values look different from the standard; no rule flags this</small>}
    </div>
    {c.issues.length > 0 && <ul className="cmp-issues">{c.issues.map((i, n) =>
      <li key={n} className={i.severity} title={i.detail}><span className={`pill ${i.severity}`}>{i.severity}</span> {i.title}
        <small className="muted">rule: {ruleName(i.check)}</small></li>)}</ul>}
  </div>
}

export function MappingCompare({ projectId, source, operations }: { projectId: string; source: Source; operations?: Step[] }) {
  const [data, setData] = useState<Comparison | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [changesOnly, setChangesOnly] = useState(false)
  const key = JSON.stringify(operations ?? null)
  useEffect(() => {
    let live = true
    const timer = window.setTimeout(() => {
      api.compareSource(projectId, source.id, operations)
        .then(d => { if (live) { setData(d); setError(null) } }, e => { if (live) setError((e as Error).message) })
    }, 250)
    return () => { live = false; window.clearTimeout(timer) }
  }, [projectId, source.id, key]) // `key` stands in for `operations`, which is a new array on every render
  const rows = useMemo(() => data ? data.columns.filter(c => !changesOnly || c.status !== 'same') : [], [data, changesOnly])

  if (error) return <p className="test-result bad"><Icon name="alert" size={14} /> {error}</p>
  if (!data) return <div className="loading"><span className="spinner" /> Comparing columns…</div>
  const counts = data.columns.reduce<Record<string, number>>((acc, c) => ({ ...acc, [c.status]: (acc[c.status] || 0) + 1 }), {})
  return <section className="compare" aria-label={`${source.name} columns compared with the standard`}>
    <div className="compare-head">
      <div className="fix-score">
        <div><small>Issues vs the standard</small><b>{data.before.total} <span>→</span> {data.after.total}</b></div>
        <div><small>Rows</small><b>{data.rows.before.toLocaleString()} <span>→</span> {data.rows.after.toLocaleString()}</b></div>
      </div>
      <div className="cmp-legend">{(['same', 'renamed', 'converted', 'flagged', 'missing'] as const).filter(s => counts[s]).map(s =>
        <span key={s} className={`pill cmp-status ${s === 'flagged' ? 'warning' : s}`}>{counts[s]} {STATUS_LABEL[s].toLowerCase()}</span>)}</div>
      <label className="toggle"><input type="checkbox" checked={changesOnly} onChange={e => setChangesOnly(e.target.checked)} /> Only changes</label>
    </div>
    <div className="cmp-grid">
      <div className="cmp-row head"><span>Their column ({source.name})</span><span /><span>Our column (standard)</span><span>Compared with the standard</span></div>
      {rows.map(c => <CompareRow key={c.name} c={c} />)}
      {data.extra.map(e => <div key={e.name} className="cmp-row extra">
        <div className="cmp-cell theirs"><code>{e.name}</code><small className="muted">{e.after.kind}</small><Samples values={e.after.samples} /></div>
        <div className="cmp-arrow"><span className="pill cmp-status warning">Not in the standard</span><Icon name="arrow" size={18} /></div>
        <div className="cmp-cell ours"><em className="muted">Flagged as a new column. Map or drop it.</em></div>
        <div className="cmp-cell vs" />
      </div>)}
      {data.dropped.map(d => <div key={d.name} className="cmp-row dropped">
        <div className="cmp-cell theirs"><code>{d.name}</code></div>
        <div className="cmp-arrow"><span className="pill cmp-status">Dropped</span></div>
        <div className="cmp-cell ours"><em className="muted">{d.effect}</em></div>
        <div className="cmp-cell vs" />
      </div>)}
    </div>
    {data.table_steps.map(s => <p key={s} className="note"><Icon name="info" size={13} /> {s}</p>)}
    {data.table_issues.map((i, n) => <p key={n} className={`note ${i.severity === 'info' ? '' : 'warn'}`}><Icon name="alert" size={13} /> {i.title}: {i.detail} <small className="muted">(rule: {ruleName(i.check)})</small></p>)}
    {data.problems.map(p => <p key={p} className="note warn"><Icon name="alert" size={13} /> {p}</p>)}
    {!data.show_values && <p className="muted small"><Icon name="shield" size={13} /> Sample values are hidden by this project's rules (report.show_values).</p>}
  </section>
}
