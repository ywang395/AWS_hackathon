// One outline icon set, 24px grid at 1.5px stroke. Icons always sit beside words: colour and
// icon together carry a status, never colour alone (skill override, clinical rules).

const PATHS = {
  alert: 'M12 3.5 2.5 20h19L12 3.5ZM12 10v4.5M12 17.25v.01',
  check: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM8 12.5l2.75 2.75L16 10',
  clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 7v5l3 2',
  stop: 'M8.3 2.5h7.4l5.8 5.8v7.4l-5.8 5.8H8.3l-5.8-5.8V8.3l5.8-5.8ZM12 7.5v5.5M12 16.25v.01',
  info: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 11v5.5M12 7.75v.01',
  critical: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 7v6M12 16.25v.01',
  route: 'M5 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4ZM19 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4ZM7 17h7.5a3.5 3.5 0 0 0 0-7h-5a3.5 3.5 0 0 1 0-7H17',
  send: 'M4 12 20 4l-4 16-4.5-6.5L4 12ZM11.5 13.5 20 4',
  person: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM4.5 20.5a7.5 7.5 0 0 1 15 0',
  list: 'M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01',
  chat: 'M4 5.5h16v10H9.5L5 19.5v-4H4v-10Z',
  lock: 'M6 10.5h12v10H6v-10ZM8.5 10.5V7.5a3.5 3.5 0 0 1 7 0v3',
  refresh: 'M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4h-4',
  grid: 'M4 4h7v7H4V4ZM13 4h7v4h-7V4ZM13 10h7v10h-7V10ZM4 13h7v7H4v-7Z',
  power: 'M12 3v8M7.05 6.05a8 8 0 1 0 9.9 0',
  escalate: 'M12 20V5M6 11l6-6 6 6',
  pill: 'M10.5 20.5a4.6 4.6 0 0 1-6.5-6.5l6-6a4.6 4.6 0 0 1 6.5 6.5l-6 6ZM7 11l6 6',
  activity: 'M3 12h4l2.5-6 5 12L17 12h4',
}

export default function Icon({ name, size = 20, className = '', title }) {
  return (
    <svg
      className={`icon ${className}`}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={title ? undefined : 'true'}
      role={title ? 'img' : undefined}
    >
      {title && <title>{title}</title>}
      <path d={PATHS[name]} />
    </svg>
  )
}

// The Wardline mark: a ward (the square) with the line through it.
export function Mark({ size = 28 }) {
  return (
    <svg className="mark" width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="5" y="5" width="14" height="14" rx="2" stroke="currentColor" strokeWidth="1.5" />
      <path d="M2 12h20" stroke="var(--accent)" strokeWidth="2" />
    </svg>
  )
}
