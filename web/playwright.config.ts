import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "@playwright/test";

// `make e2e` brings up its own stack through scripts/dev.py: the scripted reference
// planner, a fresh data directory, ERRAND_DEV=1, and the ports from ERRAND_WORLD_PORT,
// ERRAND_API_PORT and ERRAND_WEB_PORT. Playwright starts it, runs the suite and stops it.
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const worldPort = process.env.ERRAND_WORLD_PORT ?? "8765";
const apiPort = process.env.ERRAND_API_PORT ?? "8000";
const webPort = process.env.ERRAND_WEB_PORT ?? "3200";

// One data directory for the whole run. Workers re-read this file, so the directory is
// created once and handed on through the environment.
if (!process.env.ERRAND_E2E_DATA_DIR) {
  process.env.ERRAND_E2E_DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "errand-e2e-"));
}
const dataDir = process.env.ERRAND_E2E_DATA_DIR;

export default defineConfig({
  testDir: "./e2e",
  // One stack, one world: the tests share it and run one at a time.
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    trace: "retain-on-failure",
    viewport: { width: 1280, height: 900 },
  },
  webServer: {
    command: "uv run --frozen --project backend python scripts/dev.py",
    cwd: repoRoot,
    url: `http://127.0.0.1:${webPort}/api/health`,
    reuseExistingServer: false,
    timeout: 240_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 15_000 },
    stdout: "pipe",
    stderr: "pipe",
    env: {
      ...(process.env as Record<string, string>),
      ERRAND_DEV: "1",
      ERRAND_MODEL_MODE: "scripted",
      ERRAND_LANGFUSE: "off",
      // The follow-up watcher polls every second here instead of every 15 s.
      ERRAND_FOLLOWUP_WATCH_INTERVAL_S: "1",
      ERRAND_DATA_DIR: dataDir,
      ERRAND_WORLD_DB: path.join(dataDir, "world.sqlite"),
      ERRAND_WORLD_PORT: worldPort,
      ERRAND_API_PORT: apiPort,
      ERRAND_WEB_PORT: webPort,
    },
  },
});
