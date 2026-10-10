import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { api, plural, runTone, timeAgo, type Destination, type DestinationForm, type Mapping, type ProjectDetail, type Run, type Source, type SourceKind, type Step } from './api'
import { FlowDiagram, HealthCards, lastCheckOf, MappingCompare, TrendChart } from './flow'
import { Icon, type IconName } from './icons'
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
const KIND_ICON: Record<string, IconName> = {
  database: 'database', file: 'upload', link: 'external', rest: 'code', google_drive: 'external', web_page: 'activity',
}

const FILE_TYPES = '.csv,.json,.jsonl,.ndjson,.parquet,.pq,.xlsx,.xls,.pdf,.docx,.txt,.md,.markdown,.eml,.html,.htm,.png,.jpg,.jpeg,.webp,.gif'
const NAME_HINT: Record<string, string> = {
  database: 'Store database', file: 'March export', link: 'Sales sheet', rest: 'Shop API', google_drive: 'Shared orders file', web_page: 'Supplier page',
}

/** Mirrors the server's detection, to show which database a pasted URL points at. */
const DETECTED: Array<[RegExp, string]> = [
  [/^mongodb(\+srv)?:/i, 'MongoDB'], [/^(postgres(ql)?|redshift|timescaledb)(\+\w+)?:/i, 'PostgreSQL'],
  [/^(mysql|mariadb)(\+\w+)?:/i, 'MySQL / MariaDB'], [/^(mssql|sqlserver)(\+\w+)?:/i, 'Microsoft SQL Server'],
  [/^snowflake:/i, 'Snowflake'], [/^bigquery:/i, 'BigQuery'], [/^oracle(\+\w+)?:/i, 'Oracle'], [/^databricks:/i, 'Databricks'],
]
const detected = (url: string) => url.includes('://') ? DETECTED.find(([re]) => re.test(url.trim()))?.[1] || 'SQL database (driver must be installed on the server)' : null

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
    const sent = (report.delivered || []).filter(d => d.ok).length, held = (report.delivered || []).some(d => d.held)
    notify.ok(`Unified ${plural(report.rows.current, 'row')}: ${report.issues.length ? plural(report.issues.length, 'issue') + ' to review' : 'all clear'}`
      + (sent ? ` · sent to ${plural(sent, 'destination')}` : held ? ' · held back from your database' : ''))
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

    <HealthCards detail={detail} sources={list} />
    {list.length > 0 && <FlowDiagram detail={detail} sources={list} icon={k => KIND_ICON[k] || 'database'} openRun={openRun}
      unify={unify} unifying={busy === 'unify'} canUnify={ready.length > 0} />}
    <TrendChart runs={detail.runs} openRun={openRun} />

    {list.length === 0 ? <Empty icon="plug" title="No sources yet">
      <span>Any database, a file, a link, a REST API, Google Drive or a web page. Use test data to try one without your own credentials.</span>
    </Empty> : <div className="source-list">
      {list.map(s => <SourceCard key={s.id} source={s} kinds={kinds} detail={detail} ai={ai}
        refresh={refresh} edit={() => setEditing(s)} openRun={openRun} />)}
    </div>}

    {detail.has_baseline && <Destinations projectId={detail.id} />}

    {editing && <SourceDialog projectId={detail.id} kinds={kinds} source={editing === 'new' ? null : editing}
      close={() => setEditing(null)} saved={() => { setEditing(null); reload() }} />}
  </>
}

