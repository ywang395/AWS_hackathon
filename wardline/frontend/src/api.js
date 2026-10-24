// Every call to the backend (CLAUDE.md section 7). Do not change these shapes without telling the team.
//
//   POST /chat                 {token, pin, message} -> {reply, snapshot_ts, brief?, patients, deliveries}
//   GET  /dashboard            -> {system, staff, escalations, deliveries, feed}
//   POST /control/outage       -> fires OutageDetected
//   POST /control/restore      -> fires SystemRestored
//   POST /deliveries/{id}/done -> marks delivered
//   POST /escalations/{id}/resolve -> marks a human escalation handled
//
// VITE_MOCK=1 (or `npm run dev:mock`) answers from src/mock.js with the same shapes.

import { mockRequest } from './mock.js'

export const isMock = import.meta.env.MODE === 'mock' || import.meta.env.VITE_MOCK === '1'

// Where the API lives: in `npm run dev`, VITE_API_URL wins so you can point at a local server;
// in a build, public/config.js decides, so the address can change in S3 without a rebuild.
const BASE = (
  (import.meta.env.DEV && import.meta.env.VITE_API_URL)
  || window.WARDLINE_CONFIG?.apiUrl
  || import.meta.env.VITE_API_URL
  || 'http://localhost:8000'
).replace(/\/+$/, '')

export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

async function request(method, path, body, timeoutMs = 10_000, keepalive = false) {
  let status
  let data
  if (isMock) {
    ;({ status, body: data } = await mockRequest(method, path, body))
  } else {
    let res
    try {
      res = await fetch(BASE + path, {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(timeoutMs),
        keepalive,
      })
    } catch {
      throw new ApiError(0, 'Couldn’t reach Wardline.')
    }
    status = res.status
    data = await res.json().catch(() => ({}))
  }
  if (status < 200 || status >= 300) throw new ApiError(status, data?.error || `Request failed (${status}).`)
  return data
}

// The model can take a while on a tool-heavy question, so chat gets a longer timeout.
export const chat = (token, pin, message = '') => request('POST', '/chat', { token, pin, message }, 45_000)
export const getDashboard = () => request('GET', '/dashboard')
export const simulateOutage = () => request('POST', '/control/outage', undefined, 30_000)
export const restoreSystems = () => request('POST', '/control/restore')
export const resolveEscalation = (escalationId) =>
  request('POST', `/escalations/${encodeURIComponent(escalationId)}/resolve`)
// keepalive lets a pending Mark delivered finish while the page closes.
export const markDelivered = (requestId, { keepalive = false } = {}) =>
  request('POST', `/deliveries/${encodeURIComponent(requestId)}/done`, undefined, 10_000, keepalive)
