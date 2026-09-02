import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev runs on 5180 and proxies /api to the Flask app on 5002. In production the
// Flask app serves this build itself, so everything is same-origin and the proxy
// is irrelevant.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5180,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:5002',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
