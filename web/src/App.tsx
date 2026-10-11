import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { api, API_ORIGIN, plural, type Created, type Project, type User } from './api'
import { AccountPage, SignIn } from './auth'
import { Icon } from './icons'
import { ProjectView, TABS, type Tab } from './project'
import { Modal, Spinner, ToastProvider, useAction, useNotify, useTheme } from './ui'

export type Route = { id?: string; tab: Tab; run?: string; account?: boolean }

// #/p/<project>/<tab>/<run> — the URL is the source of truth, so links and the back button work.
function parseHash(): Route {
  if (window.location.hash.startsWith('#/account')) return { tab: 'checks', account: true }
  const [, id, tab, run] = window.location.hash.match(/^#\/p\/([\w-]+)(?:\/(\w+))?(?:\/([\w-]+))?/) || []
  const tabName = ['baseline', 'rules'].includes(tab) ? 'settings' : tab // old links to the Standard and Rules tabs
  return { id, tab: TABS.includes(tabName as Tab) ? tabName as Tab : 'sources', run }
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
  return <ToastProvider><Gate /></ToastProvider>
}

/** Nothing but the sign-in screen until someone is signed in. */
function Gate() {
  const [auth, setAuth] = useState<{ setupNeeded: boolean; user: User | null } | 'offline' | null>(null)
  const check = useCallback(() => api.authStatus().then(s => setAuth({ setupNeeded: s.setup_needed, user: s.user }),
    () => setAuth('offline')), [])
  useEffect(() => {
    check()
    const signedOut = () => setAuth(a => a && a !== 'offline' ? { ...a, user: null } : a)
    window.addEventListener('datapulse:signed-out', signedOut)
    return () => window.removeEventListener('datapulse:signed-out', signedOut)
  }, [check])

  if (auth === null) return <div className="loading center"><span className="spinner" /> Loading…</div>
  if (auth === 'offline') return <div className="main"><Offline retry={check} /></div>
  if (!auth.user) return <SignIn setupNeeded={auth.setupNeeded} onSignedIn={user => setAuth({ setupNeeded: false, user })} />
  const signOut = () => api.logout().finally(() => setAuth({ setupNeeded: false, user: null }))
  return <Shell user={auth.user} signOut={signOut} />
}

function Shell({ user, signOut }: { user: User; signOut: () => void }) {
  const { theme, toggle } = useTheme()
  const [route, go] = useRoute()
  const [projects, setProjects] = useState<Project[] | null>(null)
  const [offline, setOffline] = useState(false)
  const [ai, setAi] = useState(false)
  const [creating, setCreating] = useState(false)

  const refresh = useCallback(() => api.projects().then(list => { setProjects(list); setOffline(false) }, () => setOffline(true)), [])
  useEffect(() => { refresh(); api.health().then(h => setAi(h.ai), () => {}) }, [refresh])

  // Land on the first project when the URL doesn't name one that exists.
  useEffect(() => {
    if (!route.account && projects?.length && !projects.some(p => p.id === route.id)) go({ id: projects[0].id, tab: 'sources' }, true)
  }, [projects, route.id, route.account, go])

  const opened = useCallback((created: Created) => {
    setCreating(false)
    refresh().then(() => go({ id: created.id, tab: 'sources' }))
  }, [refresh, go])

  const active = projects?.find(p => p.id === route.id)

  return <div className="shell">
    <aside className="sidebar">
      <div className="brand"><span className="logo"><Icon name="activity" size={18} /></span>DataPulse</div>
      <button className="button primary full" onClick={() => setCreating(true)} disabled={offline}><Icon name="plus" size={15} /> New project</button>
      <nav className="project-list" aria-label="Projects">
        {projects?.map(p => <a key={p.id} href={`#/p/${p.id}/sources`} className={`project-link ${p.id === route.id ? 'active' : ''}`}
          aria-current={p.id === route.id ? 'page' : undefined}>
          <i className={`dot ${p.latest_status || ''}`} title={p.latest_status ? `Last check ${p.latest_status}` : 'No checks yet'} />
          <span><b>{p.name}</b><small>{p.has_baseline ? plural(p.run_count, 'check') : 'Needs a standard'}</small></span>
        </a>)}
      </nav>
      <div className="sidebar-foot">
        <a href="#/account" className={`me ${route.account ? 'active' : ''}`} title="Account & team">
          <span className="avatar">{user.name.slice(0, 1).toUpperCase()}</span>
          <span><b>{user.name}</b><small>{user.role === 'admin' ? 'Admin · manage team' : 'Account'}</small></span>
        </a>
        <div className="foot-actions">
          <a href={`${API_ORIGIN}/docs`} target="_blank" rel="noreferrer"><Icon name="code" size={14} /> API docs</a>
          <button className="icon-btn" onClick={toggle} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}>
            <Icon name={theme === 'dark' ? 'sun' : 'moon'} />
          </button>
          <button className="button ghost small-btn" onClick={signOut}>Sign out</button>
        </div>
      </div>
    </aside>

    <main className="main">
      {route.account ? <AccountPage user={user} />
        : offline ? <Offline retry={refresh} />
        : projects === null ? <div className="loading"><span className="spinner" /> Loading…</div>
        : !projects.length ? <Welcome onCreated={opened} />
        : active && <ProjectView key={active.id} route={{ ...route, id: active.id }} go={go} onChanged={refresh} ai={ai} />}
    </main>

    {creating && <Modal title="New project" onClose={() => setCreating(false)}>
      <p className="muted">A project unifies one kind of data, such as orders or patients, from all the places it lives.</p>
      <CreateProject onCreated={opened} autoFocus />
      <div className="or">or</div>
      <SampleButton onCreated={opened} />
    </Modal>}
  </div>
}

