// Nurse view: opened from the alert email, /nurse?token=…, on the nurse's own phone during an outage.
// POST /chat {token, pin, message}; message "" returns the downtime brief (CLAUDE.md section 7).
//
// Clinical rules (skill override): the downtime banner and "Records as of HH:MM · N min old" never
// scroll away; name, DOB and allergies on every card, never truncated; colour always with an icon
// and words; 48px targets; no animation; STOP is a full-screen interruption closed only by a tap.
// The PIN is held in memory for this page only, never stored.

import { useEffect, useMemo, useRef, useState } from 'react'
import Icon, { Mark } from '../components/Icon.jsx'
import DeliveryPill from '../components/DeliveryPill.jsx'
import ThemeToggle from '../components/ThemeToggle.jsx'
import { ApiError, chat } from '../api.js'
import { useNow } from '../lib/hooks.js'
import { age, clock, dob, isOverdue, localizeTimes, plural, relative, runnerName } from '../lib/format.js'

const QUICK = ['What’s due in the next hour?', 'Which of my patients have allergies?']
// The nurse prompt says "STOP" in capitals for a repeat dose (CLAUDE.md section 12); the model may
// not lead with it, so any capital STOP interrupts.
const isStop = (reply) => /\bSTOP\b/.test(reply || '')

export default function NurseView() {
  const token = useMemo(() => new URLSearchParams(window.location.search).get('token') || '', [])
  const [session, setSession] = useState(null) // { pin, snapshot_ts, brief, patients, deliveries }
  const [lockedReason, setLockedReason] = useState('')

  if (!session) {
    return (
      <PinScreen
        token={token}
        reason={lockedReason}
        onOpen={(pin, data) => {
          setLockedReason('')
          setSession({ pin, ...data })
        }}
      />
    )
  }
  return (
    <Ward
      token={token}
      session={session}
      setSession={setSession}
      onLocked={() => {
        setLockedReason('Your link or PIN stopped working. Enter your PIN again, or ask the charge nurse for a new link.')
        setSession(null)
      }}
    />
  )
}

// ---------------------------------------------------------------------------
// The header that never scrolls away
// ---------------------------------------------------------------------------

function DowntimeHeader({ snapshotTs }) {
  const now = useNow(30_000)
  return (
    <header className="nurse-header">
      <div className="downtime-banner" role="status">
        <Icon name="alert" className="neg" />
        <strong>Downtime mode</strong>
        <span>Paper MAR is the official record</span>
      </div>
      <div className="records-line num">
        <Icon name="clock" />
        <p>{snapshotTs ? <>Records as of <strong>{clock(snapshotTs)}</strong> · {age(snapshotTs, now)}</> : 'Opening the records from the hospital backup'}</p>
        <ThemeToggle />
      </div>
    </header>
  )
}

// ---------------------------------------------------------------------------
// PIN
// ---------------------------------------------------------------------------

