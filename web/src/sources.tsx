import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { api, plural, timeAgo, type Mapping, type ProjectDetail, type Run, type Source, type SourceKind, type Step } from './api'
import { Icon } from './icons'
import { Empty, Modal, Spinner, useAction, useNotify } from './ui'

const STATUS: Record<Source['status'], { label: string; tone: string }> = {
  standard: { label: 'Standard', tone: 'clean' },
  mapped: { label: 'Mapped', tone: 'clean' },
  needs_mapping: { label: 'Needs mapping', tone: 'issues' },
}

let catalogCache: Promise<SourceKind[]> | null = null
function useCatalog() {
  const [kinds, setKinds] = useState<SourceKind[]>([])
  useEffect(() => {
    catalogCache ??= api.sourceCatalog().catch(e => { catalogCache = null; throw e })
    catalogCache.then(setKinds, () => {})
  }, [])
  return kinds
}
const labelOf = (kinds: SourceKind[], kind: string) => kinds.find(k => k.kind === kind)?.label || kind

/* ---------- the Sources tab ---------- */

export function SourcesTab({ detail, changed, ai, openRun }:
  { detail: ProjectDetail; changed: () => Promise<unknown>; ai: boolean; openRun: (run?: string) => void }) {
  const notify = useNotify()
  const kinds = useCatalog()
  const [list, setList] = useState<Source[] | null>(null)
  const [editing, setEditing] = useState<Source | 'new' | null>(null)
  const [busy, act] = useAction()
  const reload = useCallback(() => api.sources(detail.id).then(setList), [detail.id])
  useEffect(() => { reload().catch(notify.fail) }, [reload, notify])
  const refresh = () => Promise.all([reload(), changed()])

  if (!list) return <div className="loading"><span className="spinner" /> Loading sources…</div>
  const ready = list.filter(s => s.status !== 'needs_mapping')
  const unify = () => act('unify', async () => {
    const report = await api.unify(detail.id)
    await refresh(); openRun(report.run_id)
    notify.ok(`Unified ${plural(report.rows.current, 'row')}: ${report.issues.length ? plural(report.issues.length, 'issue') + ' to review' : 'all clear'}`)
  })

  const lastUnified = detail.runs.find(r => r.report.unified)
  const steps: Array<[string, boolean]> = [
    ['Connect your sources', list.length > 0],
    ['Pick the standard format', detail.has_baseline],
    ['Map the others to it', list.length > 1 && list.every(s => s.status !== 'needs_mapping')],
    ['Unify into one table', !!lastUnified],
  ]
  const next = steps.findIndex(([, done]) => !done)

  return <>
    <div className="card sources-intro">
      <div>
        <h2>Unify your data</h2>
        <ol className="progress">{steps.map(([label, done], i) =>
          <li key={label} className={done ? 'done' : i === next ? 'next' : ''}><span>{done ? <Icon name="check" size={12} /> : i + 1}</span>{label}</li>)}</ol>
      </div>
      <div className="actions">
        <button className={`button ${list.length ? 'outline' : 'primary'}`} onClick={() => setEditing('new')}><Icon name="plus" size={14} /> Add source</button>
        {detail.has_baseline && <button className="button primary" disabled={!ready.length || !!busy} onClick={unify}
          title={ready.length ? undefined : 'Map a source first'}>
          {busy === 'unify' ? <Spinner text="Unifying…" /> : <><Icon name="database" size={14} /> Unify {ready.length > 1 ? `all ${ready.length} sources` : 'sources'}</>}
        </button>}
      </div>
    </div>

    {lastUnified && <div className={`card unified-latest ${lastUnified.status}`}>
      <span className="source-icon"><Icon name="database" size={18} /></span>
      <div>
        <b>Unified table · {lastUnified.report.rows.current.toLocaleString()} rows from {plural(lastUnified.report.unified!.sources.filter(x => !x.error).length, 'source')}</b>
        <small className="muted">{timeAgo(lastUnified.created_at)} · {lastUnified.issue_count ? plural(lastUnified.issue_count, 'issue') + ' to review' : 'all checks passed'}</small>
      </div>
      <button className="button ghost" onClick={() => openRun(lastUnified.id)}>Open report</button>
      <a className="button outline" href={api.runDataUrl(detail.id, lastUnified.id)}><Icon name="download" size={14} /> Download</a>
    </div>}

    {list.length === 0 ? <Empty icon="plug" title="No sources yet">
      <span>PostgreSQL, MySQL, SQL Server, MongoDB, Google Sheets, file links and more.</span>
    </Empty> : <div className="source-list">
      {list.map(s => <SourceCard key={s.id} source={s} kinds={kinds} detail={detail} ai={ai}
        refresh={refresh} edit={() => setEditing(s)} openRun={openRun} />)}
    </div>}

    {editing && <SourceDialog projectId={detail.id} kinds={kinds} source={editing === 'new' ? null : editing}
      close={() => setEditing(null)} saved={() => { setEditing(null); reload() }} />}
  </>
}

