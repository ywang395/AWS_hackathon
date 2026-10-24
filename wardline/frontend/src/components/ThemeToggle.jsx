import { useState } from 'react'
import Icon from './Icon.jsx'
import { getTheme, setTheme, THEMES } from '../lib/theme.js'

const LABEL = { system: 'Auto', light: 'Light', dark: 'Dark' }
const ICON = { system: 'monitor', light: 'sun', dark: 'moon' }

// One button that cycles Auto → Light → Dark. The label names the current choice.
export default function ThemeToggle({ className = '' }) {
  const [theme, set] = useState(getTheme)
  const next = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length]
  return (
    <button
      type="button"
      className={`theme-toggle ${className}`}
      onClick={() => { setTheme(next); set(next) }}
      title={`Theme: ${LABEL[theme]}. Switch to ${LABEL[next]}.`}
      aria-label={`Theme: ${LABEL[theme]}. Switch to ${LABEL[next]}.`}
    >
      <Icon name={ICON[theme]} />
      <span className="theme-label">{LABEL[theme]}</span>
    </button>
  )
}
