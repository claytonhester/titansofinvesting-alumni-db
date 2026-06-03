import { defineConfig } from "vitest/config";
import path from "node:path";

export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "."),
    },
  },
  test: {
    environment: "node",
    include: ["lib/**/*.test.ts", "app/**/*.test.ts"],
    coverage: {
      include: ["lib/chat/**/*.ts"],
      exclude: ["lib/chat/**/*.test.ts", "lib/chat/anthropic.ts"],
      thresholds: { lines: 80, functions: 80, statements: 80 },
    },
  },
});
