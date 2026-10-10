import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'

const API = import.meta.env.VITE_API_URL || ''

type Severity = 'critical' | 'warning' | 'info'
type IssueConsumer = { name: string; type: string; owner: string; columns: string[]; dataset_wide: boolean }
type Issue = {
  check: string; severity: Severity; title: string; columns: string[]; measured_summary: string;
  suggested_explanation: string; ai_explanation?: string | null; examples: string[]; consumers: IssueConsumer[]
}
type Report = {
  status: 'passed' | 'failed'; fail_on: string; issues: Issue[]; rows: { baseline: number; current: number };
  counts: Record<Severity, number>; passed_checks: string[]; affected_consumers: string[]; schema_changed: boolean;
  ai_note?: string | null; markdown?: string
}
type Run = { id: string; status: string; issue_count: number; created_at: string; report: Report }
type Project = { id: string; name: string; has_baseline: boolean; run_count: number; latest_status?: string | null }
type Consumer = { name: string; type: string; owner: string; columns: string[] }
type ProjectDetail = {
  id: string; name: string; has_baseline: boolean; config_yaml: string; fail_on: string; consumers: Consumer[];
  baseline?: { baseline_version: number; rows: number; source?: string; columns: Record<string, { kind: string; dtype: string; null_rate: number; unique_count: number }> } | null;
  datasets: Array<{ id: string; name: string; role: string; row_count: number; columns: string[]; created_at: string }>;
  runs: Run[];
}

const SEVERITY_LABEL: Record<Severity, string> = { critical: '● Critical', warning: '● Warning', info: '● Info' }

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, init)
  if (!response.ok) {
    let message = `Request failed (${response.status})`
    try { const body = await response.json(); message = body.detail || message } catch { /* keep status message */ }
    throw new Error(message)
  }
  return response.json()
}

function fmtDate(value?: string) { return value ? new Date(value).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '—' }