function SourceCard({ source: s, kinds, detail, ai, refresh, edit, openRun }:
  { source: Source; kinds: SourceKind[]; detail: ProjectDetail; ai: boolean; refresh: () => Promise<unknown>; edit: () => void; openRun: (run?: string) => void }) {
  const notify = useNotify()
  const [busy, act] = useAction()
  const status = STATUS[s.status]
  const pull = (role: 'baseline' | 'candidate') => act(role, async () => {
    if (role === 'baseline' && detail.has_baseline && !window.confirm(`Make "${s.name}" the new standard? Other sources' recipes may need re-mapping.`)) return
    const report = await api.pullSource(detail.id, s.id, role)
    await refresh()
    if (role === 'baseline') notify.ok(`${s.name} is now the standard format`)
    else { openRun(report.run_id); notify.ok(report.status === 'failed' ? `${s.name}: ${plural(report.issues.length, 'issue')} to review` : `${s.name} passed`) }
  })
  const map = () => act('map', async () => { await api.mapSource(detail.id, s.id); await refresh() })
  const remove = () => {
    if (!window.confirm(`Remove the source "${s.name}"? Past checks are kept.`)) return
    act('remove', async () => { await api.removeSource(detail.id, s.id); await refresh() })
  }

  return <article className="card source">
    <header>
      <span className="source-icon"><Icon name={s.kind === 'link' ? 'external' : 'database'} size={18} /></span>
      <div>
        <h3>{s.name}</h3>
        <small className="muted">{labelOf(kinds, s.kind)}{s.last_pulled_at ? ` · pulled ${timeAgo(s.last_pulled_at)}` : ' · never pulled'}</small>
      </div>
      <span className={`pill verdict ${status.tone}`}>{status.label}{s.status === 'mapped' && s.recipe.length > 0 && ` · ${plural(s.recipe.length, 'step')}`}</span>
    </header>

    {s.proposal && <MappingReview proposal={s.proposal} projectId={detail.id} source={s} refresh={refresh} />}

    <div className="actions">
      {s.proposal ? null : !detail.has_baseline ? <button className="button primary" disabled={!!busy} onClick={() => pull('baseline')}>
        {busy === 'baseline' ? <Spinner text="Learning the format…" /> : 'Use as the standard'}</button>
        : s.status === 'needs_mapping' ? <button className="button primary ai" disabled={!!busy} onClick={map}>
          {busy === 'map' ? <Spinner text="Mapping…" /> : <><Icon name="sparkle" size={14} /> Map to the standard</>}</button>
        : <button className="button primary" disabled={!!busy} onClick={() => pull('candidate')}>
          {busy === 'candidate' ? <Spinner text="Pulling…" /> : 'Pull & check'}</button>}
      {s.last_run_id && <button className="button ghost" onClick={() => openRun(s.last_run_id!)}>Last check</button>}
      {s.status === 'mapped' && <button className="button ghost" disabled={!!busy} onClick={map}>{busy === 'map' ? <Spinner text="Mapping…" /> : 'Re-map'}</button>}
      {detail.has_baseline && s.status !== 'standard' && <button className="button ghost" disabled={!!busy} onClick={() => pull('baseline')}>Make standard</button>}
      <span className="spacer" />
      <button className="button ghost" onClick={edit}>Edit</button>
      <button className="button ghost danger" disabled={!!busy} onClick={remove} aria-label={`Remove ${s.name}`}><Icon name="trash" size={14} /></button>
    </div>
    {s.status === 'needs_mapping' && !s.proposal && !ai && detail.has_baseline &&
      <small className="muted">AI is off: only columns with matching names will be mapped.</small>}
  </article>
}

