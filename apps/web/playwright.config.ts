import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  use: {
    baseURL: process.env.FLOWPILOT_WEB_URL || "http://localhost:3000",
    headless: true,
  },
});
