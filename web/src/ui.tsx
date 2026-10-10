import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { Icon, type IconName } from './icons'

/* ---------- theme ---------- */

type Theme = 'light' | 'dark'

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light')
  useEffect(() => { document.documentElement.dataset.theme = theme }, [theme])
  const toggle = () => {
    const next = theme === 'dark' ? 'light' : 'dark'
    try { localStorage.setItem('datapulse-theme', next) } catch { /* per-viewer convenience only */ }
    setTheme(next)
  }
  return { theme, toggle }
}

/* ---------- toasts ---------- */

type Toast = { id: number; error: boolean; text: string }
type Notify = { ok: (text: string) => void; fail: (e: unknown) => void }
const ToastContext = createContext<Notify>({ ok: () => {}, fail: () => {} })
export const useNotify = () => useContext(ToastContext)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const dismiss = useCallback((id: number) => setToasts(list => list.filter(t => t.id !== id)), [])
  const push = useCallback((error: boolean, text: string) => {
    const id = Date.now() + Math.random()
    setToasts(list => [...list.slice(-2), { id, error, text }])
    if (!error) window.setTimeout(() => dismiss(id), 4000) // errors stay until dismissed
  }, [dismiss])
  const [notify] = useState<Notify>(() => ({ ok: t => push(false, t), fail: e => push(true, (e as Error).message) }))
  return <ToastContext.Provider value={notify}>
    {children}
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map(t => <div key={t.id} className={`toast ${t.error ? 'error' : ''}`}>
        <Icon name={t.error ? 'alert' : 'check'} size={16} />
        <span>{t.text}</span>
        <button className="icon-btn" aria-label="Dismiss" onClick={() => dismiss(t.id)}><Icon name="x" size={14} /></button>
      </div>)}
    </div>
  </ToastContext.Provider>
}

/** Run an async action with a busy flag; errors become toasts. */
export function useAction() {
  const { fail } = useNotify()
  const [busy, setBusy] = useState<string | null>(null)
  const run = useCallback(async (key: string, action: () => Promise<unknown>) => {
    setBusy(key)
    try { await action() } catch (e) { fail(e) } finally { setBusy(null) }
  }, [fail])
  return [busy, run] as const
}

export async function copyText(text: string, notify: Notify, what = 'Copied to clipboard') {
  try { await navigator.clipboard.writeText(text); notify.ok(what) } catch { notify.fail(new Error('Copy is unavailable here; select the text instead.')) }
}

/* ---------- building blocks ---------- */

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const panel = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    panel.current?.focus()
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => { window.removeEventListener('keydown', onKey); previous?.focus() }
  }, [onClose])
  return <div className="backdrop" onClick={onClose}>
    <div className="modal" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} ref={panel} onClick={e => e.stopPropagation()}>
      <div className="modal-head"><h2>{title}</h2><button className="icon-btn" aria-label="Close" onClick={onClose}><Icon name="x" /></button></div>
      {children}
    </div>
  </div>
}

const ACCEPT = '.csv,.json,.jsonl,.ndjson,.parquet,.pq'

/** Drop or pick a file; the action starts straight away. */
export function Dropzone({ title, hint, busy, disabled, onFile }:
  { title: string; hint: string; busy?: string | false | null; disabled?: boolean; onFile: (file: File) => void }) {
  const [over, setOver] = useState(false)
  const off = disabled || !!busy
  return <label className={`dropzone ${over ? 'over' : ''} ${off ? 'off' : ''}`}
    onDragOver={e => { e.preventDefault(); if (!off) setOver(true) }}
    onDragLeave={() => setOver(false)}
    onDrop={e => { e.preventDefault(); setOver(false); if (!off && e.dataTransfer.files[0]) onFile(e.dataTransfer.files[0]) }}>
    <span className="dropzone-icon">{busy ? <span className="spinner" /> : <Icon name="upload" size={20} />}</span>
    <span><b>{busy || (over ? 'Drop to start' : title)}</b><small>{busy ? 'This usually takes a few seconds' : hint}</small></span>
    <input type="file" accept={ACCEPT} disabled={off} onChange={e => { const f = e.target.files?.[0]; e.target.value = ''; if (f) onFile(f) }} />
  </label>
}

export function Empty({ icon, title, children }: { icon: IconName; title: string; children?: ReactNode }) {
  return <div className="empty"><span className="empty-icon"><Icon name={icon} size={22} /></span><b>{title}</b>{children}</div>
}

export function CodeBlock({ code, label = 'Copy' }: { code: string; label?: string }) {
  const notify = useNotify()
  return <div className="code">
    <button className="copy" onClick={() => copyText(code, notify)}><Icon name="copy" size={13} /> {label}</button>
    <pre>{code}</pre>
  </div>
}

export const Spinner = ({ text }: { text: string }) => <><span className="spinner" /> {text}</>
