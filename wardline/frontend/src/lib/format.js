// Display helpers. The API stores times as ISO 8601 UTC; the screens show local 24-hour time
// (CLAUDE.md section 11), which avoids AM/PM mistakes on medication times.

const HHMM = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hour12: false })
const HHMMSS = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
const DAY = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' })

const MINUTE = 60_000
const HALF_DAY = 12 * 60 * MINUTE

export function parseTs(ts) {
  if (!ts) return null
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? null : d
}

export const clock = (ts) => { const d = parseTs(ts); return d ? HHMM.format(d) : '—' }
export const clockSeconds = (ts) => { const d = parseTs(ts); return d ? HHMMSS.format(d) : '—' }

// "38 min", "2 h 5 min", "3 days".
export function duration(ms) {
  const mins = Math.max(0, Math.floor(ms / MINUTE))
  if (mins < 60) return `${mins} min`
  const hours = Math.floor(mins / 60)
  if (hours < 48) return mins % 60 ? `${hours} h ${mins % 60} min` : `${hours} h`
  return `${Math.floor(hours / 24)} days`
}

export function age(ts, now) {
  const d = parseTs(ts)
  return d ? `${duration(now - d)} old` : 'age unknown'
}

// "in 8 min" / "6 min ago"; empty when the time is more than 12 hours away, so a record
// snapshot from another day never reads as "overdue by 300 days".
export function relative(ts, now) {
  const d = parseTs(ts)
  if (!d || Math.abs(d - now) > HALF_DAY) return ''
  const diff = d - now
  if (Math.abs(diff) < MINUTE) return 'now'
  return diff > 0 ? `in ${duration(diff)}` : `${duration(-diff)} ago`
}

export function isOverdue(ts, now) {
  const d = parseTs(ts)
  return Boolean(d && d < now && now - d <= HALF_DAY)
}

// DOB is a calendar date: format it without a time zone shift. "18 Mar 1947".
export function dob(iso) {
  const d = parseTs(`${iso}T00:00:00Z`)
  return d ? DAY.format(d) : iso || '—'
}

// Seeded runners (data/nurses.json). The dashboard contract carries runner_id only; the feed's
// get_runners results add names at runtime (see lib/feed.js), so this is only the fallback.
const KNOWN_RUNNERS = { N005: 'Marcus Webb', N006: 'Priya Nair', marcus: 'Marcus Webb', priya: 'Priya Nair' }

export function runnerName(id, names = {}) {
  if (!id) return 'No runner yet'
  return names[id] || KNOWN_RUNNERS[id] || `Runner ${id}`
}

export const DELIVERY_STATUS = {
  assigned: 'Runner assigned',
  picked_up: 'On the way',
  delivered: 'Delivered',
}

// Agent replies quote times as ISO strings or as "HH:MM UTC" (nurse_agent _display_ts).
// Rewrite both in local time so one screen never mixes two clocks. `ref` dates the HH:MM form.
export function localizeTimes(text, ref) {
  if (!text) return ''
  const base = parseTs(ref) || new Date()
  return text
    .replace(/\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})\b/g, (m) => clock(m))
    .replace(/\b([01]\d|2[0-3]):([0-5]\d) UTC\b/g, (_, h, m) => {
      const d = new Date(base)
      d.setUTCHours(Number(h), Number(m), 0, 0)
      if (d - base > HALF_DAY) d.setUTCDate(d.getUTCDate() - 1)
      if (base - d > HALF_DAY) d.setUTCDate(d.getUTCDate() + 1)
      return HHMM.format(d)
    })
}

export const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`
