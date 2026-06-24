import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')

  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: '0.0.0.0',
      port: Number(process.env.VITE_PORT || env.VITE_PORT || 5173),
      allowedHosts: ['app-replay'],
      watch: { usePolling: true, interval: 1000 },
      proxy: {
        '/api': process.env.VITE_API_PROXY_TARGET || env.VITE_API_PROXY_TARGET || 'http://localhost:8000'
      }
    }
  }
})
