import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { api, API, fmtDate, timeAgo, type Project, type ProjectDetail, type Severity } from './api'
import { Icon } from './icons'
import { BrandMark, CodeBlock, Dropzone, Modal, RunTrend, SchemaTable, ToneIcon, Toasts, TONE_LABEL, runTone, useTheme, type Toast } from './components'

const SEVERITIES: Severity[] = ['critical', 'warning', 'info']
const SEVERITY_LABEL: Record<Severity, string> = { critical: 'Critical', warning: 'Warning', info: 'Info' }
const SEVERITY_ICON = { critical: 'alert', warning: 'flag', info: 'activity' } as const
const DOCS_URL = `${API || 'http://localhost:8000'}/docs`

type Health = 'checking' | 'online' | 'offline'

export default function App() {
  const { theme, toggle: toggleTheme } = useTheme()
  const [projects, setProjects] = useState<Project[]>([])
  const [activeId, setActiveId] = useState('')
  const [detail, setDetail] = useState<ProjectDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [health, setHealth] = useState<Health>('checking')
  const [toasts, setToasts] = useState<Toast[]>([])
  const [projectName, setProjectName] = useState('')
  const [creating, setCreating] = useState(false)
  const [seeding, setSeeding] = useState(false)
  const [apiKey, setApiKey] = useState('')
  const [modal, setModal] = useState<'' | 'connect' | 'rules' | 'new'>('')
  const [configText, setConfigText] = useState('')
  const [savingConfig, setSavingConfig] = useState(false)
  const [baselineFile, setBaselineFile] = useState<File | null>(null)
  const [candidateFile, setCandidateFile] = useState<File | null>(null)
  const [busy, setBusy] = useState<'' | 'baseline' | 'runs'>('')
  const [aiEnabled, setAiEnabled] = useState(false)
  const [suggestions, setSuggestions] = useState('')
  const [selectedRunId, setSelectedRunId] = useState('')
  const [severityFilter, setSeverityFilter] = useState<Severity | 'all'>('all')

  const notify = useCallback((kind: Toast['kind'], text: string) => setToasts(list => [...list.slice(-2), { id: Date.now() + Math.random(), kind, text }]), [])
  const dismiss = useCallback((id: number) => setToasts(list => list.filter(t => t.id !== id)), [])
  const fail = useCallback((e: unknown) => notify('error', (e as Error).message), [notify])
  const closeModal = useCallback(() => setModal(''), [])

  const loadDetail = useCallback(async (id: string) => {
    const item = await api<ProjectDetail>(`/api/projects/${id}`)
    setDetail(item)
    setConfigText(item.config_yaml)
  }, [])

  const refresh = useCallback(async (select?: string) => {
    const list = await api<Project[]>('/api/projects')
    setProjects(list)
    const nextId = select || list[0]?.id || ''
    setActiveId(nextId)
    if (nextId) await loadDetail(nextId); else setDetail(null)
  }, [loadDetail])

  useEffect(() => {
    api('/api/health').then(() => setHealth('online')).catch(() => setHealth('offline'))
    // deep link: #/p/<project>/r/<run>
    const [, project, , run] = window.location.hash.match(/^#\/p\/([\w-]+)(\/r\/([\w-]+))?/) || []
    if (run) setSelectedRunId(run)
    refresh(project).catch(() => refresh()).catch(fail).finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    if (!activeId) return
    const hash = `#/p/${activeId}${selectedRunId ? `/r/${selectedRunId}` : ''}`
    if (window.location.hash !== hash) window.history.replaceState(null, '', hash)
  }, [activeId, selectedRunId])

  function selectProject(id: string) {
    if (id === activeId) return
    setActiveId(id); setSuggestions(''); setSelectedRunId(''); setSeverityFilter('all')
    setBaselineFile(null); setCandidateFile(null)
    loadDetail(id).catch(fail)
  }

  const runs = detail?.runs || []
  const latest = runs[0]
  const viewed = runs.find(run => run.id === selectedRunId) || latest
  const viewingLatest = viewed?.id === latest?.id
  const findings = viewed?.report?.issues || []
  const severityCounts = useMemo(() => Object.fromEntries(SEVERITIES.map(s => [s, findings.filter(f => f.severity === s).length])) as Record<Severity, number>, [findings])
  const shownFindings = severityFilter === 'all' ? findings : findings.filter(f => f.severity === severityFilter)
  const allColumns = useMemo(() => Object.entries(detail?.baseline?.columns || {}), [detail])
  const passRate = runs.length ? Math.round(runs.filter(r => r.status === 'passed').length / runs.length * 100) : null

  async function createProject(event: FormEvent) {
    event.preventDefault()
    if (projectName.trim().length < 2) return
    setCreating(true)
    try {
      const created = await api<{ id: string; api_key: string }>('/api/projects', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: projectName.trim() }) })
      setApiKey(created.api_key); setProjectName(''); setModal(''); setSelectedRunId('')
      await refresh(created.id)
      notify('success', 'Project created. Save the API key now — it is shown only once.')
    } catch (e) { fail(e) } finally { setCreating(false) }
  }

  async function seedDemo() {
    setSeeding(true)
    try {
      const created = await api<{ id: string; api_key: string }>('/api/demo/seed', { method: 'POST' })
      setApiKey(created.api_key); setModal(''); setSelectedRunId('')
      await refresh(created.id)
      notify('success', 'Sample workspace ready. Upload a changed file to run a real check.')
    } catch (e) { fail(e) } finally { setSeeding(false) }
  }

  async function upload(role: 'baseline' | 'runs') {
    const file = role === 'baseline' ? baselineFile : candidateFile
    if (!file || !activeId) return
    setBusy(role)
    const form = new FormData(); form.append('file', file); form.append('name', file.name)
    if (role === 'runs') form.append('use_ai', String(aiEnabled))
    try {
      await api(`/api/projects/${activeId}/${role}`, { method: 'POST', body: form })
      if (role === 'baseline') setBaselineFile(null); else setCandidateFile(null)
      setSelectedRunId(''); setSeverityFilter('all')
      await refresh(activeId)
      notify('success', role === 'baseline' ? 'Baseline saved. DataPulse is ready to monitor this dataset.' : 'Quality check finished — results are below.')
      if (role === 'runs') document.getElementById('findings')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    } catch (e) { fail(e) } finally { setBusy('') }
  }

  async function saveConfig() {
    if (!activeId) return
    setSavingConfig(true)
    try {
      await api(`/api/projects/${activeId}/config`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ config_yaml: configText }) })
      setModal(''); await refresh(activeId); notify('success', 'Quality rules saved.')
    } catch (e) { fail(e) } finally { setSavingConfig(false) }
  }

  async function loadSuggestions() {
    if (!activeId) return
    try { setSuggestions((await api<{ yaml: string }>(`/api/projects/${activeId}/suggestions`)).yaml) } catch (e) { fail(e) }
  }

  async function copy(text: string, what = 'Copied to clipboard.') {
    try { await navigator.clipboard.writeText(text); notify('success', what) } catch { notify('error', 'Copy is unavailable in this browser. Select and copy the text.') }
  }

  const origin = API || (typeof window !== 'undefined' && window.location.port === '5173' ? 'http://localhost:8000' : window.location.origin)
  const curlSnippet = detail ? `curl -X POST ${origin}/api/projects/${detail.id}/ingest \\
  -H 'Content-Type: application/json' \\
  -H 'X-DataPulse-Key: ${apiKey || 'YOUR_PROJECT_API_KEY'}' \\
  -d '{"dataset_name":"orders","role":"candidate",
       "rows":[{"customer_id":"C009","amount":22.5,"status":"paid"}]}'` : ''

  const status = !detail ? null : !detail.has_baseline ? { tone: 'idle', label: 'Setup needed' } : latest?.report.status === 'failed' ? { tone: 'failed', label: 'Action needed' } : latest ? { tone: 'healthy', label: 'Healthy' } : { tone: 'ready', label: 'Ready' }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><BrandMark /><div><b>datapulse</b><small>Data quality, in context</small></div></div>
      <div className="side-label"><span>Projects</span><button className="ghost-icon" aria-label="New project" title="New project" onClick={() => setModal('new')}><Icon name="plus" size={15} /></button></div>
      <nav className="project-list" aria-label="Projects">
        {loading && !projects.length && [0, 1, 2].map(i => <div key={i} className="skeleton project-skeleton" />)}
        {projects.map(project => <button key={project.id} onClick={() => selectProject(project.id)} className={`project-link ${activeId === project.id ? 'active' : ''}`} aria-current={activeId === project.id ? 'page' : undefined}>
          <span className="project-dot">{project.name.slice(0, 1).toUpperCase()}</span>
          <span className="project-text"><b>{project.name}</b><small>{project.run_count} check{project.run_count === 1 ? '' : 's'} · {project.has_baseline ? 'monitoring' : 'setup needed'}</small></span>
          <i className={`status-dot ${project.latest_status === 'failed' ? 'red' : project.latest_status === 'passed' ? 'green' : ''}`} title={project.latest_status || 'no runs yet'}></i>
        </button>)}
        {!projects.length && !loading && <div className="empty-side">Your projects will appear here.</div>}
      </nav>
      <div className="sidebar-bottom">
        <div className="stack-card"><Icon name="layers" size={18} /><div><b>Any stack. One signal.</b><small>Send rows from any language through the REST API.</small></div></div>
        <a className="docs-link" href={DOCS_URL} target="_blank" rel="noreferrer"><Icon name="code" size={14} /> API documentation <Icon name="external" size={13} className="push" /></a>
      </div>
    </aside>

    <main className="main">
      <header className="topbar">
        <div className="crumb"><span>Workspace</span><b>/</b><strong>{detail?.name || 'Overview'}</strong></div>
        <div className="top-actions">
          <span className={`connected ${health}`} title={health === 'offline' ? 'Start the API: uvicorn server.app:app --port 8000' : undefined}><i></i>{health === 'online' ? 'API connected' : health === 'offline' ? 'API offline' : 'Connecting…'}</span>
          <button className="icon-button" aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`} title="Toggle theme" onClick={toggleTheme}><Icon name={theme === 'dark' ? 'sun' : 'moon'} /></button>
          <button className="icon-button" aria-label="Connect a service" title="Connect a service" onClick={() => setModal('connect')}><Icon name="plug" /></button>
        </div>
      </header>

      <div className="content">
        {apiKey && <div className="key-banner fade-in"><Icon name="shield" size={20} /><div><b>Save your project API key</b><span>Only its hash is stored — it won't be shown again after you leave this screen.</span><code>{apiKey}</code></div><button className="button secondary" onClick={() => copy(apiKey, 'API key copied.')}><Icon name="copy" size={14} /> Copy key</button><button className="ghost-icon" aria-label="Dismiss" onClick={() => setApiKey('')}><Icon name="x" /></button></div>}

        {loading ? <LoadingView /> : !detail ? <Welcome
          projectName={projectName} setProjectName={setProjectName} creating={creating} createProject={createProject}
          seeding={seeding} seedDemo={seedDemo} openConnect={() => setModal('connect')} /> : <div className="project-view fade-in" key={detail.id}>

          <section className="project-header">
            <div>
              <div className="eyebrow"><span className={`pulse ${status?.tone}`}></span>{status?.label}</div>
              <h1>{detail.name}</h1>
              <p className="meta-line">
                {detail.has_baseline ? <>Baseline <b>v{detail.baseline?.baseline_version}</b> · {detail.baseline?.rows.toLocaleString()} rows · {allColumns.length} columns{detail.baseline?.source ? <> · <code>{detail.baseline.source}</code></> : null}</> : 'No baseline yet — upload a known-good file to start monitoring.'}
              </p>
            </div>
            <div className="header-actions">
              <button className="button outline" onClick={() => setModal('rules')}><Icon name="edit" size={14} /> Edit rules</button>
              <button className="button" onClick={() => setModal('connect')}><Icon name="plug" size={14} /> Connect API</button>
            </div>
          </section>

          <section className="summary-grid">
            <Stat label="Status" icon="activity" value={status?.label || '—'} tone={status?.tone}
              foot={latest ? `Last checked ${timeAgo(latest.created_at)}` : detail.has_baseline ? 'Waiting for the first check' : 'Add a baseline to begin'} />
            <Stat label="Open findings" icon="flag" value={String(latest?.issue_count ?? 0)}
              foot={<><span className="sev-count critical"><b>{latest?.report.issues.filter(i => i.severity === 'critical').length ?? 0}</b> critical</span><span className="sev-count warning"><b>{latest?.report.issues.filter(i => i.severity === 'warning').length ?? 0}</b> warning</span></>} />
            <Stat label="Pass rate" icon="check" value={passRate === null ? '—' : `${passRate}%`} foot={runs.length ? `Across the last ${runs.length} run${runs.length === 1 ? '' : 's'}` : 'No runs yet'} />
            <Stat label="Fail threshold" icon="shield" value={detail.fail_on} foot={`${detail.datasets.length} dataset${detail.datasets.length === 1 ? '' : 's'} uploaded`} />
          </section>

          <section className="work-grid">
            <div className="panel">
              <div className="panel-head"><div><div className="kicker">Dataset monitor</div><h2>Run a quality check</h2><p>Set a trusted baseline, then compare each new batch against it.</p></div></div>
              <div className="steps">
                <div className={`step ${detail.has_baseline ? 'done' : 'current'}`}>
                  <div className="step-head"><span className="step-num">{detail.has_baseline ? <Icon name="check" size={13} /> : '1'}</span><div><b>Known-good baseline</b><small>{detail.has_baseline ? `v${detail.baseline?.baseline_version} saved · upload again to re-baseline` : 'The profile every batch is compared against'}</small></div></div>
                  <Dropzone file={baselineFile} onFile={setBaselineFile} hint="CSV · JSON · JSONL · Parquet · 25 MB" />
                  <button className="button outline full" disabled={!baselineFile || !!busy} onClick={() => upload('baseline')}>{busy === 'baseline' ? <><span className="spinner" /> Profiling…</> : <>{detail.has_baseline ? 'Replace baseline' : 'Save baseline'} <Icon name="arrow" size={14} /></>}</button>
                </div>
                <div className={`step ${detail.has_baseline ? 'current' : 'locked'}`}>
                  <div className="step-head"><span className="step-num">2</span><div><b>New data batch</b><small>{detail.has_baseline ? 'Compared against the saved baseline' : 'Unlocks once a baseline is saved'}</small></div></div>
                  <Dropzone file={candidateFile} onFile={setCandidateFile} disabled={!detail.has_baseline} hint="Upload the new version to inspect" />
                  <button className="button primary full" disabled={!candidateFile || !detail.has_baseline || !!busy} onClick={() => upload('runs')}>{busy === 'runs' ? <><span className="spinner" /> Analyzing data…</> : <>Run quality check <Icon name="arrow" size={14} /></>}</button>
                </div>
              </div>
              <div className="panel-foot">
                <span><Icon name="shield" size={14} /> Files stay in this workspace; DataPulse stores profiles and reports.</span>
                <label className="switch"><input type="checkbox" checked={aiEnabled} onChange={e => setAiEnabled(e.target.checked)} /><span className="track"></span><Icon name="sparkle" size={13} /> AI explanations</label>
              </div>
            </div>

            <div className="panel">
              <div className="panel-head"><div><div className="kicker">Activity</div><h2>Run history</h2></div><span className="count-chip">{runs.length} saved</span></div>
              {runs.length ? <>
                <RunTrend runs={runs} selectedId={viewed?.id || ''} onSelect={id => { setSelectedRunId(id); setSeverityFilter('all') }} />
                <div className="history-list">
                  {runs.slice(0, 5).map(run => <button key={run.id} className={`history-item ${run.id === viewed?.id ? 'active' : ''}`} onClick={() => { setSelectedRunId(run.id); setSeverityFilter('all') }}>
                    <ToneIcon run={run} />
                    <span className="history-main"><b>{TONE_LABEL[runTone(run)]}</b><small>{run.report.rows.current.toLocaleString()} rows · {fmtDate(run.created_at)}</small></span>
                    <span className={`history-findings ${runTone(run)}`}>{run.issue_count ? `${run.issue_count} finding${run.issue_count === 1 ? '' : 's'}` : 'Clean'}</span>
                  </button>)}
                </div>
              </> : <EmptyState icon="activity" title="No runs yet" text="Each quality check you run is saved here with a trend over time." />}
            </div>
          </section>

          <section className="panel findings-panel" id="findings">
            <div className="panel-head">
              <div>
                <div className="kicker">{viewed ? `${viewingLatest ? 'Latest run' : 'Earlier run'} · ${fmtDate(viewed.created_at)}` : 'Awaiting data'}</div>
                <h2>{!viewed ? 'No quality runs yet' : viewed.report.status === 'failed' ? 'Findings to review' : findings.length ? 'Passed, with notes' : 'All checks passed'}</h2>
              </div>
              {viewed && <div className="findings-actions">
                {!viewingLatest && <button className="button ghost" onClick={() => setSelectedRunId('')}><Icon name="back" size={14} /> Latest run</button>}
                {viewed.report.markdown && <button className="button ghost" onClick={() => copy(viewed.report.markdown || '', 'Markdown report copied.')}><Icon name="copy" size={14} /> Markdown</button>}
                <span className={`run-badge ${runTone(viewed)}`}><ToneIcon run={viewed} />{viewed.report.status === 'failed' ? 'Needs attention' : findings.length ? 'Passed with issues' : 'All clear'}</span>
              </div>}
            </div>

            {viewed && <div className="run-meta">
              <span><b>{viewed.report.rows.current.toLocaleString()}</b> rows checked</span>
              <span><b>{viewed.report.rows.baseline.toLocaleString()}</b> in baseline</span>
              <span><b>{viewed.report.passed_checks?.length ?? 0}</b> checks passed</span>
              {viewed.report.schema_changed && <span className="schema-flag"><Icon name="alert" size={13} /> Schema changed</span>}
            </div>}

            {!viewed ? <EmptyState icon="database" title="Run your first check" text="Upload a new data batch above. Findings appear here, linked to the consumers they affect." />
              : findings.length === 0 ? <div className="clear-state"><span className="tone-icon clean big"><Icon name="check" size={18} /></span><div><b>Everything looks good.</b><small>{viewed.report.passed_checks?.length ? `Passed: ${viewed.report.passed_checks.join(', ')}` : 'No configured checks failed on this batch.'}</small></div></div>
              : <>
                <div className="filter-row" role="tablist" aria-label="Filter by severity">
                  <button role="tab" aria-selected={severityFilter === 'all'} className={`filter-chip ${severityFilter === 'all' ? 'active' : ''}`} onClick={() => setSeverityFilter('all')}>All <span>{findings.length}</span></button>
                  {SEVERITIES.filter(s => severityCounts[s]).map(s => <button role="tab" aria-selected={severityFilter === s} key={s} className={`filter-chip ${s} ${severityFilter === s ? 'active' : ''}`} onClick={() => setSeverityFilter(s)}><Icon name={SEVERITY_ICON[s]} size={13} />{SEVERITY_LABEL[s]} <span>{severityCounts[s]}</span></button>)}
                </div>
                <div className="finding-list">
                  {shownFindings.map((item, index) => <article className={`finding ${item.severity}`} key={`${viewed.id}-${item.check}-${item.columns.join(',')}-${index}`} style={{ animationDelay: `${Math.min(index, 8) * 35}ms` }}>
                    <div className="finding-side"><span className={`severity-pill ${item.severity}`}><Icon name={SEVERITY_ICON[item.severity]} size={13} />{SEVERITY_LABEL[item.severity]}</span></div>
                    <div className="finding-body">
                      <div className="finding-title"><b>{item.title}</b><span className="check-name">{item.check}</span></div>
                      <div className="finding-fields">{item.columns.length ? item.columns.map(col => <code key={col}>{col}</code>) : <code>dataset</code>}</div>
                      <p className="measured">{item.measured_summary}</p>
                      {item.examples?.length ? <p className="examples">Examples: {item.examples.join('; ')}</p> : null}
                      <p className="explanation"><i>Possible explanation (suggested, not verified):</i> {item.suggested_explanation}</p>
                      {item.ai_explanation && <p className="explanation ai"><i><Icon name="sparkle" size={12} /> AI-generated (unverified):</i> {item.ai_explanation}</p>}
                    </div>
                    <div className="finding-consumers">
                      <small>Affects</small>
                      {item.consumers?.length ? item.consumers.map(c => <span className="consumer-chip" key={c.name} title={c.dataset_wide ? 'Reads this dataset; the issue is dataset-wide' : `Uses ${c.columns.join(', ')}`}>{c.name}</span>) : <span className="muted">No mapped consumers</span>}
                    </div>
                  </article>)}
                </div>
              </>}
            {viewed?.report?.ai_note && <div className="ai-note"><span><Icon name="sparkle" size={13} /> AI explanation</span>{viewed.report.ai_note}</div>}
          </section>

          <section className="bottom-row">
            <div className="panel">
              <div className="panel-head"><div><div className="kicker">Downstream impact</div><h2>What depends on this data</h2><p>When a field drifts, see the dashboards and models that may break.</p></div></div>
              {detail.consumers.length ? <div className="consumer-list">{detail.consumers.map((consumer, i) => {
                const affected = latest?.report.affected_consumers?.includes(consumer.name)
                return <div className={`consumer-item ${affected ? 'affected' : ''}`} key={consumer.name}>
                  <div className={`consumer-symbol c${i % 4}`}>{consumer.name.slice(0, 1).toUpperCase()}</div>
                  <div className="consumer-main"><b>{consumer.name}</b><small>{consumer.type}{consumer.owner ? ` · ${consumer.owner}` : ''}{consumer.columns.length ? ` · ${consumer.columns.join(', ')}` : ''}</small></div>
                  <span className={`state-chip ${affected ? 'affected' : ''}`}>{affected ? <><Icon name="alert" size={12} /> Affected</> : 'Mapped'}</span>
                </div>
              })}</div> : <EmptyState icon="layers" title="No consumers mapped" text="Add a consumers: section to the rules to see who is affected by each finding." action={<button className="button outline" onClick={() => setModal('rules')}><Icon name="edit" size={14} /> Edit rules</button>} />}
            </div>
            <div className="panel">
              <div className="panel-head"><div><div className="kicker">Baseline profile</div><h2>Schema</h2><p>{allColumns.length ? `${allColumns.length} columns profiled from the trusted baseline.` : 'Columns appear once a baseline is saved.'}</p></div></div>
              {allColumns.length ? <SchemaTable columns={allColumns} /> : <EmptyState icon="table" title="No baseline yet" text="Save a known-good file to see each column's type, null rate and cardinality." />}
            </div>
          </section>
        </div>}

        <footer><span>DataPulse · data quality in context</span><span>Same checks as the CLI, REST API and GitHub Action</span></footer>
      </div>
    </main>

    <Toasts items={toasts} dismiss={dismiss} />

    {modal === 'connect' && <Modal onClose={closeModal} labelledBy="connect-title">
      <div className="modal-icon"><Icon name="plug" size={18} /></div>
      <div className="kicker">Language-agnostic ingestion</div>
      <h2 id="connect-title">Connect any service</h2>
      <p>Send JSON rows to the REST API from any language or runtime. The project API key is returned once, when the project is created.</p>
      <div className="endpoint-row"><span>POST</span><code>/api/projects/{detail?.id || '{project_id}'}/ingest</code></div>
      {curlSnippet ? <CodeBlock code={curlSnippet} onCopy={() => copy(curlSnippet, 'Command copied.')} /> : <div className="modal-hint">Create a project to get a project-specific endpoint and API key.</div>}
      <div className="language-chips">{['Node.js', 'Python', 'Go', 'Java', 'Ruby', 'C#', 'PHP', 'Rust'].map(l => <span key={l}>{l}</span>)}</div>
      <div className="modal-hint"><b>Large datasets:</b> use the dashboard upload or the GitHub Action. The API accepts up to 50,000 rows per request.</div>
      <a href={DOCS_URL} target="_blank" rel="noreferrer" className="link">Open interactive API docs <Icon name="external" size={13} /></a>
    </Modal>}

    {modal === 'rules' && detail && <Modal onClose={closeModal} labelledBy="rules-title">
      <div className="modal-icon"><Icon name="edit" size={18} /></div>
      <div className="kicker">{detail.name}</div>
      <h2 id="rules-title">Quality rules</h2>
      <p>This is the project's <code>datapulse.yml</code>, validated exactly like the CLI's.</p>
      <textarea className="yaml-editor" value={configText} onChange={e => setConfigText(e.target.value)} spellCheck={false} aria-label="DataPulse YAML configuration"
        onKeyDown={e => { if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); saveConfig() } }} />
      <div className="modal-actions">
        {detail.has_baseline && <button className="button outline" onClick={loadSuggestions}><Icon name="sparkle" size={14} /> Suggest from baseline</button>}
        <span className="hint">Ctrl/⌘ + S to save</span>
        <button className="button primary" onClick={saveConfig} disabled={savingConfig}>{savingConfig ? <><span className="spinner" /> Saving…</> : 'Save rules'}</button>
      </div>
      {suggestions && <div className="suggestions"><b>Suggested rules — review before copying; nothing was applied.</b><CodeBlock code={suggestions} onCopy={() => copy(suggestions, 'Suggestions copied.')} /></div>}
    </Modal>}

    {modal === 'new' && <Modal onClose={closeModal} labelledBy="new-title">
      <div className="modal-icon"><Icon name="plus" size={18} /></div>
      <div className="kicker">Start monitoring</div>
      <h2 id="new-title">New data project</h2>
      <CreateForm projectName={projectName} setProjectName={setProjectName} creating={creating} createProject={createProject} autoFocus />
      <div className="divider"><span>or</span></div>
      <button className="button outline full" onClick={seedDemo} disabled={seeding}>{seeding ? <><span className="spinner" /> Loading…</> : <><Icon name="sparkle" size={14} /> Load the sample workspace</>}</button>
    </Modal>}
  </div>
}

function Stat({ label, icon, value, foot, tone }: { label: string; icon: 'activity' | 'flag' | 'check' | 'shield'; value: string; foot: ReactNode; tone?: string }) {
  return <div className={`stat-card ${tone ? `tone-${tone}` : ''}`}>
    <div className="stat-label">{label}<Icon name={icon} size={15} /></div>
    <div className="stat-value">{value}</div>
    <div className="stat-foot">{foot}</div>
  </div>
}

function EmptyState({ icon, title, text, action }: { icon: 'activity' | 'database' | 'layers' | 'table'; title: string; text: string; action?: ReactNode }) {
  return <div className="empty-state"><span className="empty-icon"><Icon name={icon} size={22} /></span><b>{title}</b><span>{text}</span>{action}</div>
}

function LoadingView() {
  return <div className="loading-view" aria-busy="true" aria-label="Loading">
    <div className="skeleton" style={{ width: 120, height: 14 }} /><div className="skeleton" style={{ width: 320, height: 38, marginTop: 14 }} />
    <div className="summary-grid" style={{ marginTop: 28 }}>{[0, 1, 2, 3].map(i => <div key={i} className="skeleton" style={{ height: 112 }} />)}</div>
    <div className="work-grid">{[0, 1].map(i => <div key={i} className="skeleton" style={{ height: 320 }} />)}</div>
  </div>
}

type CreateProps = { projectName: string; setProjectName: (v: string) => void; creating: boolean; createProject: (e: FormEvent) => void; autoFocus?: boolean }

function CreateForm({ projectName, setProjectName, creating, createProject, autoFocus }: CreateProps) {
  return <form onSubmit={createProject} className="create-form">
    <label htmlFor="project-name">Project name</label>
    <div className="input-button">
      <input id="project-name" autoFocus={autoFocus} value={projectName} onChange={e => setProjectName(e.target.value)} placeholder="e.g. Orders pipeline" minLength={2} maxLength={80} required />
      <button className="button primary" disabled={creating || projectName.trim().length < 2}>{creating ? <><span className="spinner" /> Creating…</> : <>Create <Icon name="arrow" size={14} /></>}</button>
    </div>
  </form>
}

function Welcome(props: CreateProps & { seeding: boolean; seedDemo: () => void; openConnect: () => void }) {
  return <div className="fade-in">
    <section className="hero">
      <div>
        <div className="eyebrow"><span className="pulse healthy"></span>Data observability · language agnostic</div>
        <h1>Trust your data<br /><em>in every deploy.</em></h1>
        <p>Catch schema drift and quality issues before they break the dashboards, models and services that depend on your data.</p>
      </div>
      <div className="hero-art" aria-hidden="true">
        <div className="orbit orbit-one"></div><div className="orbit orbit-two"></div><div className="orbit orbit-three"></div>
        <div className="orbit-center"><BrandMark large /></div>
        <div className="orbit-node n1">CSV</div><div className="orbit-node n2">JSON</div><div className="orbit-node n3">API</div><div className="orbit-node n4">PARQUET</div>
      </div>
    </section>
    <section className="welcome-grid">
      <div className="panel welcome-card">
        <div className="kicker">Start monitoring</div>
        <h2>Create a data project</h2>
        <p>Connect a dataset and DataPulse builds a baseline you can check on every release.</p>
        <CreateForm {...props} />
        <div className="format-row"><span>Supported</span>{['CSV', 'JSON', 'JSONL', 'Parquet'].map(f => <b key={f}>{f}</b>)}</div>
      </div>
      <div className="panel welcome-card sample-card">
        <div className="kicker"><Icon name="sparkle" size={13} /> Interactive starter</div>
        <h2>Explore a working dataset</h2>
        <p>Load a sample orders dataset with a baseline, downstream map and quality rules — then upload a changed file to watch the monitor work.</p>
        <button className="button primary" onClick={props.seedDemo} disabled={props.seeding}>{props.seeding ? <><span className="spinner" /> Loading…</> : <>Load sample workspace <Icon name="arrow" size={14} /></>}</button>
      </div>
      <button className="wide-note" onClick={props.openConnect}>
        <span className="wide-icon"><Icon name="plug" size={16} /></span>
        <span className="wide-text"><b>Connect any language over HTTP</b><small>Java · JavaScript · Go · Ruby · Python · anything that can send JSON</small></span>
        <span className="link">View API pattern <Icon name="arrow" size={14} /></span>
      </button>
    </section>
  </div>
}
