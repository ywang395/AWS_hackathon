import Icon from './Icon.jsx'
import { DELIVERY_STATUS } from '../lib/format.js'

// A delivery's state as icon + words; red only when a dose is overdue.
export default function DeliveryPill({ status, late }) {
  if (status === 'delivered') return <span className="pill pos"><Icon name="check" size={16} /> Delivered</span>
  if (late) return <span className="pill neg"><Icon name="alert" size={16} /> Overdue</span>
  return <span className="pill"><Icon name="route" size={16} /> {DELIVERY_STATUS[status] || status}</span>
}
