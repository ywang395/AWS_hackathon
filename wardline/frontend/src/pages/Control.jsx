// Demo controls: POST /control/outage and /control/restore (CLAUDE.md section 7, demo script step 2).

import { useState } from 'react'
import Shell from '../components/Shell.jsx'
import Icon from '../components/Icon.jsx'
import { getDashboard, isMock, restoreSystems, simulateOutage } from '../api.js'
import { usePoll } from '../lib/hooks.js'
import { clock, clockSeconds } from '../lib/format.js'

export default function Control() {
  const { data, error } = usePoll(getDashboard, 3000)
  const [busy, setBusy] = useState(null)
  const [last, setLast] = useState(null)
  const status = data?.system?.status

  async function run(kind) {
    setBusy(kind)
    try {
      const body = await (kind === 'outage' ? simulateOutage() : restoreSystems())
      setLast({ kind, at: Date.now(), body })
    } catch (err) {
      setLast({ kind, at: Date.now(), error: err.message })
    } finally {
      setBusy(null)
    }
  }

  return (
    <Shell current="control">
      <header className="title-row">
        <div>
          <h1>Demo controls</h1>
          <p className="sub">
            {status === 'down' && <><Icon name="alert" size={16} className="neg" /> Hospital systems are down (simulated) since {clock(data.system.outage_started_at)}.</>}
            {status === 'up' && <><Icon name="check" size={16} className="pos" /> Hospital systems are up.</>}
            {!status && (error ? 'Couldn’t read the system status. Both controls stay available.' : 'Checking system status…')}
          </p>
        </div>
      </header>

      <div className="controls">
        <section className="panel control-card" aria-labelledby="c-outage">
          <h2 id="c-outage">Simulate ransomware</h2>
          <p>Marks hospital systems down, emails every on-shift nurse a secure link, and runs the first routing scan.</p>
          <button type="button" className="btn btn-danger btn-big" disabled={busy !== null || status === 'down'} onClick={() => run('outage')}>
            <Icon name="alert" /> {busy === 'outage' ? 'Starting the outage…' : 'Simulate ransomware'}
          </button>
          {status === 'down' && <p className="hint">Systems are already down. Restore them first to run the demo again.</p>}
        </section>

        <section className="panel control-card" aria-labelledby="c-restore">
          <h2 id="c-restore">Restore systems</h2>
          <p>Marks hospital systems up and publishes SystemRestored for the recovery report.</p>
          <button type="button" className="btn btn-big" disabled={busy !== null || status === 'up'} onClick={() => run('restore')}>
            <Icon name="refresh" /> {busy === 'restore' ? 'Restoring…' : 'Restore systems'}
          </button>
          {status === 'up' && <p className="hint">Systems are already up.</p>}
        </section>
      </div>

      <section className="panel last-action" aria-labelledby="c-last">
        <header>
          <h2 id="c-last">Last action</h2>
          <a className="text-btn" href="/dashboard">Open the dashboard</a>
        </header>
        {!last && <p className="empty">No action from this page yet.</p>}
        {last && <LastAction {...last} />}
      </section>

      {isMock && (
        <p className="notice">
          <Icon name="info" size={16} /> Mock mode: answers come from src/mock.js and no email is sent. The demo nurse link is{' '}
          <a href="/nurse?token=demo-n001">/nurse?token=demo-n001</a> with PIN 1234.
        </p>
      )}
    </Shell>
  )
}

function LastAction({ kind, at, body, error }) {
  const label = kind === 'outage' ? 'Simulate ransomware' : 'Restore systems'
  if (error) {
    return <p className="row-meta neg" role="alert"><Icon name="alert" size={16} /> {label} failed at {clockSeconds(at)}: {error}</p>
  }
  const alerts = Array.isArray(body.alerts) ? body.alerts : []
  const sent = alerts.filter((a) => a.action === 'sent' || a.action === 'resent').length
  const lines = [
    `${label} at ${clockSeconds(at)}: systems ${body.status}.`,
    kind === 'outage' && alerts.length ? `Alert agent: ${sent} of ${alerts.length} nurses emailed.` : null,
    body.alerts_error ? `Alert agent failed: ${body.alerts_error}` : null,
    body.routing ? `Routing agent: scan ran (${body.routing.event_type}).` : null,
    body.routing_error ? `Routing agent failed: ${body.routing_error}` : null,
  ].filter(Boolean)
  return (
    <>
      <ul className="rows">{lines.map((l) => <li key={l} className="row">{l}</li>)}</ul>
      <details className="raw">
        <summary>Response</summary>
        <pre>{JSON.stringify(body, null, 2)}</pre>
      </details>
    </>
  )
}
