import { useEffect, useState, type FormEvent } from 'react'
import { api, type User } from './api'
import { Icon } from './icons'
import { Spinner, useAction, useNotify } from './ui'

/** First run (create the admin account) or sign in. */
export function SignIn({ setupNeeded, onSignedIn }: { setupNeeded: boolean; onSignedIn: (u: User) => void }) {
  const [busy, run] = useAction()
  const [form, setForm] = useState({ name: '', email: '', password: '' })
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm(f => ({ ...f, [k]: e.target.value }))
  const submit = (e: FormEvent) => {
    e.preventDefault()
    run('auth', async () => onSignedIn(setupNeeded
      ? await api.setup(form.name.trim(), form.email, form.password)
      : await api.login(form.email, form.password)))
  }
  return <div className="auth-page">
    <form className="auth-card" onSubmit={submit}>
      <div className="brand"><span className="logo"><Icon name="activity" size={18} /></span>DataPulse</div>
      <h1>{setupNeeded ? 'Create the admin account' : 'Sign in'}</h1>
      {setupNeeded && <p className="muted">You're the first person here. You'll add your teammates next.</p>}
      {setupNeeded && <label>Name<input value={form.name} onChange={set('name')} autoFocus required maxLength={80} /></label>}
      <label>Email<input type="email" value={form.email} onChange={set('email')} autoFocus={!setupNeeded} required autoComplete="username" /></label>
      <label>Password<input type="password" value={form.password} onChange={set('password')} required
        minLength={setupNeeded ? 8 : 1} autoComplete={setupNeeded ? 'new-password' : 'current-password'} />
        {setupNeeded && <small className="muted">At least 8 characters</small>}</label>
      <button className="button primary full" disabled={!!busy}>{busy ? <Spinner text="One moment…" /> : setupNeeded ? 'Create account' : 'Sign in'}</button>
      {!setupNeeded && <p className="muted small">No account? Ask your DataPulse admin to add you.</p>}
    </form>
  </div>
}

/** Change your password; admins also manage who has access. */
export function AccountPage({ user }: { user: User }) {
  return <div className="account">
    <h1>Account & team</h1>
    <p className="muted">Signed in as <b>{user.name}</b> ({user.email}) · {user.role}</p>
    {user.role === 'admin' && <Team me={user} />}
    <ChangePassword />
  </div>
}

function Team({ me }: { me: User }) {
  const notify = useNotify()
  const [busy, run] = useAction()
  const [users, setUsers] = useState<User[]>([])
  const [form, setForm] = useState({ name: '', email: '', password: '', role: 'member' as User['role'] })
  const load = () => api.users().then(setUsers, notify.fail)
  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const add = (e: FormEvent) => {
    e.preventDefault()
    run('add', async () => {
      await api.addUser(form)
      notify.ok(`Added ${form.name}. Send them the address of this dashboard and their temporary password.`)
      setForm({ name: '', email: '', password: '', role: 'member' })
      await load()
    })
  }
  const remove = (u: User) => {
    if (!window.confirm(`Remove ${u.name}? They will be signed out and lose access.`)) return
    run('remove', async () => { await api.removeUser(u.id); notify.ok(`Removed ${u.name}`); await load() })
  }

  return <section className="card">
    <h2>Team</h2>
    <p className="muted">Everyone here sees the same projects, checks and AI triage.</p>
    <ul className="people">{users.map(u => <li key={u.id}>
      <span className="avatar">{u.name.slice(0, 1).toUpperCase()}</span>
      <span><b>{u.name}{u.id === me.id && ' (you)'}</b><small>{u.email}</small></span>
      <em>{u.role}</em>
      {u.id !== me.id && <button className="icon-btn" aria-label={`Remove ${u.name}`} onClick={() => remove(u)}><Icon name="trash" size={15} /></button>}
    </li>)}</ul>
    <form className="add-person" onSubmit={add}>
      <h3>Add a teammate</h3>
      <div className="grid">
        <input placeholder="Name" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} required maxLength={80} />
        <input placeholder="Email" type="email" value={form.email} onChange={e => setForm({ ...form, email: e.target.value })} required />
        <input placeholder="Temporary password (8+ characters)" value={form.password} minLength={8}
          onChange={e => setForm({ ...form, password: e.target.value })} required />
        <select value={form.role} onChange={e => setForm({ ...form, role: e.target.value as User['role'] })} aria-label="Role">
          <option value="member">Member</option><option value="admin">Admin</option>
        </select>
      </div>
      <button className="button primary" disabled={!!busy}>{busy === 'add' ? <Spinner text="Adding…" /> : 'Add teammate'}</button>
    </form>
  </section>
}

function ChangePassword() {
  const notify = useNotify()
  const [busy, run] = useAction()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const submit = (e: FormEvent) => {
    e.preventDefault()
    run('pw', async () => { await api.changePassword(current, next); setCurrent(''); setNext(''); notify.ok('Password changed') })
  }
  return <form className="card change-password" onSubmit={submit}>
    <h2>Change your password</h2>
    <input type="password" placeholder="Current password" value={current} onChange={e => setCurrent(e.target.value)} required autoComplete="current-password" />
    <input type="password" placeholder="New password (8+ characters)" value={next} onChange={e => setNext(e.target.value)} minLength={8} required autoComplete="new-password" />
    <button className="button outline" disabled={!!busy}>{busy ? <Spinner text="Saving…" /> : 'Change password'}</button>
  </form>
}
