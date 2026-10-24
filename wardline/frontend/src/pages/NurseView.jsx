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

// A dose log in one fixed form, so the nurse agent's check_recent_doses + log_event run on exactly
// what the nurse confirmed (identity included), not on a free-text guess. Still a plain /chat message.
const doseMessage = (p, m, at, checkedBy) =>
  `Log med_given for room ${p.room}: ${m.med} ${m.dose} ${m.route} given at ${at}. ` +
  `Patient identity checked on the wristband: ${p.name}, DOB ${p.dob}.` +
  (checkedBy ? ` Double-checked by ${checkedBy}.` : '')
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
  const [logging, setLogging] = useState(null) // { patient, med } while the dose sheet is open
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
    if (!message || sending) return false
    setSending(true)
    setSendError('')
    setMessages((m) => [...m, { id: m.length, role: 'nurse', text: message, ts: Date.now() }])
    setDraft('')
    try {
      const data = await chat(token, session.pin, message)
      applyResponse(data)
      setMessages((m) => [...m, { id: m.length, role: 'assistant', text: data.reply, ts: Date.now(), stop: isStop(data.reply) }])
      if (isStop(data.reply)) setStop(data.reply)
      return true
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) { onLocked(); return false }
      setMessages((m) => m.slice(0, -1))
      setDraft(message)
      setSendError('Couldn’t reach Wardline. Your message wasn’t sent. Check your signal and try again.')
      return false
    } finally {
      setSending(false)
    }
  }

  // Confirmed in the dose sheet: send the fixed-form log and show the answer (or STOP) in chat.
  async function logDose(patient, med, checkedBy) {
    setTab('chat')
    const ok = await send(doseMessage(patient, med, clock(new Date()), checkedBy.trim()))
    if (ok) setLogging(null)
    return ok
  }

  const openLog = (room, medName) => {
    const patient = patients.find((p) => String(p.room) === String(room))
    const med = patient?.meds?.find((m) => m.med === medName)
    if (patient && med) setLogging({ patient, med })
  }

  // A room named in a reply opens that patient's card, so allergies and DOB are one tap away.
  function showRoom(room) {
    setTab('patients')
    requestAnimationFrame(() => {
      const card = document.getElementById(`card-${room}`)
      card?.scrollIntoView({ block: 'start' })
      card?.focus()
    })
  }
  const myRooms = useMemo(() => new Set(patients.map((p) => String(p.room))), [patients])

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
          <DueNext patients={patients} deliveries={session.deliveries || []} onLog={openLog} />
          <h2 className="section-title">Patients</h2>
          {!patients.length && <p className="empty">No patients are assigned to you in the record. Ask the charge nurse.</p>}
          <ul className="cards">
            {patients.map((p) => (
              <li key={p.patient_id || p.room}>
                <PatientCard patient={p} deliveries={session.deliveries || []} onAsk={() => askAbout(p.room)} onLog={(m) => setLogging({ patient: p, med: m })} />
              </li>
            ))}
          </ul>
        </main>

        <section className="ward-chat" id="panel-chat" aria-labelledby="chat-title">
          <h2 id="chat-title" className="section-title">Ask Wardline</h2>
          <ol className="messages" aria-live="polite">
            {messages.map((m) => <Message key={m.id} message={m} snapshotTs={session.snapshot_ts} rooms={myRooms} onRoom={showRoom} />)}
            {sending && <li className="message message-pending">Wardline is checking the records…</li>}
            <li ref={endRef} aria-hidden="true" className="messages-end" />
          </ol>
          <form className="composer" onSubmit={(e) => { e.preventDefault(); send(draft) }}>
            {messages.length < 2 && (
              <p className="guide">Ask about your patients in your own words. To record a dose, use Log dose on the patient’s card.</p>
            )}
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

      <nav className="tabbar" aria-label="Nurse view" role="tablist">
        <button type="button" role="tab" aria-selected={tab === 'patients'} aria-controls="panel-patients" onClick={() => setTab('patients')}>
          <Icon name="list" /> Patients
        </button>
        <button type="button" role="tab" aria-selected={tab === 'chat'} aria-controls="panel-chat" onClick={() => setTab('chat')}>
          <Icon name="chat" /> Ask Wardline
        </button>
      </nav>

      {logging && !stop && (
        <DoseSheet
          patient={logging.patient}
          med={logging.med}
          sending={sending}
          error={sendError}
          onConfirm={(checkedBy) => logDose(logging.patient, logging.med, checkedBy)}
          onCancel={() => setLogging(null)}
        />
      )}
      {stop && <StopScreen text={stop} snapshotTs={session.snapshot_ts} onClose={() => setStop(null)} />}
    </div>
  )
}

