// Small stroke icon set (24px grid, inherits currentColor) so the UI doesn't depend on font glyphs.
const PATHS: Record<string, string> = {
  plus: 'M12 5v14M5 12h14',
  upload: 'M12 16V4m0 0-4 4m4-4 4 4M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2',
  check: 'M5 12.5 10 17l9-10',
  alert: 'M12 8v5m0 3.5v.5M10.3 3.9 2.4 17.6A2 2 0 0 0 4.1 20.6h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z',
  x: 'M6 6l12 12M18 6 6 18',
  sun: 'M12 3v2m0 14v2M5.6 5.6 7 7m10 10 1.4 1.4M3 12h2m14 0h2M5.6 18.4 7 17M17 7l1.4-1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
  moon: 'M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z',
  plug: 'M9 7V3m6 4V3M7 7h10v4a5 5 0 0 1-10 0V7Zm5 9v5',
  copy: 'M9 9h10v10H9zM5 15V5h10',
  sparkle: 'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Z',
  database: 'M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3Zm0 0v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3',
  activity: 'M3 12h4l3-8 4 16 3-8h4',
  flag: 'M5 21V4m0 0h11l-2 4 2 4H5',
  external: 'M14 4h6v6m0-6L10 14M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5',
  code: 'M8 7l-5 5 5 5m8-10 5 5-5 5',
  info: 'M12 11v5m0-8.5v.5M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
  trash: 'M4 7h16M10 11v6m4-6v6M6 7l1 13h10l1-13M9 7V4h6v3',
  wrench: 'M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.5-.5-.5-2.5 2.5-2.5Z',
  download: 'M12 4v12m0 0-4-4m4 4 4-4M4 18v1a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-1',
  shield: 'M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6l-8-3Z',
  arrow: 'M4 12h15m0 0-5-5m5 5-5 5',
  chevronDown: 'm6 9 6 6 6-6',
  more: 'M5 11.5a.5.5 0 1 0 0 1 .5.5 0 0 0 0-1Zm7 0a.5.5 0 1 0 0 1 .5.5 0 0 0 0-1Zm7 0a.5.5 0 1 0 0 1 .5.5 0 0 0 0-1Z',
}

export type IconName = keyof typeof PATHS

/** The DataPulse mark: a row of records with one pulse in it. */
export function Logo({ size = 20 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
    <g fill="currentColor"><circle cx="3" cy="12" r="1.3" /><circle cx="6.5" cy="12" r="1.3" /><circle cx="21" cy="12" r="1.3" /></g>
    <path d="M10 12l2.5-6.5 3.5 13 1.5-6.5" fill="none" stroke="currentColor" strokeWidth={2.2}
      strokeLinecap="round" strokeLinejoin="round" />
  </svg>
}

export function Brand() {
  return <div className="brand"><span className="logo"><Logo /></span><span className="wordmark">Data<em>Pulse</em></span></div>
}

export function Icon({ name, size = 16, className }: { name: IconName; size?: number; className?: string }) {
  return <svg className={`icon ${className || ''}`} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={PATHS[name]} /></svg>
}
