import Icon, { Mark } from './Icon.jsx'
import ThemeToggle from './ThemeToggle.jsx'

const NAV = [
  { id: 'dashboard', href: '/dashboard', label: 'Dashboard', icon: 'grid' },
  { id: 'control', href: '/control', label: 'Demo controls', short: 'Controls', icon: 'power' },
]

// Icon rail on desktop (the dealt shell), a bottom bar with labels on a phone. The selected
// item carries the style's one move: a 1px accent line on the nearest edge.
export default function Shell({ current, children }) {
  return (
    <div className="app">
      <nav className="rail" aria-label="Wardline">
        <a className="rail-mark" href="/dashboard" title="Wardline">
          <Mark />
          <span className="rail-name">Wardline</span>
        </a>
        <ul className="rail-items">
          {NAV.map((item) => (
            <li key={item.id}>
              <a href={item.href} className="rail-item" aria-current={item.id === current ? 'page' : undefined} title={item.label}>
                <Icon name={item.icon} size={20} />
                <span className="rail-label">{item.short || item.label}</span>
              </a>
            </li>
          ))}
        </ul>
        <ThemeToggle className="rail-theme" />
      </nav>
      <div className="canvas">{children}</div>
    </div>
  )
}
