import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run build` writes into the Python package so `tracker serve` can serve the UI.
// `npm run dev` serves the UI on :5173 and proxies /api to `tracker serve` on :8000.
export default defineConfig({
  plugins: [react()],
  // Relative asset URLs, so the built UI works under any path prefix (see API_PATH in the README).
  base: "./",
  build: {
    outDir: "../src/tracker/web/static",
    emptyOutDir: true,
  },
  server: {
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
