// Theme choice: 'system' follows the device; 'light' and 'dark' are set on <html data-theme>.
// index.html applies the stored choice before the first paint, with the same key.

const KEY = 'wardline-theme'
export const THEMES = ['system', 'light', 'dark']

export function getTheme() {
  try {
    const t = localStorage.getItem(KEY)
    return THEMES.includes(t) ? t : 'system'
  } catch {
    return 'system'
  }
}

export function setTheme(theme) {
  const root = document.documentElement
  if (theme === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
  try { localStorage.setItem(KEY, theme) } catch { /* the choice lasts for this page only */ }
}
