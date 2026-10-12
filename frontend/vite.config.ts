import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// SPA is served by FastAPI at /ui/ (frontend/dist). base stays '/ui/' in both
// dev and build so asset URLs and React Router basename match everywhere.
// Dev proxy: /api → :8000 (FastAPI); /ui → the Vite dev server itself.
const __dirname = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  plugins: [react()],
  base: '/ui/',
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rolldownOptions: {
      output: {
        // recharts is ~half the bundle; give it its own chunk so app code
        // loads fast and the chart vendor cache stays warm across releases.
        codeSplitting: {
          groups: [
            { name: 'vendor-react', test: /node_modules\/(?:react|react-dom|react-router-dom|@tanstack)\// },
            { name: 'vendor-recharts', test: /node_modules\/recharts\// },
          ],
        },
      },
    },
  },
})