/* ---------- reviewing a proposed mapping ---------- */

const describe = (op: Step) => op.effect || `${op.operation.replace(/_/g, ' ')} ${Object.values(op.args).join(' → ')}`

function MappingReview({ proposal: p, projectId, source, refresh }: { proposal: Mapping; projectId: string; source: Source; refresh: () => Promise<unknown> }) {
  const [busy, act] = useAction()
  const notify = useNotify()
  const [keep, setKeep] = useState(() => p.operations.map(() => true))
  const chosen = p.operations.filter((_, i) => keep[i])
  const approve = () => act('approve', async () => {
    await api.saveRecipe(projectId, source.id, chosen.map(({ effect: _effect, ...step }) => step))
    await refresh()
    notify.ok(`${source.name} is mapped. Every pull now converts it automatically.`)
  })
  const discard = () => act('discard', async () => { await api.discardMapping(projectId, source.id); await refresh() })

  return <section className="triage mapping">
    <header>
      <span className="pill verdict issues"><Icon name="sparkle" size={12} /> Proposed mapping</span>
      <small className="muted">{p.model ? 'AI + name matching' : 'Name matching'}</small>
    </header>
    <div className="fix-score">
      <div><small>Issues against the standard</small><b>{p.before.total} <span>→</span> {p.after.total}</b></div>
      <div><small>Rows</small><b>{p.rows.before.toLocaleString()} <span>→</span> {p.rows.after.toLocaleString()}</b></div>
    </div>
    <p>{p.summary}</p>
    <h4>Steps · uncheck any you don't want</h4>
    <ul className="recipe">{p.operations.map((op, i) => <li key={i}>
      <label><input type="checkbox" checked={keep[i]} onChange={e => setKeep(k => k.map((v, j) => j === i ? e.target.checked : v))} />
        <span><b>{describe(op)}</b><small>{i < p.automatic_steps ? 'Matched by name' : op.reason}</small></span></label>
    </li>)}</ul>
    {p.not_fixed.length > 0 && <><h4>Not mapped</h4>
      <ul className="notify">{p.not_fixed.map((n, i) => <li key={i}><b>{n.issue}</b> {n.why}</li>)}</ul></>}
    {p.after.remaining.length > 0 && <p className="note"><Icon name="info" size={13} /> Still flagged after mapping: {p.after.remaining.join('; ')}</p>}
    <div className="actions">
      <button className="button primary" disabled={!!busy} onClick={approve}>{busy === 'approve' ? <Spinner text="Saving…" /> : `Approve ${plural(chosen.length, 'step')}`}</button>
      <button className="button ghost" disabled={!!busy} onClick={discard}>Discard</button>
    </div>
  </section>
}

/* ---------- add / edit a source ---------- */

