import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { api, API_ORIGIN, plural, type Created, type Project } from './api'
import { Icon } from './icons'
import { ProjectView, TABS, type Tab } from './project'
import { Modal, Spinner, ToastProvider, useAction, useNotify, useTheme } from './ui'

export type Route = { id?: string; tab: Tab; run?: string }

// #/p/<project>/<tab>/<run> — the URL is the source of truth, so links and the back button work.
function parseHash(): Route {
  const [, id, tab, run] = window.location.hash.match(/^#\/p\/([\w-]+)(?:\/(\w+))?(?:\/([\w-]+))?/) || []
  return { id, tab: TABS.includes(tab as Tab) ? tab as Tab : 'checks', run }
}

function useRoute() {
  const [route, setRoute] = useState(parseHash)
  useEffect(() => {
    const onChange = () => setRoute(parseHash())
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  const go = useCallback((next: Route, replace = false) => {
    const hash = next.id ? `#/p/${next.id}/${next.tab}${next.run ? `/${next.run}` : ''}` : '#/'
    if (replace) { window.history.replaceState(null, '', hash); setRoute(parseHash()) } else window.location.hash = hash
  }, [])
  return [route, go] as const
}

export default function App() {
  return <ToastProvider><Shell /></ToastProvider>
}

function Shell() {
  const { theme, toggle } = useTheme()
  const [route, go] = useRoute()
  const [projects, setProjects] = useState<Project[] | null>(null)
  const [offline, setOffline] = useState(false)
  const [creating, setCreating] = useState(false)
  const [newKey, setNewKey] = useState<{ id: string; key: string } | null>(null)

  const refresh = useCallback(() => api.projects().then(list => { setProjects(list); setOffline(false) }, () => setOffline(true)), [])
  useEffect(() => { refresh() }, [refresh])

  // Land on the first project when the URL doesn't name one that exists.
  useEffect(() => {
    if (projects?.length && !projects.some(p => p.id === route.id)) go({ id: projects[0].id, tab: 'checks' }, true)
  }, [projects, route.id, go])

  const opened = useCallback((created: Created) => {
    setNewKey({ id: created.id, key: created.api_key })
    setCreating(false)
    refresh().then(() => go({ id: created.id, tab: 'checks' }))
  }, [refresh, go])

  const active = projects?.find(p => p.id === route.id)

  return <div className="shell">
    <aside className="sidebar">
      <div className="brand"><span className="logo"><Icon name="activity" size={18} /></span>DataPulse</div>
      <button className="button primary full" onClick={() => setCreating(true)} disabled={offline}><Icon name="plus" size={15} /> New project</button>
      <nav className="project-list" aria-label="Projects">
        {projects?.map(p => <a key={p.id} href={`#/p/${p.id}/checks`} className={`project-link ${p.id === route.id ? 'active' : ''}`}
          aria-current={p.id === route.id ? 'page' : undefined}>
          <i className={`dot ${p.latest_status || ''}`} title={p.latest_status ? `Last check ${p.latest_status}` : 'No checks yet'} />
          <span><b>{p.name}</b><small>{p.has_baseline ? plural(p.run_count, 'check') : 'Needs a baseline'}</small></span>
        </a>)}
      </nav>
      <div className="sidebar-foot">
        <a href={`${API_ORIGIN}/docs`} target="_blank" rel="noreferrer"><Icon name="code" size={14} /> API docs</a>
        <button className="icon-btn" onClick={toggle} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}>
          <Icon name={theme === 'dark' ? 'sun' : 'moon'} />
        </button>
      </div>
    </aside>

    <main className="main">
      {offline ? <Offline retry={refresh} />
        : projects === null ? <div className="loading"><span className="spinner" /> Loading…</div>
        : !projects.length ? <Welcome onCreated={opened} />
        : active && <ProjectView key={active.id} route={{ ...route, id: active.id }} go={go} onChanged={refresh}
            apiKey={newKey?.id === active.id ? newKey.key : undefined} dismissKey={() => setNewKey(null)} />}
    </main>

    {creating && <Modal title="New project" onClose={() => setCreating(false)}>
      <p className="muted">A project holds one dataset's baseline, rules and check history.</p>
      <CreateProject onCreated={opened} autoFocus />
    </Modal>}
  </div>
}

function Offline({ retry }: { retry: () => void }) {
  return <div className="center-card">
    <span className="empty-icon bad"><Icon name="plug" size={22} /></span>
    <h1>Can't reach the DataPulse API</h1>
    <p>In the project folder (not <code>web</code>), start the server in its own terminal, then try again:</p>
    <pre className="inline-code">python -m uvicorn server.app:app --port 8000</pre>
    <p className="muted">It's running when that terminal shows <code>Uvicorn running on http://127.0.0.1:8000</code>.</p>
    <button className="button primary" onClick={retry}>Try again</button>
  </div>
}

function Welcome({ onCreated }: { onCreated: (c: Created) => void }) {
  const [busy, run] = useAction()
  const notify = useNotify()
  return <div className="center-card wide">
    <h1>Catch data changes before they break things</h1>
    <p>DataPulse learns what your data normally looks like, then checks every new file against it:
      missing columns, type changes, nulls, duplicates, new categories and shifted numbers.</p>
    <ol className="how">
      <li><b>Save a baseline</b><span>Upload a file you trust.</span></li>
      <li><b>Check new data</b><span>Upload each new version and see what changed.</span></li>
      <li><b>Know who's affected</b><span>Map the dashboards and models that use each column.</span></li>
    </ol>
    <div className="welcome-actions">
      <CreateProject onCreated={onCreated} />
      <div className="or">or</div>
      <button className="button outline" disabled={!!busy} onClick={() => run('seed', async () => {
        onCreated(await api.seed()); notify.ok('Sample project ready. Try uploading examples/data/orders_drifted.csv.')
      })}>{busy ? <Spinner text="Loading…" /> : <><Icon name="sparkle" size={15} /> Try it with sample data</>}</button>
    </div>
  </div>
}

function CreateProject({ onCreated, autoFocus }: { onCreated: (c: Created) => void; autoFocus?: boolean }) {
  const [name, setName] = useState('')
  const [busy, run] = useAction()
  const valid = name.trim().length >= 2
  const submit = (e: FormEvent) => { e.preventDefault(); if (valid) run('create', async () => onCreated(await api.create(name.trim()))) }
  return <form className="create" onSubmit={submit}>
    <label htmlFor="project-name">Project name</label>
    <div className="row">
      <input id="project-name" autoFocus={autoFocus} value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Orders pipeline" maxLength={80} />
      <button className="button primary" disabled={!valid || !!busy}>{busy ? <Spinner text="Creating…" /> : 'Create'}</button>
    </div>
  </form>
}
