/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Slice-1 of issue #639 — AG-UI reactive rewrite.
//
// Build output goes to `../static/react-dist/` inside the `src/status_site/`
// Python package. The alice-status container (python:3.12-alpine, see
// `sandbox/docker-compose.yml`) bind-mounts the whole package read-only,
// so committed dist files land in the container unchanged. The Python
// server serves that directory under `/ui/` via `_react_dist_file` in
// `server.py`.
//
// base: '/ui/' — the built `index.html` references assets as
// `/ui/assets/…`. This is the public URL prefix the server serves
// `react-dist/` under. Keep aligned with the `_ui_dist_file` route.
//
// We avoid any `define`/`loader` tricks that would need extra server
// rewriting; the client fetches `/events?section=running_now` directly
// from the same origin.

export default defineConfig({
  plugins: [react()],
  base: '/ui/',
  build: {
    // Where the Python package picks up the built bundle. Checked in
    // so the alpine-based container (which has no npm/vite) can serve
    // it straight from the bind-mount.
    outDir: '../static/react-dist',
    emptyOutDir: true,
    sourcemap: false,
    // Pinning entry names makes the server-side allowlist tractable.
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