export default function App() {
  const [projects, setProjects] = useState<Project[]>([])
  const [activeId, setActiveId] = useState('')
  const [detail, setDetail] = useState<ProjectDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [projectName, setProjectName] = useState('')
  const [creating, setCreating] = useState(false)
  const [apiKey, setApiKey] = useState('')
  const [configOpen, setConfigOpen] = useState(false)
  const [configText, setConfigText] = useState('')
  const [baselineFile, setBaselineFile] = useState<File | null>(null)
  const [candidateFile, setCandidateFile] = useState<File | null>(null)
  const [scanning, setScanning] = useState(false)
  const [aiEnabled, setAiEnabled] = useState(false)
  const [showConnect, setShowConnect] = useState(false)
  const [suggestions, setSuggestions] = useState('')

  const refresh = useCallback(async (select?: string) => {
    const list = await api<Project[]>('/api/projects')
    setProjects(list)
    const nextId = select || activeId || list[0]?.id || ''
    setActiveId(nextId)
    if (nextId) {
      const item = await api<ProjectDetail>(`/api/projects/${nextId}`)
      setDetail(item)
      setConfigText(item.config_yaml)
    } else setDetail(null)
  }, [activeId])

  useEffect(() => {
    refresh().catch(e => setError(e.message)).finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    if (!activeId) return
    setSuggestions('')
    api<ProjectDetail>(`/api/projects/${activeId}`).then(item => { setDetail(item); setConfigText(item.config_yaml) }).catch(e => setError(e.message))
  }, [activeId])

  const latest = detail?.runs?.[0]
  const findings = latest?.report?.issues || []
  const errors = findings.filter(item => item.severity === 'critical').length
  const warnings = findings.filter(item => item.severity === 'warning').length
  const allColumns = useMemo(() => Object.entries(detail?.baseline?.columns || {}), [detail])

  async function createProject(event: FormEvent) {
    event.preventDefault()
    if (projectName.trim().length < 2) return
    setCreating(true); setError('')
    try {
      const created = await api<{ id: string; api_key: string }>('/api/projects', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: projectName.trim() }) })
      setApiKey(created.api_key)
      setProjectName('')
      await refresh(created.id)
      setNotice('Project created. Save the API key now; it is shown only once.')
    } catch (e) { setError((e as Error).message) } finally { setCreating(false) }
  }

  async function seedDemo() {
    setError(''); setLoading(true)
    try {
      const created = await api<{ id: string; api_key: string }>('/api/demo/seed', { method: 'POST' })
      setApiKey(created.api_key)
      await refresh(created.id)
      setNotice('Sample workspace ready. Upload a changed file to run a real check.')
    } catch (e) { setError((e as Error).message) } finally { setLoading(false) }
  }

  async function upload(role: 'baseline' | 'runs') {
    const file = role === 'baseline' ? baselineFile : candidateFile
    if (!file || !activeId) return
    setError(''); setNotice(''); setScanning(true)
    const form = new FormData(); form.append('file', file); form.append('name', file.name)
    if (role === 'runs') form.append('use_ai', String(aiEnabled))
    try {
      await api(`/api/projects/${activeId}/${role === 'baseline' ? 'baseline' : 'runs'}`, { method: 'POST', body: form })
      if (role === 'baseline') setBaselineFile(null); else setCandidateFile(null)
      await refresh(activeId)
      setNotice(role === 'baseline' ? 'Baseline saved. DataPulse is ready to monitor this dataset.' : 'Quality check finished. The latest findings are shown below.')
    } catch (e) { setError((e as Error).message) } finally { setScanning(false) }
  }

  async function saveConfig() {
    if (!activeId) return
    setError(''); setNotice('')
    try {
      await api(`/api/projects/${activeId}/config`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ config_yaml: configText }) })
      setConfigOpen(false); await refresh(activeId); setNotice('Quality rules saved.')
    } catch (e) { setError((e as Error).message) }
  }

  async function loadSuggestions() {
    if (!activeId) return
    setError('')
    try { setSuggestions((await api<{ yaml: string }>(`/api/projects/${activeId}/suggestions`)).yaml) } catch (e) { setError((e as Error).message) }
  }

  async function copy(text: string) {
    try { await navigator.clipboard.writeText(text); setNotice('Copied to clipboard.') } catch { setNotice('Copy is unavailable in this browser. Select and copy the text.') }
  }

  const curlSnippet = apiKey && detail ? `curl -X POST http://localhost:8000/api/projects/${detail.id}/ingest -H 'Content-Type: application/json' -H 'X-DataPulse-Key: ${apiKey}' -d '{"dataset_name":"orders","role":"candidate","rows":[{"customer_id":"C009","amount":22.5,"status":"paid"}]}'` : ''

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><span></span><span></span><span></span><span></span></div><div><b>datapulse</b><small>DATA QUALITY, IN CONTEXT</small></div></div>
      <div className="side-label">WORKSPACES <button className="tiny-add" onClick={() => document.getElementById('project-name')?.focus()}>＋</button></div>
      <div className="project-list">
        {projects.map(project => <button key={project.id} onClick={() => { setActiveId(project.id); setError('') }} className={`project-link ${activeId === project.id ? 'active' : ''}`}>
          <span className="project-dot">{project.name.slice(0, 1).toUpperCase()}</span><span className="project-text"><b>{project.name}</b><small>{project.run_count} checks · {project.has_baseline ? 'monitoring' : 'setup needed'}</small></span>
          <i className={`status-dot ${project.latest_status === 'failed' ? 'red' : project.latest_status === 'passed' ? 'green' : ''}`}></i>
        </button>)}
        {!projects.length && !loading && <div className="empty-side">Your projects will appear here.</div>}
      </div>
      <div className="sidebar-bottom"><div className="stack-card"><div className="stack-icon">◎</div><div><b>Any stack. One signal.</b><small>Send data from any language with the REST API.</small></div></div><a className="docs-link" href="http://localhost:8000/docs" target="_blank" rel="noreferrer"><span>⌘</span> API documentation <span className="external">↗</span></a><div className="profile"><div className="avatar">DP</div><span><b>DataPulse</b><small>Local workspace</small></span><span className="profile-menu">•••</span></div></div>
    </aside>

    <main className="main">
      <header className="topbar"><div className="crumb"><span>Workspace</span><b>/</b><strong>{detail?.name || 'Overview'}</strong></div><div className="top-actions"><span className="connected"><i></i> API connected</span><button className="icon-button" title="API connection" onClick={() => setShowConnect(true)}>⌘</button><div className="avatar small-avatar">KB</div></div></header>
      <div className="content">
        <div className="eyebrow"><span className="pulse-icon">◉</span> DATA OBSERVABILITY <span className="eyebrow-line"></span><span className="stack-agnostic">LANGUAGE AGNOSTIC</span></div>
        <section className="hero-row"><div><h1>Trust your data<br /><em>in every deploy.</em></h1><p>Catch schema drift and quality issues before they break the dashboards, models, and services that depend on your data.</p></div><div className="hero-art"><div className="orbit orbit-one"></div><div className="orbit orbit-two"></div><div className="orbit orbit-three"></div><div className="orbit-center"><div className="brand-mark large"><span></span><span></span><span></span><span></span></div></div><div className="orbit-node n1">CSV</div><div className="orbit-node n2">JSON</div><div className="orbit-node n3">API</div><div className="orbit-node n4">PARQUET</div></div></section>

        {error && <div className="alert error-alert"><b>Something needs attention</b><span>{error}</span><button onClick={() => setError('')}>×</button></div>}
        {notice && <div className="alert success-alert"><b>✓</b><span>{notice}</span><button onClick={() => setNotice('')}>×</button></div>}
        {apiKey && <div className="key-banner"><div><b>Save your project API key</b><span>It is stored as a hash and will not be shown again after you leave this screen.</span><code>{apiKey}</code></div><button className="button secondary" onClick={() => copy(apiKey)}>Copy key</button><button className="close-key" onClick={() => setApiKey('')}>×</button></div>}

        {!detail ? <section className="welcome-grid">
          <div className="panel create-panel"><div className="panel-kicker">START MONITORING</div><h2>Create a data project</h2><p>Connect a dataset and DataPulse will build a baseline you can check on every release.</p><form onSubmit={createProject} className="create-form"><label htmlFor="project-name">Project name</label><div className="input-button"><input id="project-name" value={projectName} onChange={e => setProjectName(e.target.value)} placeholder="e.g. Orders pipeline" minLength={2} maxLength={80} required /><button disabled={creating}>{creating ? 'Creating…' : 'Create project'} <span>→</span></button></div></form><div className="format-row"><span>SUPPORTED DATA</span><b>CSV</b><b>JSON</b><b>JSONL</b><b>PARQUET</b></div></div>
          <div className="panel sample-panel"><div className="sample-top"><div className="sample-icon">✦</div><span className="sample-tag">INTERACTIVE STARTER</span></div><h2>Explore a working dataset</h2><p>Load a real sample orders dataset, baseline, downstream map, and quality rules. Then upload a changed file to see the monitor work.</p><button className="button dark" onClick={seedDemo} disabled={loading}>{loading ? 'Loading…' : 'Load sample workspace'} <span>→</span></button><div className="sample-note"><i></i> Uses the same checks as the REST API and GitHub Action.</div></div>
          <div className="wide-note"><span>↗</span><div><b>Connect any language through HTTP</b><small>Java · JavaScript · Go · Ruby · Python · anything that can send JSON</small></div><button onClick={() => setShowConnect(true)}>View API pattern <span>→</span></button></div>
        </section> : <>
          <section className="summary-grid">
            <div className="stat-card"><div className="stat-label">MONITORING STATUS <span className="stat-icon">◉</span></div><div className="stat-value">{!detail.has_baseline ? 'Setup needed' : latest?.report?.status === 'failed' ? 'Action needed' : latest ? 'Healthy' : 'Ready'}</div><div className="stat-foot"><i className={`status-dot ${latest?.report?.status === 'failed' ? 'red' : detail.has_baseline ? 'green' : ''}`}></i>{latest ? `Last checked ${fmtDate(latest.created_at)}` : detail.has_baseline ? 'Baseline created · waiting for next run' : 'Add a baseline to start monitoring'}</div></div>
            <div className="stat-card"><div className="stat-label">DATASETS TRACKED <span className="stat-icon">▤</span></div><div className="stat-value">{detail.datasets.length}<small> datasets</small></div><div className="stat-foot">{detail.has_baseline ? `Baseline v${detail.baseline?.baseline_version} · ${allColumns.length} columns` : 'No baseline yet'}</div></div>
            <div className="stat-card"><div className="stat-label">OPEN FINDINGS <span className="stat-icon">⚑</span></div><div className="stat-value">{errors + warnings}<small> findings</small></div><div className="stat-foot"><span className="severity-text"><b>{errors}</b> critical</span><span className="severity-text warn"><b>{warnings}</b> warnings</span></div></div>
            <div className="stat-card"><div className="stat-label">TOTAL CHECKS <span className="stat-icon">◷</span></div><div className="stat-value">{detail.runs.length}<small> runs</small></div><div className="stat-foot">Fails on <code>{detail.fail_on}</code> and above</div></div>
          </section>

          <section className="work-grid">
            <div className="panel run-panel"><div className="section-head"><div><div className="panel-kicker">DATASET MONITOR</div><h2>Run a quality check</h2><p>Set a trusted baseline, then compare each new batch.</p></div><button className="text-button" onClick={() => setShowConnect(true)}>⌘ Connect API</button></div>
              <div className="upload-grid">
                <div className={`upload-box ${detail.has_baseline ? 'has-baseline' : ''}`}><div className="upload-title"><span className="step-number">01</span><div><b>Known-good baseline</b><small>{detail.has_baseline ? `v${detail.baseline?.baseline_version} · ${detail.baseline?.rows?.toLocaleString()} rows saved` : 'Create a profile to compare against'}</small></div><span className={`mini-state ${detail.has_baseline ? 'done' : ''}`}>{detail.has_baseline ? 'SAVED' : 'REQUIRED'}</span></div><label className="file-picker"><span className="upload-symbol">↑</span><span>{baselineFile?.name || 'Choose a file'}<small>CSV · JSON · JSONL · Parquet · 25 MB max</small></span><input type="file" accept=".csv,.json,.jsonl,.ndjson,.parquet,.pq" onChange={e => setBaselineFile(e.target.files?.[0] || null)} /></label><button className="button outline full" disabled={!baselineFile || scanning} onClick={() => upload('baseline')}>{scanning ? 'Processing…' : 'Save baseline'} <span>→</span></button></div>
                <div className={`upload-box candidate-box ${!detail.has_baseline ? 'locked' : ''}`}><div className="upload-title"><span className="step-number">02</span><div><b>New data batch</b><small>{detail.has_baseline ? 'Compare with the saved baseline' : 'Available once baseline is ready'}</small></div><span className="mini-state">CHECK</span></div><label className="file-picker"><span className="upload-symbol">↥</span><span>{candidateFile?.name || 'Choose a file'}<small>Upload a new version to inspect</small></span><input type="file" accept=".csv,.json,.jsonl,.ndjson,.parquet,.pq" onChange={e => setCandidateFile(e.target.files?.[0] || null)} disabled={!detail.has_baseline} /></label><button className="button primary full" disabled={!candidateFile || !detail.has_baseline || scanning} onClick={() => upload('runs')}>{scanning ? <><span className="spinner"></span> Analyzing data…</> : <>Run quality check <span>→</span></>}</button></div>
              </div>
              <div className="upload-foot"><span>▣</span> Your uploaded dataset stays in this workspace. DataPulse stores profiles and reports for review.<label className="ai-toggle"><input type="checkbox" checked={aiEnabled} onChange={e => setAiEnabled(e.target.checked)} /> AI explanation</label></div>
            </div>
            <div className="panel consumers-panel"><div className="panel-kicker">DOWNSTREAM IMPACT</div><div className="section-title-line"><h2>What depends on your data</h2><button className="edit-button" onClick={() => setConfigOpen(!configOpen)}>{configOpen ? 'Close' : 'Edit rules'}</button></div><p>When a field drifts, see the dashboards and models that may be affected.</p>
              {configOpen ? <div className="config-editor"><textarea value={configText} onChange={e => setConfigText(e.target.value)} spellCheck={false} aria-label="DataPulse YAML configuration"/><button className="button primary full" onClick={saveConfig}>Save quality rules <span>→</span></button>{detail.has_baseline && <button className="button outline full" onClick={loadSuggestions}>Suggest rules from baseline <span>✦</span></button>}{suggestions && <div className="suggestions"><b>Suggested rules (review before copying; nothing was applied)</b><div className="code-snippet"><button onClick={() => copy(suggestions)}>Copy</button><pre>{suggestions}</pre></div></div>}</div> : <div className="consumer-list">{detail.consumers.length ? detail.consumers.slice(0, 5).map((consumer, i) => <div className="consumer-item" key={consumer.name}><div className={`consumer-symbol c${i % 4}`}>{['◉', '↗', '▤', '◈'][i % 4]}</div><div className="consumer-main"><b>{consumer.name}</b><small>{consumer.type}{consumer.owner ? ` · ${consumer.owner}` : ''} · {consumer.columns.join(', ')}</small></div><span className="consumer-count">{latest?.report.affected_consumers?.includes(consumer.name) ? 'AFFECTED' : 'MAPPED'}</span></div>) : allColumns.slice(0, 4).map(([col, stats], i) => <div className="consumer-item" key={col}><div className={`consumer-symbol c${i}`}>{['◉', '↗', '▤', '◈'][i]}</div><div className="consumer-main"><b>{col}</b><small>{stats.kind} · {(stats.null_rate * 100).toFixed(1)}% null · {stats.unique_count.toLocaleString()} unique</small></div><span className="consumer-count">FIELD</span></div>)}{!detail.consumers.length && <div className="empty-small">{allColumns.length ? 'No consumers mapped yet. Add a consumers: section to the rules.' : 'Create a baseline to view fields.'}</div>}</div>}
              <button className="rules-link" onClick={() => setConfigOpen(!configOpen)}>{configOpen ? 'Back to impact map' : 'Configure quality rules'} <span>→</span></button>
            </div>
          </section>

          <section className="panel findings-panel"><div className="section-head"><div><div className="panel-kicker">LATEST RUN · {latest ? fmtDate(latest.created_at).toUpperCase() : 'AWAITING DATA'}</div><h2>{latest ? (latest.report.status === 'failed' ? 'Findings to review' : 'Checks passed') : 'No quality runs yet'}</h2></div>{latest && <div className="findings-actions">{latest.report.markdown && <button className="edit-button" onClick={() => copy(latest.report.markdown || '')}>Copy Markdown report</button>}<span className={`run-badge ${latest.report.status}`}>{latest.report.status === 'failed' ? '● NEEDS ATTENTION' : findings.length ? '● PASSED WITH ISSUES' : '● ALL CLEAR'}</span></div>}</div>
            {!latest ? <div className="empty-run"><div className="empty-illustration">⌁</div><b>Run your first check</b><span>Upload a new data batch above. Results and run history will appear here.</span></div> : findings.length === 0 ? <div className="clear-state"><span>✓</span><div><b>Everything looks good.</b><small>{latest.report.passed_checks?.length ? `Passed: ${latest.report.passed_checks.join(', ')}` : 'No configured checks failed on this batch.'}</small></div></div> : <div className="table-wrap"><table><thead><tr><th>SEVERITY</th><th>FIELD</th><th>FINDING</th><th>MEASURED</th><th>AFFECTED CONSUMERS</th></tr></thead><tbody>{findings.map((item, index) => <tr key={`${item.check}-${item.columns.join(',')}-${index}`}><td><span className={`severity-pill ${item.severity}`}>{SEVERITY_LABEL[item.severity]}</span></td><td>{item.columns.length ? item.columns.map(col => <code key={col}>{col}</code>) : <code>dataset</code>}</td><td><b>{item.title}</b><small>{item.check}</small><span className="explanation"><i>Possible explanation (suggested, not verified):</i> {item.suggested_explanation}</span>{item.ai_explanation && <span className="explanation ai"><i>✦ AI-generated (unverified):</i> {item.ai_explanation}</span>}</td><td>{item.measured_summary}{item.examples?.length ? <small>Examples: {item.examples.join('; ')}</small> : null}</td><td>{item.consumers?.length ? item.consumers.map(c => <span className="consumer-chip" key={c.name} title={c.dataset_wide ? 'Reads this dataset; the issue is dataset-wide' : `Uses ${c.columns.join(', ')}`}>{c.name}</span>) : '—'}</td></tr>)}</tbody></table></div>}
            {latest?.report?.ai_note && <div className="ai-note"><span>✦ AI EXPLANATION</span>{latest.report.ai_note}</div>}
          </section>
          <section className="bottom-row"><div className="panel history-panel"><div className="section-title-line"><div><div className="panel-kicker">ACTIVITY</div><h2>Recent runs</h2></div><span className="history-count">{detail.runs.length} saved</span></div>{detail.runs.length ? detail.runs.slice(0, 5).map(run => <div className="history-item" key={run.id}><span className={`history-icon ${run.status}`}>{run.status === 'passed' ? '✓' : '!'}</span><div><b>{run.status === 'passed' ? (run.issue_count ? 'Passed with issues' : 'Quality checks passed') : 'Checks failed'}</b><small>{run.report.rows.current.toLocaleString()} rows · {fmtDate(run.created_at)}</small></div><span className={`history-findings ${run.status}`}>{run.issue_count ? `${run.issue_count} findings` : 'Clean'}</span></div>) : <div className="empty-small">Checks you run will be saved here.</div>}</div><div className="panel integration-panel"><div className="panel-kicker">BUILT FOR YOUR STACK</div><h2>One API. Every language.</h2><p>Send rows from Node, Java, Go, Ruby, Python, or any service that can make an HTTP request.</p><div className="language-chips"><span>Node.js</span><span>Java</span><span>Go</span><span>Ruby</span><span>Python</span><span>REST</span></div><button className="button dark" onClick={() => setShowConnect(true)}>Connect a service <span>→</span></button></div></section>
        </>}
        <footer><span>DataPulse <i>·</i> data quality in context</span><span>Built for pipelines in every stack <b>✦</b></span></footer>
      </div>
    </main>

    {showConnect && <div className="modal-backdrop" role="presentation" onClick={() => setShowConnect(false)}><div className="connect-modal" role="dialog" aria-modal="true" aria-labelledby="connect-title" onClick={e => e.stopPropagation()}><button className="modal-close" onClick={() => setShowConnect(false)}>×</button><div className="modal-icon">⌘</div><div className="panel-kicker">LANGUAGE-AGNOSTIC INGESTION</div><h2 id="connect-title">Connect any service</h2><p>Send JSON rows to the DataPulse REST API from any language or runtime. The project API key is returned once when you create a project.</p><div className="endpoint-row"><span>POST</span><code>/api/projects/{detail?.id || '{project_id}'}/ingest</code></div><div className="code-snippet"><button onClick={() => curlSnippet && copy(curlSnippet)}>Copy</button><pre>{curlSnippet || `Create a project to get a project-specific endpoint and API key.`}</pre></div><div className="modal-hint"><b>For large datasets:</b> use the dashboard upload or run the GitHub Action against a file in your repository. The API accepts up to 50,000 JSON rows per request.</div><a href="http://localhost:8000/docs" target="_blank" rel="noreferrer" className="api-doc-link">Open interactive API docs ↗</a></div></div>}
  </div>
}
