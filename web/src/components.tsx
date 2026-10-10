import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { Icon } from './icons'
import { fmtDate, type ColumnStats, type Run } from './api'

export function BrandMark({ large }: { large?: boolean }) {
  return <div className={`brand-mark ${large ? 'large' : ''}`} aria-hidden="true"><span></span><span></span><span></span><span></span></div>
}

/* ---------- theme ---------- */

type Theme = 'light' | 'dark'

function storedTheme(): Theme | null {
  try { const value = localStorage.getItem('datapulse-theme'); return value === 'light' || value === 'dark' ? value : null } catch { return null }
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => storedTheme() || (window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'))
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#0e1512' : '#f3f6f4')
  }, [theme])
  const toggle = () => setTheme(current => {
    const next = current === 'dark' ? 'light' : 'dark'
    try { localStorage.setItem('datapulse-theme', next) } catch { /* per-viewer convenience only */ }
    return next
  })
  return { theme, toggle }
}

/* ---------- toasts ---------- */

export type Toast = { id: number; kind: 'success' | 'error'; text: string }

export function Toasts({ items, dismiss }: { items: Toast[]; dismiss: (id: number) => void }) {
  return <div className="toast-stack" role="status" aria-live="polite">
    {items.map(toast => <ToastItem key={toast.id} toast={toast} dismiss={dismiss} />)}
  </div>
}

function ToastItem({ toast, dismiss }: { toast: Toast; dismiss: (id: number) => void }) {
  useEffect(() => {
    // errors stay until dismissed; confirmations clear themselves
    if (toast.kind === 'error') return
    const timer = window.setTimeout(() => dismiss(toast.id), 4200)
    return () => window.clearTimeout(timer)
  }, [toast, dismiss])
  return <div className={`toast ${toast.kind}`}>
    <span className="toast-icon"><Icon name={toast.kind === 'error' ? 'alert' : 'check'} size={15} /></span>
    <div><b>{toast.kind === 'error' ? 'Something needs attention' : 'Done'}</b><span>{toast.text}</span></div>
    <button className="ghost-icon" aria-label="Dismiss" onClick={() => dismiss(toast.id)}><Icon name="x" size={14} /></button>
  </div>
}

/* ---------- modal ---------- */

export function Modal({ onClose, labelledBy, children }: { onClose: () => void; labelledBy: string; children: ReactNode }) {
  const panel = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    panel.current?.focus()
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    document.body.style.overflow = 'hidden'
    return () => { window.removeEventListener('keydown', onKey); document.body.style.overflow = ''; previous?.focus() }
  }, [onClose])
  return <div className="modal-backdrop" role="presentation" onClick={onClose}>
    <div className="modal" role="dialog" aria-modal="true" aria-labelledby={labelledBy} tabIndex={-1} ref={panel} onClick={e => e.stopPropagation()}>
      <button className="ghost-icon modal-close" aria-label="Close" onClick={onClose}><Icon name="x" size={18} /></button>
      {children}
    </div>
  </div>
}

/* ---------- file dropzone ---------- */

const ACCEPT = '.csv,.json,.jsonl,.ndjson,.parquet,.pq'