function Offline({ retry }: { retry: () => void }) {
  return <div className="center-card">
    <span className="empty-icon bad"><Icon name="plug" size={22} /></span>
    <h1>Can't reach the DataPulse API</h1>
    <p>Start it from the project folder, then try again:</p>
    <pre className="inline-code">docker compose up</pre>
    <button className="button primary" onClick={retry}>Try again</button>
  </div>
}

function Welcome({ onCreated }: { onCreated: (c: Created) => void }) {
  return <div className="center-card wide">
    <h1>All your data, in one format, checked</h1>
    <p>The same data lives in many places, each in its own shape: a PostgreSQL database, a MySQL store system,
      MongoDB, a Google Sheet. DataPulse connects to all of them, converts each one to a single standard format,
      and combines them into one clean table, checked on every pull.</p>
    <ol className="how">
      <li><b>Connect your sources</b><span>Databases, spreadsheets and file links. Passwords stay on the server; access is read-only.</span></li>
      <li><b>AI maps each one to your standard</b><span>Renamed columns, cents vs. dollars, numbers stored as text. You approve once; every later pull converts automatically.</span></li>
      <li><b>Get one unified, checked table</b><span>Rules and an ML drift model catch problems, and AI sums up what went wrong.</span></li>
    </ol>
    <div className="welcome-actions">
      <CreateProject onCreated={onCreated} />
      <div className="or">or</div>
      <SampleButton onCreated={onCreated} />
    </div>
  </div>
}

/** A sample project with three connected databases holding the same orders in different formats. */
function SampleButton({ onCreated }: { onCreated: (c: Created) => void }) {
  const [busy, run] = useAction()
  const notify = useNotify()
  return <button className="button outline" disabled={!!busy} onClick={() => run('seed', async () => {
    onCreated(await api.seed()); notify.ok('Sample project ready: 3 connected sources. Map the two that need it, then Unify.')
  })}>{busy ? <Spinner text="Loading…" /> : <><Icon name="sparkle" size={15} /> Try it with sample data</>}</button>
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
