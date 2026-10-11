import { useEffect, useMemo, useState } from 'react'
import { api, plural, type BadRow, type BadRows, type CellFlag, type FixStep, type Fix, type FlagSeverity, type RowFixPreview,
  type Run, type RowRule, type RowsResult } from './api'
import { Icon } from './icons'
import { Spinner, useAction, useNotify } from './ui'

const PAGE = 25
const RANK: Record<FlagSeverity, number> = { critical: 3, warning: 2, info: 1, standard: 0 }
const KIND: Record<string, string> = {
  empty: 'empty', unknown: 'unknown value', not_number: 'not a number', range: 'outside the standard range',
  duplicate: 'duplicate', required: 'required', unique: 'must be unique', number: 'not a number', min: 'below the minimum',
  max: 'above the maximum', allowed: 'not allowed', pattern: "doesn't match the pattern",
}
const STEP: Record<string, string> = {
  scale_column: 'Rescale', convert_to_number: 'Read as numbers', standardize_categories: 'Fix spelling',
  remove_duplicates: 'Remove duplicates', fill_missing: 'Fill empty values', drop_rows_missing: 'Remove rows with empty values',
  drop_rows_not_in: 'Remove rows with unknown values', drop_rows_outside_range: 'Remove rows outside the range',
  rename_column: 'Rename', drop_column: 'Remove column',
}
const REMOVES = new Set(['remove_duplicates', 'drop_rows_missing', 'drop_rows_not_in', 'drop_rows_outside_range'])
const SEV_LABEL: Record<FlagSeverity, string> = { critical: 'Breaks a critical rule', warning: 'Breaks a warning rule', info: 'Info', standard: 'Differs from the standard (allowed)' }

const worst = (flags: CellFlag[]) => flags.reduce<FlagSeverity>((w, f) => RANK[f.severity] > RANK[w] ? f.severity : w, 'standard')
const allFlags = (r: BadRow) => [...Object.values(r.cells).flat(), ...r.row_flags]
const ruleKey = (r: Pick<RowRule, 'rule' | 'column'>) => `${r.rule}:${r.column ?? ''}`
const stepKey = (s: FixStep) => `${s.operation}:${JSON.stringify(s.args)}`
const describeStep = (s: FixStep) => {
  const col = typeof s.args.column === 'string' ? s.args.column : Array.isArray(s.args.columns) && s.args.columns.length ? s.args.columns.join(', ') : ''
  const extra = s.operation === 'scale_column' ? ` × ${s.args.factor}` : ''
  return <>{STEP[s.operation] ?? s.operation.replace(/_/g, ' ')}{col && <> <code>{col}</code></>}{extra}</>
}