function fmtBytes(bytes: number) {
  return bytes < 1024 ? `${bytes} B` : bytes < 1024 ** 2 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`
}

export function Dropzone({ file, onFile, disabled, hint }: { file: File | null; onFile: (file: File | null) => void; disabled?: boolean; hint: string }) {
  const [over, setOver] = useState(false)
  return <label className={`dropzone ${over ? 'over' : ''} ${file ? 'has-file' : ''} ${disabled ? 'disabled' : ''}`}
    onDragOver={e => { e.preventDefault(); if (!disabled) setOver(true) }}
    onDragLeave={() => setOver(false)}
    onDrop={e => { e.preventDefault(); setOver(false); if (!disabled && e.dataTransfer.files[0]) onFile(e.dataTransfer.files[0]) }}>
    <span className="dropzone-icon"><Icon name={file ? 'file' : 'upload'} size={18} /></span>
    <span className="dropzone-text">
      <b>{file ? file.name : over ? 'Drop to attach' : <>Drop a file or <u>browse</u></>}</b>
      <small>{file ? fmtBytes(file.size) : hint}</small>
    </span>
    {file && !disabled && <button type="button" className="ghost-icon" aria-label="Remove file" onClick={e => { e.preventDefault(); onFile(null) }}><Icon name="x" size={14} /></button>}
    <input type="file" accept={ACCEPT} disabled={disabled} onChange={e => { onFile(e.target.files?.[0] || null); e.target.value = '' }} />
  </label>
}

/* ---------- run trend chart ---------- */

export function runTone(run: Pick<Run, 'status' | 'issue_count'>): 'failed' | 'issues' | 'clean' {
  return run.status === 'failed' ? 'failed' : run.issue_count ? 'issues' : 'clean'
}
const TONE_LABEL = { failed: 'Failed', issues: 'Passed with issues', clean: 'Clean' } as const
const TONE_ICON = { failed: 'alert', issues: 'flag', clean: 'check' } as const

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    if (!ref.current) return
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width))
    observer.observe(ref.current)
    return () => observer.disconnect()
  }, [])
  return [ref, width] as const
}

export function RunTrend({ runs, selectedId, onSelect }: { runs: Run[]; selectedId: string; onSelect: (id: string) => void }) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const [hover, setHover] = useState<number | null>(null)
  const series = [...runs].reverse() // oldest -> newest, left to right
  const height = 132, axisGap = 26, top = 10
  const plot = height - axisGap - top
  const max = Math.max(1, ...series.map(run => run.issue_count))
  const slot = series.length ? width / series.length : 0
  const barWidth = Math.max(4, Math.min(30, slot - 6))
  const present = new Set(series.map(runTone))
  const hovered = hover === null ? null : series[hover]

  return <div className="trend">
    <div className="trend-legend" aria-hidden="true">
      {(['failed', 'issues', 'clean'] as const).filter(tone => present.has(tone)).map(tone =>
        <span key={tone}><i className={`swatch ${tone}`}></i>{TONE_LABEL[tone]}</span>)}
      <span className="trend-scale">Findings per run · max {max}</span>
    </div>
    <div className="trend-plot" ref={ref} onMouseLeave={() => setHover(null)}>
      {width > 0 && <svg width={width} height={height} role="img" aria-label={`Findings for the last ${series.length} runs`}>
        <line className="trend-grid" x1={0} x2={width} y1={top} y2={top} />
        <line className="trend-grid" x1={0} x2={width} y1={top + plot / 2} y2={top + plot / 2} />
        <line className="trend-axis" x1={0} x2={width} y1={top + plot} y2={top + plot} />
        {series.map((run, i) => {
          const tone = runTone(run)
          const h = run.issue_count ? Math.max(6, (run.issue_count / max) * plot) : 4
          const x = i * slot + (slot - barWidth) / 2
          const y = top + plot - h
          const r = Math.min(4, barWidth / 2)
          // rounded data-end, square at the baseline
          const d = `M${x},${top + plot} V${y + r} Q${x},${y} ${x + r},${y} H${x + barWidth - r} Q${x + barWidth},${y} ${x + barWidth},${y + r} V${top + plot} Z`
          return <g key={run.id} className={`trend-bar ${tone} ${run.id === selectedId ? 'selected' : ''} ${hover !== null && hover !== i ? 'dim' : ''}`}
            onMouseEnter={() => setHover(i)} onClick={() => onSelect(run.id)}>
            <rect className="hit" x={i * slot} y={0} width={slot} height={height} />
            <path d={d} />
            {run.id === selectedId && <circle className="selected-dot" cx={x + barWidth / 2} cy={top + plot + 9} r={3} />}
          </g>
        })}
        <text className="trend-label" x={0} y={height - 2}>{fmtDate(series[0]?.created_at)}</text>
        {series.length > 1 && <text className="trend-label" x={width} y={height - 2} textAnchor="end">{fmtDate(series[series.length - 1].created_at)}</text>}
      </svg>}
      {hovered && hover !== null && <div className="tooltip" style={{ left: Math.min(Math.max(hover * slot + slot / 2, 90), width - 90) }}>
        <b><span className={`tone-icon ${runTone(hovered)}`}><Icon name={TONE_ICON[runTone(hovered)]} size={12} /></span>{TONE_LABEL[runTone(hovered)]}</b>
        <span>{hovered.issue_count} finding{hovered.issue_count === 1 ? '' : 's'} · {hovered.report.rows.current.toLocaleString()} rows</span>
        <small>{fmtDate(hovered.created_at)} · click to open</small>
      </div>}
    </div>
  </div>
}

export function ToneIcon({ run }: { run: Pick<Run, 'status' | 'issue_count'> }) {
  const tone = runTone(run)
  return <span className={`tone-icon ${tone}`} title={TONE_LABEL[tone]}><Icon name={TONE_ICON[tone]} size={13} /></span>
}
export { TONE_LABEL }

/* ---------- baseline schema ---------- */

export function SchemaTable({ columns }: { columns: Array<[string, ColumnStats]> }) {
  const [query, setQuery] = useState('')
  const shown = columns.filter(([name]) => name.toLowerCase().includes(query.trim().toLowerCase()))
  return <>
    {columns.length > 6 && <input className="search-input" placeholder={`Filter ${columns.length} columns…`} value={query} onChange={e => setQuery(e.target.value)} aria-label="Filter columns" />}
    <div className="table-wrap schema-wrap"><table className="schema-table">
      <thead><tr><th>Column</th><th>Type</th><th>Null rate</th><th className="num">Unique</th></tr></thead>
      <tbody>{shown.map(([name, stats]) => {
        const nullRate = stats.null_rate ?? 0
        return <tr key={name}>
          <td><code>{name}</code></td>
          <td><span className="kind-chip">{stats.kind}</span><small className="dtype">{stats.dtype}</small></td>
          <td><div className="meter" title={`${(nullRate * 100).toFixed(1)}% null`}><span style={{ width: `${Math.max(nullRate * 100, nullRate ? 2 : 0)}%` }}></span></div><small className="meter-value">{(nullRate * 100).toFixed(1)}%</small></td>
          <td className="num">{stats.unique_count?.toLocaleString() ?? '—'}</td>
        </tr>
      })}</tbody>
    </table>{!shown.length && <div className="empty-small">No columns match “{query}”.</div>}</div>
  </>
}

export function CodeBlock({ code, onCopy }: { code: string; onCopy: () => void }) {
  return <div className="code-block"><button className="copy-button" onClick={onCopy}><Icon name="copy" size={13} /> Copy</button><pre>{code}</pre></div>
}