// The brief: every patient's next dose in time order, with its delivery when one is coming.
function DueNext({ patients, deliveries, onLog }) {
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
                <button type="button" className="btn btn-quiet btn-log" onClick={() => onLog(m.room, m.med)}>
                  <Icon name="check" /> Log dose given
                </button>
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

function PatientCard({ patient: p, deliveries, onAsk, onLog }) {
  const now = useNow(30_000)
  const v = p.vitals || {}
  const allergies = p.allergies || []
  return (
    <article className="card" id={`card-${p.room}`} tabIndex={-1} aria-labelledby={`room-${p.room}`}>
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
              <button type="button" className="btn btn-quiet btn-log" onClick={() => onLog(m)}>
                <Icon name="check" /> Log dose given
              </button>
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

function Message({ message, snapshotTs, rooms, onRoom }) {
  const mine = message.role === 'nurse'
  return (
    <li className={`message ${mine ? 'message-mine' : 'message-wardline'}${message.stop ? ' message-stop' : ''}`}>
      <p className="message-who num">{mine ? 'You' : 'Wardline'} · {clock(message.ts)}</p>
      <p className="message-text">
        {message.stop && <Icon name="stop" className="neg" />}
        {mine ? message.text : linkRooms(localizeTimes(message.text, snapshotTs), rooms, onRoom)}
      </p>
    </li>
  )
}

// "room 304" / "rooms 301 and 302" in a reply become buttons to those patients' cards (this nurse's
// rooms only; any other room number stays plain text).
function linkRooms(text, rooms, onRoom) {
  const out = []
  let last = 0
  for (const m of text.matchAll(/\b[Rr]ooms?\s+\d{3}(?:(?:,\s*|\s+and\s+)\d{3})*/g)) {
    out.push(text.slice(last, m.index))
    let cursor = 0
    for (const n of m[0].matchAll(/\d{3}/g)) {
      out.push(m[0].slice(cursor, n.index))
      out.push(rooms.has(n[0])
        ? <button key={`${m.index}-${n.index}`} type="button" className="room-link" onClick={() => onRoom(n[0])}>{n[0]}</button>
        : n[0])
      cursor = n.index + n[0].length
    }
    out.push(m[0].slice(cursor))
    last = m.index + m[0].length
  }
  out.push(text.slice(last))
  return out
}

// Modal focus: lock the page behind, keep Tab inside the dialog, focus `first` on open.
function useModal(ref, first) {
  useEffect(() => {
    first.current?.focus()
    const { overflow } = document.body.style
    document.body.style.overflow = 'hidden'
    const trap = (e) => {
      if (e.key !== 'Tab' || !ref.current) return
      const items = [...ref.current.querySelectorAll('button, input, [href], [tabindex]:not([tabindex="-1"])')].filter((el) => !el.disabled)
      if (!items.length) return
      const [a, z] = [items[0], items[items.length - 1]]
      if (e.shiftKey && document.activeElement === a) { e.preventDefault(); z.focus() }
      else if (!e.shiftKey && document.activeElement === z) { e.preventDefault(); a.focus() }
      else if (!ref.current.contains(document.activeElement)) { e.preventDefault(); a.focus() }
    }
    document.addEventListener('keydown', trap)
    return () => { document.body.style.overflow = overflow; document.removeEventListener('keydown', trap) }
  }, [ref, first])
}

// ---------------------------------------------------------------------------
// Log dose: the two-identifier check before anything is written to the shared log
// ---------------------------------------------------------------------------

function DoseSheet({ patient: p, med, sending, error, onConfirm, onCancel }) {
  const ref = useRef(null)
  const checkRef = useRef(null)
  const [checked, setChecked] = useState(false)
  const [checkedBy, setCheckedBy] = useState('')
  const now = useNow(15_000)
  useModal(ref, checkRef)
  useEffect(() => {
    const esc = (e) => { if (e.key === 'Escape' && !sending) onCancel() }
    document.addEventListener('keydown', esc)
    return () => document.removeEventListener('keydown', esc)
  }, [onCancel, sending])
  const allergies = p.allergies || []
  return (
    <div className="sheet-backdrop">
      <div ref={ref} className="sheet" role="dialog" aria-modal="true" aria-labelledby="sheet-title">
        <h2 id="sheet-title">Log dose given</h2>
        <dl className="sheet-facts">
          <div><dt>Patient</dt><dd><strong>{p.name}</strong></dd></div>
          <div><dt>Date of birth</dt><dd className="num">{dob(p.dob)}</dd></div>
          <div><dt>Room</dt><dd className="num">{p.room}</dd></div>
          <div><dt>Medication</dt><dd><strong>{med.med} {med.dose} {med.route}</strong></dd></div>
          <div><dt>Due</dt><dd className="num">{clock(med.due_at)}</dd></div>
          <div><dt>Given at</dt><dd className="num">{clock(now)} (now)</dd></div>
        </dl>
        {allergies.length
          ? <p className="allergy allergy-yes"><Icon name="alert" className="neg" /><span><span className="allergy-label">Allergies</span> <strong>{allergies.join(', ')}</strong></span></p>
          : <p className="allergy allergy-none"><Icon name="info" /><span>No known allergies</span></p>}
        <label className="check-row">
          <input ref={checkRef} type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} />
          <span>I checked the name and date of birth on the wristband.</span>
        </label>
        <label className="field-label" htmlFor="checked-by">Double-checked by <span className="muted">(optional)</span></label>
        <input id="checked-by" className="text-input" value={checkedBy} onChange={(e) => setCheckedBy(e.target.value)} autoComplete="off" />
        {error && <p className="form-error" role="alert"><Icon name="alert" /> {error}</p>}
        <p className="hint">Wardline checks the shared log for a recent dose first. The paper MAR stays the official record.</p>
        <div className="sheet-actions">
          <button type="button" className="btn btn-primary btn-big" disabled={!checked || sending} onClick={() => onConfirm(checkedBy)}>
            <Icon name="check" /> {sending ? 'Checking the log…' : 'Confirm dose given'}
          </button>
          <button type="button" className="btn btn-quiet btn-big" disabled={sending} onClick={onCancel}>Cancel</button>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// STOP: a full-screen interruption, never a toast
// ---------------------------------------------------------------------------

function StopScreen({ text, snapshotTs, onClose }) {
  const ref = useRef(null)
  const buttonRef = useRef(null)
  useModal(ref, buttonRef)
  const detail = localizeTimes(text.replace(/^\s*STOP[:.!]?\s*/i, ''), snapshotTs)
  return (
    <div ref={ref} className="stop" role="alertdialog" aria-modal="true" aria-labelledby="stop-title" aria-describedby="stop-text">
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
