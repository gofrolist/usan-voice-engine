import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Same-origin in production (Caddy proxies /v1 -> api). In dev, proxy /v1 to the
// local API so the session cookie + relative URLs work without CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
  },
});
