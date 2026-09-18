import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  // tsconfig.json sets "jsx": "preserve" for Next's own SWC compiler; esbuild
  // doesn't understand "preserve" and falls back to the classic runtime
  // (React.createElement with no auto-import), so it's pinned explicitly here.
  esbuild: {
    jsx: "automatic",
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["**/*.test.{ts,tsx}"],
    exclude: ["node_modules", ".next"],
    coverage: {
      provider: "v8",
      include: ["components/**", "lib/**", "middleware.ts"],
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "."),
    },
  },
});
