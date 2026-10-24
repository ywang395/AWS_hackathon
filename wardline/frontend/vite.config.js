import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// `npm run dev:mock` / `npm run build:mock` run Vite in "mock" mode, which src/api.js reads as
// VITE_MOCK=1: every call is answered by src/mock.js and no backend is needed.
export default defineConfig({
  plugins: [react()],
})
