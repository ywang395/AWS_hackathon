import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import NurseView from './pages/NurseView.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Control from './pages/Control.jsx'
import './styles/app.css'
import './styles/nurse.css'

// Three routes, chosen by path. S3 serves index.html for every path (template.yaml ErrorDocument),
// so /nurse?token=… from the alert email lands here too.
const ROUTES = { '/nurse': NurseView, '/dashboard': Dashboard, '/control': Control }

function App() {
  const path = window.location.pathname.replace(/\/+$/, '') || '/'
  if (path === '/') {
    window.history.replaceState(null, '', '/dashboard')
    return <Dashboard />
  }
  const Page = ROUTES[path]
  if (Page) return <Page />
  return (
    <main className="not-found">
      <h1>Page not found</h1>
      <p>Nurses: open the link from your Wardline email. Charge nurse: <a href="/dashboard">go to the dashboard</a>.</p>
    </main>
  )
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
