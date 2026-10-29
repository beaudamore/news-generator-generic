import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Served by FastAPI at /admin in production; in dev, API calls are proxied to the app on :8010.
export default defineConfig({
  plugins: [react()],
  base: '/admin/',
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8020',
      '/health': 'http://127.0.0.1:8020',
      '/mcp': 'http://127.0.0.1:8020',
      '/voices': 'http://127.0.0.1:8020',
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
