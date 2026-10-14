import { useState } from 'react'
import type { ColumnChart } from './api'

const pct = (p: number) => `${(p * 100).toFixed(p > 0 && p < 0.1 ? 1 : 0)}%`
const num = (v: number) => Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 })
  : v.toLocaleString(undefined, { maximumFractionDigits: Math.abs(v) < 10 ? 2 : 1 })

/** Bucket i of a histogram whose edges are the baseline's deciles: "< e0", "e0–e1", …, "> eN". */
function bucketLabel(edges: number[], i: number) {
  if (i === 0) return `under ${num(edges[0])}`
  if (i === edges.length) return `over ${num(edges[edges.length - 1])}`
  return `${num(edges[i - 1])} – ${num(edges[i])}`
}

function Legend() {
  return <div className="chart-legend">
    <span><i className="sw base" />Baseline</span><span><i className="sw new" />New file</span>
  </div>
}

/** Share of values in each of the baseline's buckets, baseline vs. the new file, on one scale. */
export function BeforeAfter({ column, chart }: { column: string; chart: NonNullable<ColumnChart['hist']> }) {
  const [hover, setHover] = useState<number | null>(null)
  const n = chart.baseline.length
  const W = 560, H = 170, top = 14, bottom = 30, plot = H - top - bottom
  const max = Math.max(...chart.baseline, ...chart.current, 0.01)
  const slot = W / n, gap = 2, bar = Math.max(3, Math.min(22, (slot - 10) / 2))
  const y = (p: number) => top + plot - (p / max) * plot
  const path = (x: number, p: number) => {
    if (p <= 0) return ''
    const h = Math.max(2, (p / max) * plot), r = Math.min(4, bar / 2, h)
    const yt = top + plot - h
    return `M${x},${top + plot} V${yt + r} Q${x},${yt} ${x + r},${yt} H${x + bar - r} Q${x + bar},${yt} ${x + bar},${yt + r} V${top + plot} Z`
  }
  const peak = chart.current.indexOf(Math.max(...chart.current))
  return <figure className="chart">
    <Legend />
    <div className="chart-plot" onMouseLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img"
        aria-label={`${column}: share of values per range, baseline vs new file. Largest share in the new file: ${bucketLabel(chart.edges, peak)} (${pct(chart.current[peak])}).`}>
        <line className="grid" x1={0} x2={W} y1={y(max)} y2={y(max)} />
        <text className="tick" x={0} y={y(max) - 4}>{pct(max)}</text>
        <line className="axis" x1={0} x2={W} y1={top + plot} y2={top + plot} />
        {chart.baseline.map((b, i) => {
          const x0 = i * slot + (slot - 2 * bar - gap) / 2
          return <g key={i} className={hover !== null && hover !== i ? 'dim' : ''} onMouseEnter={() => setHover(i)}>
            <rect className="hit" x={i * slot} y={0} width={slot} height={H} />
            <path className="bar base" d={path(x0, b)} />
            <path className="bar new" d={path(x0 + bar + gap, chart.current[i])} />
          </g>
        })}
        <text className="tick" x={2} y={H - 8}>{bucketLabel(chart.edges, 0)}</text>
        <text className="tick" x={W - 2} y={H - 8} textAnchor="end">{bucketLabel(chart.edges, n - 1)}</text>
        {chart.current[peak] > 0.3 && <text className="direct" x={peak * slot + slot / 2} y={y(chart.current[peak]) - 5}
          textAnchor="middle">{pct(chart.current[peak])}</text>}
      </svg>
      {hover !== null && <div className="chart-tip" style={{ left: `${((hover + 0.5) / n) * 100}%` }}>
        <b>{bucketLabel(chart.edges, hover)}</b>
        <span><i className="sw base" />Baseline {pct(chart.baseline[hover])}</span>
        <span><i className="sw new" />New file {pct(chart.current[hover])}</span>
      </div>}
    </div>
    <figcaption>Share of <code>{column}</code> values in each range. The ranges are the baseline's deciles, so a normal file has about 10% in each.</figcaption>
  </figure>
}

/** Empty-value rate, baseline vs. the new file. */
export function NullRates({ column, rates }: { column: string; rates: [number, number | null] }) {
  const rows: Array<[string, string, number | null]> = [['Baseline', 'base', rates[0]], ['New file', 'new', rates[1]]]
  const max = Math.max(rates[0], rates[1] ?? 0, 0.05)
  return <figure className="chart" aria-label={`Empty values in ${column}: baseline ${pct(rates[0])}, new file ${rates[1] === null ? 'column missing' : pct(rates[1])}`}>
    <div className="hbars">
      {rows.map(([label, cls, v]) => <div key={label} className="hbar">
        <span>{label}</span>
        <span className="track"><i className={cls} style={{ width: `${((v ?? 0) / max) * 100}%` }} /></span>
        <b>{v === null ? 'missing' : pct(v)}</b>
      </div>)}
    </div>
    <figcaption>Share of empty values in <code>{column}</code>.</figcaption>
  </figure>
}
