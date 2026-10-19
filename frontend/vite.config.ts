/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: { host: true, proxy: { "/api": process.env.VITE_API_URL ?? "http://localhost:8000" } },
  build: { sourcemap: false, chunkSizeWarningLimit: 300 },
  // slow machines: whole-app tests can take a few seconds each under load
  test: { environment: "jsdom", setupFiles: ["./src/__tests__/setup.ts"], css: false, testTimeout: 20000 },
});
