import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served from ``/app/`` by status_site/server.py. Relative paths keep
// the bundle portable if we ever move the mount point.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Single chunk keeps the dist footprint small and easy to audit;
    // this bundle is tiny.
    sourcemap: false,
  },
});