function SourceDialog({ projectId, kinds, source, close, saved }:
  { projectId: string; kinds: SourceKind[]; source: Source | null; close: () => void; saved: () => void }) {
  const notify = useNotify()
  const [busy, act] = useAction()
  const [kind, setKind] = useState(source?.kind || '')
  const [name, setName] = useState(source?.name || '')
  const [values, setValues] = useState<Record<string, string>>(() => {
    const { saved_secrets: _saved, ...rest } = source?.settings || {}
    return rest as Record<string, string>
  })
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null)
  const plugin = kinds.find(k => k.kind === kind)
  const groups = useMemo(() => [...new Set(kinds.map(k => k.group))], [kinds])
  const savedSecrets = source?.settings.saved_secrets || []

  const settings = () => Object.fromEntries(Object.entries(values).filter(([k]) => plugin?.fields.some(f => f.name === k)))
  const test = () => act('test', async () => setResult(await api.testSource(projectId, kind, settings(), source?.id)))
  const submit = (e: FormEvent) => {
    e.preventDefault()
    act('save', async () => {
      const form = { name: name.trim() || plugin!.label, kind, settings: settings() }
      if (source) await api.editSource(projectId, source.id, form)
      else await api.addSource(projectId, form)
      notify.ok(`${form.name} saved`)
      saved()
    })
  }

  return <Modal title={source ? `Edit ${source.name}` : 'Add a data source'} onClose={close}>
    {!plugin ? <div className="kind-picker">
      {groups.map(g => <div key={g}>
        <h4>{g}</h4>
        <div className="kinds">{kinds.filter(k => k.group === g).map(k => <button key={k.kind} type="button" className="kind"
          disabled={!k.available} title={k.available ? k.description : `Needs ${k.missing_packages.join(', ')} on the server`}
          onClick={() => { setKind(k.kind); setValues({}); setResult(null) }}>
          <b>{k.label}</b><small>{k.available ? k.description : 'Not installed on this server'}</small>
        </button>)}</div>
      </div>)}
    </div> : <form className="source-form" onSubmit={submit}>
      {!source && <button type="button" className="link back" onClick={() => setKind('')}>← {plugin.label}</button>}
      <label>Name<input value={name} onChange={e => setName(e.target.value)} placeholder={`e.g. ${plugin.group === 'Databases' ? 'Store database' : 'Sales sheet'}`} maxLength={80} /></label>
      {plugin.fields.map(f => <label key={f.name}>{f.label}{!f.required && <small className="muted"> optional</small>}
        {f.multiline
          ? <textarea rows={3} value={values[f.name] || ''} placeholder={f.placeholder} spellCheck={false}
            onChange={e => { setValues(v => ({ ...v, [f.name]: e.target.value })); setResult(null) }} />
          : <input type={f.secret ? 'password' : undefined} value={values[f.name] || ''} autoComplete="off" spellCheck={false}
            placeholder={f.secret && savedSecrets.includes(f.name) ? 'Saved. Leave empty to keep it' : f.placeholder}
            onChange={e => { setValues(v => ({ ...v, [f.name]: e.target.value })); setResult(null) }} />}
        {f.help && <small className="muted">{f.help}</small>}
      </label>)}
      {plugin.fields.some(f => f.secret) && <p className="muted small"><Icon name="shield" size={13} /> Passwords stay on the DataPulse server and are never shown again. A read-only database user is best.</p>}
      {result && <p className={`test-result ${result.ok ? 'ok' : 'bad'}`}><Icon name={result.ok ? 'check' : 'alert'} size={14} /> {result.message}</p>}
      <div className="actions">
        <button type="button" className="button outline" disabled={!!busy} onClick={test}>{busy === 'test' ? <Spinner text="Connecting…" /> : 'Test connection'}</button>
        <button className="button primary" disabled={!!busy}>{busy === 'save' ? <Spinner text="Saving…" /> : source ? 'Save changes' : 'Add source'}</button>
      </div>
    </form>}
  </Modal>
}

/* ---------- where a check's data came from ---------- */

export function SourceNote({ projectId, run }: { projectId: string; run: Run }) {
  const kinds = useCatalog()
  const s = run.report.source!
  return <div className="source-note">
    <Icon name="database" size={15} />
    <span>Pulled {s.rows_pulled.toLocaleString()} rows from <b>{s.name}</b> ({labelOf(kinds, s.kind)})
      {s.mapped ? (s.steps.length ? `, converted to the standard in ${plural(s.steps.length, 'step')}` : '') : '. Not mapped to the standard yet.'}</span>
    <a className="link" href={api.runDataUrl(projectId, run.id)}><Icon name="download" size={13} /> Data</a>
    {s.problems.map(p => <p key={p} className="note warn"><Icon name="alert" size={13} /> The source changed shape. {p}</p>)}
  </div>
}

export function UnifiedNote({ projectId, run }: { projectId: string; run: Run }) {
  const kinds = useCatalog()
  return <div className="source-note unified">
    <Icon name="database" size={15} />
    <span>Combined from {plural(run.report.unified!.sources.filter(s => !s.error).length, 'source')}, each converted to the standard.</span>
    <a className="button outline" href={api.runDataUrl(projectId, run.id)}><Icon name="download" size={14} /> Download unified table</a>
    <ul>{run.report.unified!.sources.map(s => <li key={s.name}>
      <b>{s.name}</b> <small className="muted">{labelOf(kinds, s.kind)}</small>
      {s.error ? <em className="bad">{s.error}</em> : <span>{s.rows.toLocaleString()} rows</span>}
      {s.problems.map(p => <em key={p} className="bad">{p}</em>)}
    </li>)}</ul>
  </div>
}
