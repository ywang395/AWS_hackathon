import { useCallback, useEffect, useRef, useState } from 'react'

// The current time, re-read every `ms`, for ages and countdowns that tick in place.
export function useNow(ms = 30_000) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms)
    return () => clearInterval(id)
  }, [ms])
  return now
}

// Polls `load` every `ms`. Keeps the last good data on screen when a poll fails, so a dropped
// request never blanks the dashboard; `error` and `updatedAt` say how fresh it is.
export function usePoll(load, ms = 3000) {
  const [state, setState] = useState({ data: null, error: null, updatedAt: null })
  const busy = useRef(false)

  const refresh = useCallback(async () => {
    if (busy.current) return
    busy.current = true
    try {
      const data = await load()
      setState({ data, error: null, updatedAt: Date.now() })
    } catch (error) {
      setState((s) => ({ ...s, error }))
    } finally {
      busy.current = false
    }
  }, [load])

  useEffect(() => {
    refresh()
    const id = setInterval(() => { if (!document.hidden) refresh() }, ms)
    return () => clearInterval(id)
  }, [refresh, ms])

  return { ...state, refresh }
}
