import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// base = '/admin/' is load-bearing, not cosmetic. The gateway serves this bundle
// under /admin, so with the default base of '/' every generated asset URL would
// point at /assets/... - which the gateway does not route and the ALB rule does not
// match. The page would load and then fail to fetch its own JavaScript.
export default defineConfig({
  base: '/admin/',
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // Fingerprinted filenames let the gateway cache assets immutably for a year
    // (see send_file in server.py) while never caching the HTML shell.
    assetsDir: 'assets',
    // Charts and the table library dominate the bundle; splitting them keeps the
    // app chunk small enough that a rebuild of our own code does not invalidate
    // the vendor chunk in the browser cache.
    rollupOptions: {
      output: {
        manualChunks: {
          charts: ['recharts'],
          table: ['@tanstack/react-table'],
        },
      },
    },
    // The charts chunk lands at roughly 570 kB raw / 160 kB gzipped, because
    // recharts 2.x still pulls in lodash. Raised so the build is not permanently
    // warning about a size we have accepted - a warning that fires on every build
    // teaches people to ignore warnings.
    //
    // Accepted rather than fixed because the page is served over the same internal
    // ALB as the API and loads once per session: 160 kB is well under a second and
    // the alternatives all cost more than they save. The real fix, if it ever
    // matters, is recharts 3 (no lodash) or importing only the chart types used.
    chunkSizeWarningLimit: 700,
  },
  server: {
    // Local development proxies the API to a gateway running on 8080, so the
    // dashboard can be worked on without rebuilding the container.
    proxy: {
      '/admin/api': 'http://127.0.0.1:8080',
    },
  },
})
