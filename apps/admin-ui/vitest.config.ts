import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    css: false,
    // Unit/component tests live under src/. e2e/ holds Playwright specs that run in
    // CI/P5 (they import @playwright/test, not installed here), so keep them out.
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
  },
});