/** Rows that break the rules or don't match the standard, highlighted cell by cell, with fixes to preview and apply. */
export function BadRowsSection({ run, projectId, ai, changed, select }:
  { run: Run; projectId: string; ai: boolean; changed: () => Promise<unknown>; select: (id?: string) => void }) {
  const notify = useNotify()
  const [busy, act] = useAction()
  const [data, setData] = useState<RowsResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [steps, setSteps] = useState<FixStep[]>([])
  const [chosen, setChosen] = useState<Record<string, boolean>>({})
  const [preview, setPreview] = useState<RowFixPreview | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [filter, setFilter] = useState<string | null>(null)
  const [failingOnly, setFailingOnly] = useState(false)
  const [view, setView] = useState<'now' | 'after'>('now')
  const [limit, setLimit] = useState(PAGE)
  const [ask, setAsk] = useState('')
  const [agent, setAgent] = useState<Omit<Fix, 'file_name'> | null>(null)

  useEffect(() => {
    let live = true
    api.badRows(projectId, run.id).then(r => {
      if (!live) return
      setData(r); setSteps(r.suggestions)
      setChosen(Object.fromEntries(r.suggestions.map(s => [stepKey(s), !!s.default])))
    }, e => live && setError(e instanceof Error ? e.message : String(e)))
    return () => { live = false }
  }, [projectId, run.id])

  const picked = useMemo(() => steps.filter(s => chosen[stepKey(s)]), [steps, chosen])
  const pickedKey = JSON.stringify(picked.map(s => [s.operation, s.args]))
  useEffect(() => {
    if (!data || !picked.length) { setPreview(null); return }
    let live = true
    setPreviewing(true)
    const t = setTimeout(() => api.badRows(projectId, run.id, picked).then(r => live && setPreview(r.preview),
      e => live && notify.fail(e)).finally(() => live && setPreviewing(false)), 300)
    return () => { live = false; clearTimeout(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pickedKey, data, projectId, run.id])

  if (error) return <section className="badrows"><p className="note warn"><Icon name="alert" size={13} /> Bad rows unavailable: {error}</p></section>
  if (!data) return <section className="badrows"><Spinner text="Finding the rows that break the rules…" /></section>
  const before = data.rows
  if (!before.bad_rows) return <p className="passed"><Icon name="check" size={14} /> Every row follows the rules and matches the standard.</p>

  const shownData: BadRows = view === 'after' && preview ? preview.rows : before
  const removed = new Set(view === 'now' && preview ? preview.removed : [])
  const stillBad = view === 'now' && preview ? new Set(preview.rows.rows.map(r => r.row)) : null
  const afterComplete = !!preview && preview.rows.shown >= preview.rows.bad_rows
  const rows = shownData.rows.filter(r => (!failingOnly || allFlags(r).some(f => f.severity !== 'standard'))
    && (!filter || [...Object.entries(r.cells).flatMap(([c, fs]) => fs.map(f => `${f.rule}:${c}`)), ...r.row_flags.map(f => `${f.rule}:`)].includes(filter)))
  const hasSource = shownData.rows.some(r => r.source)

  const askAgent = () => act('agent', async () => {
    const result = await api.rowsAgent(projectId, run.id, ask)
    setAgent(result)
    const added = result.operations.map(op => ({ ...op, by_ai: true }))
    setSteps(prev => [...prev, ...added.filter(a => !prev.some(p => stepKey(p) === stepKey(a)))])
    setChosen(prev => ({ ...prev, ...Object.fromEntries(added.map(a => [stepKey(a), true])) }))
    notify.ok(result.operations.length ? `The agent suggested ${plural(result.operations.length, 'step')}; review them below` : 'The agent found nothing it could safely fix')
  })
  const apply = () => act('apply', async () => {
    const report = await api.fixRows(projectId, run.id, picked)
    await changed(); select(report.run_id)
    notify.ok(`Fixed copy checked: ${plural(report.issues.length, 'issue')} left`)
  })

  return <section className="badrows">
    <div className="card-head">
      <div>
        <h3><Icon name="alert" size={16} /> Bad rows</h3>
        <p className="muted"><b className="bad-count">{before.bad_rows.toLocaleString()}</b> of {before.total_rows.toLocaleString()} rows break a rule or don't match the standard
          {before.failing_rows > 0 && <> · <b className="bad-count">{before.failing_rows.toLocaleString()}</b> break a rule that fails this check</>}</p>
      </div>
      {preview && <div className="segmented">
        <button className={view === 'now' ? 'on' : ''} onClick={() => setView('now')}>Now</button>
        <button className={view === 'after' ? 'on' : ''} onClick={() => setView('after')}>After fixes</button>
      </div>}
    </div>

    <div className="rule-chips">
      {shownData.summary.map(s => <button key={ruleKey(s) + s.kind} className={`rule-chip ${s.severity} ${filter === ruleKey(s) ? 'on' : ''}`}
        onClick={() => { setFilter(filter === ruleKey(s) ? null : ruleKey(s)); setLimit(PAGE) }} title={SEV_LABEL[s.severity]}>
        {s.column ? <code>{s.column}</code> : 'Whole row'} {KIND[s.kind] ?? s.kind}<small>{s.label}</small><b>{s.rows.toLocaleString()}</b>
      </button>)}
      {!shownData.summary.length && <span className="muted">No bad rows left after these fixes.</span>}
    </div>
    <div className="badrows-bar">
      <span className="legend"><i className="lg critical" />Breaks a critical rule <i className="lg warning" />Breaks a warning rule <i className="lg standard" />Differs from the standard</span>
      <label className="toggle"><input type="checkbox" checked={failingOnly} onChange={e => setFailingOnly(e.target.checked)} /> Only rows that break a rule</label>
    </div>

    {rows.length > 0 && <div className="table-wrap"><table className="bad-table">
      <thead><tr><th>Row</th>{hasSource && <th>Source</th>}{shownData.columns.map(c => <th key={c}>{c}</th>)}<th>Why</th></tr></thead>
      <tbody>{rows.slice(0, limit).map(r => {
        const flags = allFlags(r)
        const fate = removed.has(r.row) ? 'removed' : stillBad && afterComplete && !stillBad.has(r.row) ? 'fixed' : ''
        return <tr key={r.row} className={`${worst(flags)} ${fate}`}>
          <td className="rownum">{r.row}{fate && <small className={`fate ${fate}`}>{fate === 'removed' ? 'removed by fix' : 'fixed'}</small>}</td>
          {hasSource && <td><span className="chip">{r.source}</span></td>}
          {shownData.columns.map(c => {
            const cell = r.cells[c]
            const value = r.values?.[c]
            return <td key={c} className={cell ? `flag ${worst(cell)}` : ''} title={cell?.map(f => f.reason).join('\n')}>
              {r.values ? value ?? <i className="empty">empty</i> : cell ? <Icon name="alert" size={12} /> : ''}
            </td>
          })}
          <td className="why">{flags.slice(0, 2).map((f, i) => <small key={i} className={f.severity}>{f.reason}</small>)}
            {flags.length > 2 && <small className="muted">+{flags.length - 2} more</small>}</td>
        </tr>
      })}</tbody>
    </table></div>}
    <p className="muted small">
      Showing {Math.min(limit, rows.length)} of {rows.length < shownData.shown ? `${rows.length} matching` : shownData.bad_rows.toLocaleString()} bad rows{shownData.shown < shownData.bad_rows && ' (the worst first)'}.
      {rows.length > limit && <> <button className="inline-link" onClick={() => setLimit(limit + PAGE)}>Show more</button></>}
      {!before.show_values && ' Values are hidden by this project\'s rules (report.show_values); hover a flagged cell for the reason.'}
    </p>

    <div className="fix-rows">
      <h4><Icon name="wrench" size={14} /> Fix these rows</h4>
      {steps.length === 0 && <p className="muted">No automatic fix for these rows. Fix them at the source, or ask the AI agent.</p>}
      <ul className="fix-steps">{steps.map(s => {
        const k = stepKey(s)
        return <li key={k} className={chosen[k] ? 'on' : ''}><label>
          <input type="checkbox" checked={!!chosen[k]} onChange={e => setChosen({ ...chosen, [k]: e.target.checked })} />
          <span><b>{describeStep(s)}</b><small>{s.reason}</small></span>
          {s.rows ? <em>{plural(s.rows, 'row')}</em> : null}
          {REMOVES.has(s.operation) && <em className="warn">removes rows</em>}
          {s.operation === 'fill_missing' && <em className="warn">fills in values</em>}
          {s.by_ai && <em className="ai"><Icon name="sparkle" size={11} /> AI</em>}
        </label></li>
      })}</ul>

      {picked.length > 0 && <div className="fix-score">
        {previewing && !preview ? <Spinner text="Previewing…" /> : preview && <>
          <div><small>Bad rows</small><b>{before.bad_rows.toLocaleString()} <span>→</span> {preview.rows.bad_rows.toLocaleString()}</b></div>
          <div><small>Issues</small><b>{preview.before.total} <span>→</span> {preview.after.total}</b></div>
          <div><small>Critical</small><b>{preview.before.critical} <span>→</span> {preview.after.critical}</b></div>
          <div><small>Rows removed</small><b>{preview.removed_rows.toLocaleString()}</b></div>
          {previewing && <Spinner text="Updating…" />}
        </>}
      </div>}
      {preview?.problems.map(p => <p key={p} className="note warn"><Icon name="alert" size={13} /> {p}</p>)}
      {preview && preview.remaining.length > 0 && <p className="muted small">Still open after these fixes: {preview.remaining.join(' · ')}</p>}

      <div className="agent-ask">
        <input value={ask} onChange={e => setAsk(e.target.value)} maxLength={1000} disabled={!ai || !!busy}
          placeholder={ai ? "Tell the agent what's wrong (optional), e.g. amounts arrived in cents" : 'AI is off. Set ANTHROPIC_API_KEY on the server to turn it on.'} />
        <button className="button outline" disabled={!ai || !!busy} onClick={askAgent}>
          {busy === 'agent' ? <Spinner text="The agent is working…" /> : <><Icon name="sparkle" size={14} /> Ask the AI agent</>}</button>
      </div>
      {agent && <div className="agent-result">
        <p><Icon name="sparkle" size={13} /> {agent.summary}</p>
        {agent.not_fixed.length > 0 && <ul className="notify">{agent.not_fixed.map((n, i) => <li key={i}><b>{n.issue}</b> {n.why}</li>)}</ul>}
        {agent.warnings.map((w, i) => <p key={i} className="note"><Icon name="info" size={13} /> {w}</p>)}
      </div>}

      <div className="actions">
        <button className="button primary" disabled={!picked.length || !!busy} onClick={apply}>
          {busy === 'apply' ? <Spinner text="Fixing and checking…" /> : <>Apply {picked.length} {picked.length === 1 ? 'fix' : 'fixes'} and check the copy</>}</button>
        <small className="muted">Makes a fixed copy and checks it. The original data is never changed.</small>
      </div>
    </div>
  </section>
}

/** On a fixed copy: where it came from and what was done. */
export function FixedNote({ run, select }: { run: Run; select: (id?: string) => void }) {
  const from = run.report.fixed_from
  if (!from) return null
  return <div className="note fixed-note">
    <Icon name="wrench" size={13} /> Fixed copy of <button className="inline-link" onClick={() => select(from.run_id)}>{from.data_file}</button>.
    {(run.report.fix_steps ?? []).length > 0 && <ul>{run.report.fix_steps!.map((s, i) => <li key={i}>{s}</li>)}</ul>}
    {(run.report.fix_problems ?? []).map(p => <p key={p} className="muted small">{p}</p>)}
  </div>
}