function PinScreen({ token, reason, onOpen }) {
  const [pin, setPin] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(reason)

  async function submit(e) {
    e.preventDefault()
    if (!pin.trim()) return setError('Enter your PIN.')
    setBusy(true)
    setError('')
    try {
      const data = await chat(token, pin.trim(), '')
      onOpen(pin.trim(), data)
    } catch (err) {
      setError(err instanceof ApiError && (err.status === 401 || err.status === 400)
        ? 'That link or PIN didn’t work. Check your PIN, or ask the charge nurse for a new link.'
        : 'Couldn’t reach Wardline. Check your signal and try again. Until then, follow the paper downtime procedure.')
      setBusy(false)
    }
  }

  return (
    <div className="nurse">
      <DowntimeHeader />
      <main className="pin">
        <p className="brand"><Mark size={24} /> Wardline</p>
        {!token ? (
          <>
            <h1>This link is incomplete</h1>
            <p>Open the link from your Wardline email again. If it still doesn’t work, ask the charge nurse.</p>
          </>
        ) : (
          <form onSubmit={submit} noValidate>
            <h1>Enter your PIN</h1>
            <p>Your PIN opens your patients’ records from the hospital backup.</p>
            <label htmlFor="pin">PIN</label>
            <input
              id="pin"
              className="pin-input num"
              type="password"
              inputMode="numeric"
              autoComplete="off"
              maxLength={8}
              value={pin}
              onChange={(e) => setPin(e.target.value.replace(/\D/g, ''))}
              aria-describedby={error ? 'pin-error' : undefined}
              aria-invalid={error ? 'true' : undefined}
              autoFocus
            />
            {error && <p id="pin-error" className="form-error" role="alert"><Icon name="alert" /> {error}</p>}
            <button type="submit" className="btn btn-primary btn-big" disabled={busy}>
              <Icon name="lock" /> {busy ? 'Opening…' : 'Open my patients'}
            </button>
          </form>
        )}
      </main>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Patients and chat
// ---------------------------------------------------------------------------

function Ward({ token, session, setSession, onLocked }) {
  const [tab, setTab] = useState('patients')
  const [messages, setMessages] = useState(() => [{ id: 0, role: 'assistant', text: session.reply, ts: Date.now() }])
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState('')
  const [stop, setStop] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [refreshError, setRefreshError] = useState('')
  const inputRef = useRef(null)
  const endRef = useRef(null)

  // Keep the newest message in view, without a smooth scroll (no motion on this screen).
  useEffect(() => { if (tab === 'chat') endRef.current?.scrollIntoView({ block: 'end' }) }, [messages, sending, tab])

  const patients = useMemo(() => [...(session.patients || [])].sort((a, b) => String(a.room).localeCompare(String(b.room))), [session.patients])

  function applyResponse(data) {
    setSession((s) => ({ ...s, snapshot_ts: data.snapshot_ts, patients: data.patients, deliveries: data.deliveries, ...(data.brief ? { brief: data.brief } : {}) }))
  }

  async function send(text) {
    const message = text.trim()
    if (!message || sending) return
    setSending(true)
    setSendError('')
    setMessages((m) => [...m, { id: m.length, role: 'nurse', text: message, ts: Date.now() }])
    setDraft('')
    try {
      const data = await chat(token, session.pin, message)
      applyResponse(data)
      setMessages((m) => [...m, { id: m.length, role: 'assistant', text: data.reply, ts: Date.now(), stop: isStop(data.reply) }])
      if (isStop(data.reply)) setStop(data.reply)
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return onLocked()
      setMessages((m) => m.slice(0, -1))
      setDraft(message)
      setSendError('Couldn’t reach Wardline. Your message wasn’t sent. Check your signal and try again.')
    } finally {
      setSending(false)
    }
  }

  async function refresh() {
    setRefreshing(true)
    setRefreshError('')
    try {
      applyResponse(await chat(token, session.pin, ''))
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return onLocked()
      setRefreshError(`Couldn’t check for updates at ${clock(new Date())}. The records below are still from ${clock(session.snapshot_ts)}.`)
    } finally {
      setRefreshing(false)
    }
  }

  function askAbout(room) {
    setDraft(`Room ${room}: `)
    setTab('chat')
    requestAnimationFrame(() => inputRef.current?.focus())
  }

  return (
    <div className="nurse">
      <DowntimeHeader snapshotTs={session.snapshot_ts} />
      <div className="ward" data-tab={tab}>
        <main className="ward-patients" id="panel-patients" aria-labelledby="patients-title">
          <div className="ward-title">
            <h1 id="patients-title">Your {plural(patients.length, 'patient')}</h1>
            <button type="button" className="btn btn-quiet" onClick={refresh} disabled={refreshing}>
              <Icon name="refresh" /> {refreshing ? 'Checking…' : 'Check for updates'}
            </button>
          </div>
          {refreshError && <p className="form-error" role="alert"><Icon name="alert" /> {refreshError}</p>}
          <DueNext patients={patients} deliveries={session.deliveries || []} />
          <h2 className="section-title">Patients</h2>
          {!patients.length && <p className="empty">No patients are assigned to you in the record. Ask the charge nurse.</p>}
          <ul className="cards">
            {patients.map((p) => (
              <li key={p.patient_id || p.room}>
                <PatientCard patient={p} deliveries={session.deliveries || []} onAsk={() => askAbout(p.room)} />
              </li>
            ))}
          </ul>
        </main>

        <section className="ward-chat" id="panel-chat" aria-labelledby="chat-title">
          <h2 id="chat-title" className="section-title">Ask Wardline</h2>
          <ol className="messages" aria-live="polite">
            {messages.map((m) => <Message key={m.id} message={m} snapshotTs={session.snapshot_ts} />)}
            {sending && <li className="message message-pending">Wardline is checking the records…</li>}
            <li ref={endRef} aria-hidden="true" className="messages-end" />
          </ol>
          <form className="composer" onSubmit={(e) => { e.preventDefault(); send(draft) }}>
            {messages.length < 2 && (
              <div className="quick">
                {QUICK.map((q) => <button key={q} type="button" className="chip" onClick={() => send(q)} disabled={sending}>{q}</button>)}
              </div>
            )}
            {sendError && <p className="form-error" role="alert"><Icon name="alert" /> {sendError}</p>}
            <div className="composer-row">
              <label htmlFor="message" className="visually-hidden">Message to Wardline</label>
              <input
                id="message"
                ref={inputRef}
                className="composer-input"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="Ask or log a dose"
                autoComplete="off"
                enterKeyHint="send"
              />
              <button type="submit" className="btn btn-primary" disabled={sending || !draft.trim()}>
                <Icon name="send" /> Send
              </button>
            </div>
          </form>
        </section>
      </div>

      <nav className="tabbar" aria-label="Nurse view">
        <button type="button" aria-pressed={tab === 'patients'} aria-controls="panel-patients" onClick={() => setTab('patients')}>
          <Icon name="list" /> Patients
        </button>
        <button type="button" aria-pressed={tab === 'chat'} aria-controls="panel-chat" onClick={() => setTab('chat')}>
          <Icon name="chat" /> Ask Wardline
        </button>
      </nav>

      {stop && <StopScreen text={stop} snapshotTs={session.snapshot_ts} onClose={() => setStop(null)} />}
    </div>
  )
}

// The brief: every patient's next dose in time order, with its delivery when one is coming.
function DueNext({ patients, deliveries }) {
  const now = useNow(30_000)
  const doses = patients
    .flatMap((p) => (p.meds || []).map((m) => ({ ...m, room: p.room, name: p.name })))
    .sort((a, b) => String(a.due_at).localeCompare(String(b.due_at)))
  return (
    <section aria-labelledby="due-title">
      <h2 id="due-title" className="section-title">Due next</h2>
      {!doses.length && <p className="empty">No medications are due in the record.</p>}
      <ol className="due">
        {doses.map((m) => {
          const delivery = deliveryFor(deliveries, m.room, m.med)
          const late = isOverdue(m.due_at, now) && delivery?.status !== 'delivered'
          return (
            <li key={`${m.room}-${m.med}-${m.due_at}`} className="due-row">
              <span className="due-time num">{clock(m.due_at)}</span>
              <div className="due-body">
                <p className="due-what"><strong>Room {m.room}</strong> · {m.med} {m.dose} {m.route}</p>
                <p className="due-tags">
                  {m.critical && <span className="pill warn"><Icon name="critical" size={16} /> Critical</span>}
                  {late
                    ? <span className="pill neg"><Icon name="alert" size={16} /> Overdue · {relative(m.due_at, now)}</span>
                    : relative(m.due_at, now) && <span className="muted num">{relative(m.due_at, now)}</span>}
                </p>
                {delivery && <DeliveryLine delivery={delivery} now={now} />}
              </div>
            </li>
          )
        })}
      </ol>
    </section>
  )
}

const deliveryFor = (deliveries, room, med) =>
  deliveries.find((d) => String(d.to_room) === String(room) && String(d.med).toLowerCase() === String(med).toLowerCase())

function DeliveryLine({ delivery, now }) {
  const late = delivery.status !== 'delivered' && isOverdue(delivery.due_at, now)
  const who = delivery.status === 'delivered' ? 'Delivered' : `${runnerName(delivery.runner_id)} is bringing it from ${delivery.from_location}`
  return (
    <p className="delivery-line">
      <DeliveryPill status={delivery.status} late={late} />
      <span>{who}</span>
    </p>
  )
}

function PatientCard({ patient: p, deliveries, onAsk }) {
  const now = useNow(30_000)
  const v = p.vitals || {}
  const allergies = p.allergies || []
  return (
    <article className="card" aria-labelledby={`room-${p.room}`}>
      <header className="card-head">
        <h3 id={`room-${p.room}`}>Room {p.room}</h3>
        {(p.meds || []).some((m) => m.critical) && <span className="pill warn"><Icon name="critical" size={16} /> Critical med</span>}
      </header>
      <p className="identity">
        <strong>{p.name}</strong>
        <span className="num">DOB {dob(p.dob)}</span>
      </p>
      {allergies.length ? (
        <p className="allergy allergy-yes">
          <Icon name="alert" className="neg" />
          <span><span className="allergy-label">Allergies</span> <strong>{allergies.join(', ')}</strong></span>
        </p>
      ) : (
        <p className="allergy allergy-none">
          <Icon name="info" />
          <span>No known allergies</span>
        </p>
      )}
      <ul className="meds">
        {(p.meds || []).map((m) => {
          const delivery = deliveryFor(deliveries, p.room, m.med)
          return (
            <li key={`${m.med}-${m.due_at}`}>
              <p className="med">
                <Icon name="pill" className="muted" />
                <span className="med-name">{m.med} {m.dose} {m.route}</span>
                <span className="med-due num">due {clock(m.due_at)}</span>
              </p>
              {delivery && <DeliveryLine delivery={delivery} now={now} />}
            </li>
          )
        })}
      </ul>
      <dl className="vitals num">
        <div><dt>HR</dt><dd>{v.hr ?? '—'}</dd></div>
        <div><dt>BP</dt><dd>{v.bp ?? '—'}</dd></div>
        <div><dt>SpO₂</dt><dd>{v.spo2 != null ? `${v.spo2}%` : '—'}</dd></div>
        <div><dt>Temp</dt><dd>{v.temp_f != null ? `${v.temp_f} °F` : '—'}</dd></div>
      </dl>
      <p className="muted vitals-time num">Vitals recorded {clock(v.recorded_at)}</p>
      {p.notes && <p className="notes">{p.notes}</p>}
      <button type="button" className="btn btn-quiet btn-wide" onClick={onAsk}>
        <Icon name="chat" /> Ask about room {p.room}
      </button>
    </article>
  )
}

function Message({ message, snapshotTs }) {
  const mine = message.role === 'nurse'
  return (
    <li className={`message ${mine ? 'message-mine' : 'message-wardline'}${message.stop ? ' message-stop' : ''}`}>
      <p className="message-who num">{mine ? 'You' : 'Wardline'} · {clock(message.ts)}</p>
      <p className="message-text">
        {message.stop && <Icon name="stop" className="neg" />}
        {mine ? message.text : localizeTimes(message.text, snapshotTs)}
      </p>
    </li>
  )
}

// ---------------------------------------------------------------------------
// STOP: a full-screen interruption, never a toast
// ---------------------------------------------------------------------------

function StopScreen({ text, snapshotTs, onClose }) {
  const buttonRef = useRef(null)
  useEffect(() => {
    buttonRef.current?.focus()
    const { overflow } = document.body.style
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = overflow }
  }, [])
  const detail = localizeTimes(text.replace(/^\s*STOP[:.!]?\s*/i, ''), snapshotTs)
  return (
    <div className="stop" role="alertdialog" aria-modal="true" aria-labelledby="stop-title" aria-describedby="stop-text">
      <div className="stop-inner">
        <Icon name="stop" size={72} />
        <h2 id="stop-title">STOP</h2>
        <p className="stop-lead">Don’t give this dose.</p>
        <p id="stop-text" className="stop-text">{detail}</p>
        <button ref={buttonRef} type="button" className="btn btn-stop btn-big" onClick={onClose}>
          I understand. I won’t give it.
        </button>
      </div>
    </div>
  )
}
