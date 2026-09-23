import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // API_PROXY_TARGET points the dev server at another backend, e.g.
      // https://openhighway.fly.dev to work on the map against live data
      // without running the backend and a full source sync locally.
      '/api': {
        target: process.env.API_PROXY_TARGET || 'http://localhost:5000',
        changeOrigin: true,
      },
    },
  },
})