function SourceCard({ source: s, kinds, detail, ai, refresh, edit, openRun }:
  { source: Source; kinds: SourceKind[]; detail: ProjectDetail; ai: boolean; refresh: () => Promise<unknown>; edit: () => void; openRun: (run?: string) => void }) {
  const notify = useNotify()
  const [busy, act] = useAction()
  const [comparing, setComparing] = useState(false)
  const status = STATUS[s.status]
  const lastRun = lastCheckOf(detail, s)
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

  return <article className="card source" id={`source-${s.id}`}>
    <header>
      <span className="source-icon"><Icon name={KIND_ICON[s.kind] || 'database'} size={18} /></span>
      <div>
        <h3>{s.name}</h3>
        <small className="muted">{s.type || labelOf(kinds, s.kind)}{s.last_pulled_at ? ` · pulled ${timeAgo(s.last_pulled_at)}` : ' · never pulled'}
          {lastRun && <> · last check <span className={`flow-status ${runTone(lastRun)}`}>{lastRun.issue_count ? plural(lastRun.issue_count, 'issue') : 'passed'}</span></>}</small>
      </div>
      <span className={`pill verdict ${status.tone}`}>{status.label}{s.status === 'mapped' && s.recipe.length > 0 && ` · ${plural(s.recipe.length, 'step')}`}</span>
    </header>

    {s.proposal && <MappingReview proposal={s.proposal} projectId={detail.id} source={s} refresh={refresh} />}
    {!s.proposal && comparing && <MappingCompare projectId={detail.id} source={s} operations={s.recipe} />}

    <div className="actions">
      {s.proposal ? null : !detail.has_baseline ? <button className="button primary" disabled={!!busy} onClick={() => pull('baseline')}>
        {busy === 'baseline' ? <Spinner text="Learning the format…" /> : 'Use as the standard'}</button>
        : s.status === 'needs_mapping' ? <button className="button primary ai" disabled={!!busy} onClick={map}>
          {busy === 'map' ? <Spinner text="Mapping…" /> : <><Icon name="sparkle" size={14} /> Map to the standard</>}</button>
        : <button className="button primary" disabled={!!busy} onClick={() => pull('candidate')}>
          {busy === 'candidate' ? <Spinner text="Pulling…" /> : 'Pull & check'}</button>}
      {s.last_run_id && <button className="button ghost" onClick={() => openRun(s.last_run_id!)}>Last check</button>}
      {s.status === 'mapped' && <button className="button ghost" disabled={!!busy} onClick={map}>{busy === 'map' ? <Spinner text="Mapping…" /> : 'Re-map'}</button>}
      {!s.proposal && detail.has_baseline && s.status !== 'standard' && <button className="button ghost" onClick={() => setComparing(c => !c)}>
        {comparing ? 'Hide columns' : 'Compare columns'}</button>}
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
    <h4>Their column → our column · updates as you choose steps</h4>
    <MappingCompare projectId={projectId} source={source} operations={chosen.map(({ effect: _effect, ...step }) => step)} />
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
  const savedSecrets = source?.settings.saved_secrets || []

  const settings = () => Object.fromEntries(Object.entries(values).filter(([k]) => plugin?.fields.some(f => f.name === k)))
  const test = () => act('test', async () => setResult(await api.testSource(projectId, kind, settings(), source?.id)))
  const fill = () => act('sample', async () => {
    const sample = await api.sourceSample(projectId, kind)
    setName(current => current.trim() ? current : sample.name)
    setValues(current => ({ ...current, ...sample.settings }))
    setResult(null)
  })
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

  const upload = (field: string, file: File) => act('upload', async () => {
    const stored = await api.uploadSourceFile(projectId, file)
    setValues(v => ({ ...v, [field]: stored.file }))
    setResult(stored.note ? { ok: true, message: stored.note } : null)
    if (!name.trim()) setName(stored.name.replace(/\.[^.]+$/, ''))
  })
  const choices = kinds.filter(k => !k.hidden)

  return <Modal title={source ? `Edit ${source.name}` : 'Add a data source'} onClose={close}>
    {!plugin ? <div className="kind-picker">
      <p className="muted">Where does this data live?</p>
      <div className="kinds big">{choices.map(k => <button key={k.kind} type="button" className="kind"
        disabled={!k.available} title={k.available ? undefined : `Needs ${k.missing_packages.join(', ')} on the server`}
        onClick={() => { setKind(k.kind); setValues({}); setResult(null) }}>
        <span className="source-icon"><Icon name={KIND_ICON[k.kind] || 'database'} size={20} /></span>
        <b>{k.label}</b><small>{k.available ? k.description : 'Not installed on this server'}</small>
      </button>)}</div>
    </div> : <form className="source-form" onSubmit={submit}>
      {!source && <button type="button" className="link back" onClick={() => setKind('')}>← {plugin.label}</button>}
      <label>Name<input value={name} onChange={e => setName(e.target.value)} maxLength={80}
        placeholder={`e.g. ${NAME_HINT[kind] || 'Sales data'}`} /></label>
      {plugin.fields.map(f => <label key={f.name}>{f.label}{!f.required && <small className="muted"> optional</small>}
        {f.input === 'file'
          ? <span className="file-pick">
            <input type="file" accept={FILE_TYPES} disabled={!!busy}
              onChange={e => { const file = e.target.files?.[0]; e.target.value = ''; if (file) upload(f.name, file) }} />
            <small className="muted">{busy === 'upload' ? 'Uploading… documents are read by the AI, which can take a minute' : values[f.name] ? `Uploaded: ${values[f.name].split('/').pop()}` : f.placeholder}</small>
          </span>
          : f.multiline
          ? <textarea rows={3} value={values[f.name] || ''} placeholder={f.placeholder} spellCheck={false}
            onChange={e => { setValues(v => ({ ...v, [f.name]: e.target.value })); setResult(null) }} />
          : <input type={f.secret ? 'password' : undefined} value={values[f.name] || ''} autoComplete="off" spellCheck={false}
            placeholder={f.secret && savedSecrets.includes(f.name) ? 'Saved. Leave empty to keep it' : f.placeholder}
            onChange={e => { setValues(v => ({ ...v, [f.name]: e.target.value })); setResult(null) }} />}
        {kind === 'database' && f.name === 'url' && values.url && detected(values.url)
          ? <small className="detected"><Icon name="check" size={12} /> {detected(values.url)}</small>
          : f.help && <small className="muted">{f.help}</small>}
      </label>)}
      {plugin.fields.some(f => f.secret) && <p className="muted small"><Icon name="shield" size={13} /> Passwords stay on the DataPulse server and are never shown again. A read-only database user is best.</p>}
      {result && <p className={`test-result ${result.ok ? 'ok' : 'bad'}`}><Icon name={result.ok ? 'check' : 'alert'} size={14} /> {result.message}</p>}
      <div className="actions">
        {plugin.has_sample && <button type="button" className="button ghost" disabled={!!busy} onClick={fill}>
          {busy === 'sample' ? <Spinner text="Preparing…" /> : 'Use test data'}</button>}
        <button type="button" className="button outline" disabled={!!busy} onClick={test}>
          {busy === 'test' ? <Spinner text={kind === 'file' ? 'Reading…' : 'Connecting…'} /> : kind === 'file' ? 'Read the file' : 'Test connection'}</button>
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
    <span>Pulled {s.rows_pulled.toLocaleString()} rows from <b>{s.name}</b> ({s.type || labelOf(kinds, s.kind)})
      {s.mapped ? (s.steps.length ? `, converted to the standard in ${plural(s.steps.length, 'step')}` : '') : '. Not mapped to the standard yet.'}</span>
    <a className="link" href={api.runDataUrl(projectId, run.id)}><Icon name="download" size={13} /> Data</a>
    {s.problems.map(p => <p key={p} className="note warn"><Icon name="alert" size={13} /> The source changed shape. {p}</p>)}
  </div>
}

export function UnifiedNote({ projectId, run, changed }: { projectId: string; run: Run; changed: () => Promise<unknown> }) {
  const kinds = useCatalog()
  const notify = useNotify()
  const [busy, act] = useAction()
  const delivered = run.report.delivered || []
  const resend = () => act('send', async () => {
    const { delivered } = await api.sendRun(projectId, run.id)
    await changed()
    if (!delivered.length) notify.ok('Add a destination on the Sources tab first')
    else if (delivered.every(d => d.ok)) notify.ok(`Sent to ${plural(delivered.length, 'destination')}`)
    else notify.fail(new Error(delivered.find(d => !d.ok)!.message))
  })
  return <div className="source-note unified">
    <Icon name="database" size={15} />
    <span>Combined from {plural(run.report.unified!.sources.filter(s => !s.error).length, 'source')}, each converted to the standard.</span>
    <a className="button outline" href={api.runDataUrl(projectId, run.id)}><Icon name="download" size={14} /> Download unified table</a>
    <ul>{run.report.unified!.sources.map(s => <li key={s.name}>
      <b>{s.name}</b> <small className="muted">{s.type || labelOf(kinds, s.kind)}</small>
      {s.error ? <em className="bad">{s.error}</em> : <span>{s.rows.toLocaleString()} rows</span>}
      {s.problems.map(p => <em key={p} className="bad">{p}</em>)}
    </li>)}</ul>
    {delivered.length > 0 && <ul className="deliveries">{delivered.map(d => <li key={d.id} className={d.ok ? 'ok' : d.held ? 'held' : 'bad'}>
      <Icon name={d.ok ? 'check' : d.held ? 'shield' : 'alert'} size={13} /><b>{d.name}</b><span>{d.message}</span>
    </li>)}</ul>}
    {run.status === 'passed' && (!delivered.length || delivered.some(d => !d.ok)) &&
      <button className="button ghost" disabled={!!busy} onClick={resend}>{busy ? <Spinner text="Sending…" /> : <><Icon name="upload" size={14} /> Send to your database</>}</button>}
  </div>
}

/* ---------- destinations: send unified data into the company's own database ---------- */

function Destinations({ projectId }: { projectId: string }) {
  const notify = useNotify()
  const [list, setList] = useState<Destination[] | null>(null)
  const [editing, setEditing] = useState<Destination | 'new' | null>(null)
  const [busy, act] = useAction()
  const reload = useCallback(() => api.destinations(projectId).then(setList), [projectId])
  useEffect(() => { reload().catch(notify.fail) }, [reload, notify])
  if (!list) return null
  const remove = (d: Destination) => {
    if (!window.confirm(`Stop sending to "${d.name}"? Data already sent stays in your database.`)) return
    act(d.id, async () => { await api.removeDestination(projectId, d.id); await reload() })
  }
  return <section className="card destinations">
    <div className="card-head">
      <div><h2>Send unified data to</h2>
        <p className="muted">Every time you unify, the result is written into your own database, but only if it passes its checks.</p></div>
      <button className="button outline" onClick={() => setEditing('new')}><Icon name="plus" size={14} /> Add destination</button>
    </div>
    {list.length === 0 ? <p className="muted">No destination yet. Unified data can still be downloaded.</p>
      : <ul className="destination-list">{list.map(d => <li key={d.id}>
        <span className="source-icon"><Icon name="database" size={16} /></span>
        <div><b>{d.name}</b>
          <small className="muted">{d.type} · table <code>{d.table}</code> · {d.mode === 'replace' ? 'replaced each time' : 'new rows added each time'}</small>
          {d.last_result && <small className="muted">Last: {d.last_result} · {timeAgo(d.last_sent_at || undefined)}</small>}
        </div>
        <button className="button ghost" onClick={() => setEditing(d)}>Edit</button>
        <button className="button ghost danger" disabled={busy === d.id} onClick={() => remove(d)} aria-label={`Remove ${d.name}`}><Icon name="trash" size={14} /></button>
      </li>)}</ul>}
    {editing && <DestinationDialog projectId={projectId} destination={editing === 'new' ? null : editing}
      close={() => setEditing(null)} saved={() => { setEditing(null); reload() }} />}
  </section>
}

function DestinationDialog({ projectId, destination, close, saved }:
  { projectId: string; destination: Destination | null; close: () => void; saved: () => void }) {
  const notify = useNotify()
  const [busy, act] = useAction()
  const [form, setForm] = useState<DestinationForm>({ name: destination?.name || '', url: '', table: destination?.table || 'unified_data', mode: destination?.mode || 'replace' })
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null)
  const set = (k: keyof DestinationForm, v: string) => { setForm(f => ({ ...f, [k]: v })); setResult(null) }
  const test = () => act('test', async () => setResult(await api.testDestination(projectId, form, destination?.id)))
  const submit = (e: FormEvent) => {
    e.preventDefault()
    act('save', async () => {
      const body = { ...form, name: form.name.trim() || 'My database' }
      if (destination) await api.editDestination(projectId, destination.id, body)
      else await api.addDestination(projectId, body)
      notify.ok(`${body.name} saved. Unified data will be sent there.`)
      saved()
    })
  }
  return <Modal title={destination ? `Edit ${destination.name}` : 'Send unified data to your database'} onClose={close}>
    <form className="source-form" onSubmit={submit}>
      <label>Name<input value={form.name} onChange={e => set('name', e.target.value)} placeholder="e.g. Company warehouse" maxLength={80} /></label>
      <label>Connection URL
        <input type="password" value={form.url} onChange={e => set('url', e.target.value)} autoComplete="off" spellCheck={false}
          placeholder={destination ? 'Saved. Leave empty to keep it' : 'postgresql://user:password@host:5432/database'} />
        {form.url && detected(form.url) ? <small className="detected"><Icon name="check" size={12} /> {detected(form.url)}</small>
          : <small className="muted">PostgreSQL, Supabase, MySQL, SQL Server, MongoDB… The user needs permission to write this table.</small>}
      </label>
      <label>Table<input value={form.table} onChange={e => set('table', e.target.value)} spellCheck={false} placeholder="unified_data" />
        <small className="muted">Created on the first send if it doesn't exist. Only this table is ever written. For MongoDB: a collection.</small></label>
      <div className="mode-pick" role="radiogroup" aria-label="Each time">
        {(['replace', 'append'] as const).map(m => <label key={m} className={form.mode === m ? 'on' : ''}>
          <input type="radio" name="mode" checked={form.mode === m} onChange={() => set('mode', m)} />
          <span><b>{m === 'replace' ? 'Replace' : 'Add rows'}</b><small>{m === 'replace' ? 'The table always holds the latest unified data' : 'Each unify adds its rows, keeping history'}</small></span>
        </label>)}
      </div>
      <p className="muted small"><Icon name="shield" size={13} /> The URL stays on the DataPulse server. Data that fails its checks is never sent.</p>
      {result && <p className={`test-result ${result.ok ? 'ok' : 'bad'}`}><Icon name={result.ok ? 'check' : 'alert'} size={14} /> {result.message}</p>}
      <div className="actions">
        <button type="button" className="button outline" disabled={!!busy || (!form.url && !destination)} onClick={test}>{busy === 'test' ? <Spinner text="Connecting…" /> : 'Test connection'}</button>
        <button className="button primary" disabled={!!busy}>{busy === 'save' ? <Spinner text="Saving…" /> : destination ? 'Save changes' : 'Add destination'}</button>
      </div>
    </form>
  </Modal>
}
