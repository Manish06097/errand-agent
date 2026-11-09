import path from "node:path";
import { fileURLToPath } from "node:url";
import type { NextConfig } from "next";

// The browser only ever talks to this origin; /api/* is proxied to FastAPI. make dev
// sets ERRAND_API_URL from ERRAND_API_PORT, so a moved API port moves the rewrite too.
const apiUrl =
  process.env.ERRAND_API_URL ?? `http://127.0.0.1:${process.env.ERRAND_API_PORT ?? "8000"}`;

const nextConfig: NextConfig = {
  // Pin the workspace root to web/: without it Turbopack walks up and adopts any stray
  // lockfile it finds in a parent directory.
  turbopack: { root: path.dirname(fileURLToPath(import.meta.url)) },
  // next dev otherwise writes an AGENTS.md into web/ whenever it detects a coding agent
  // (BUG-001). The repo carries no tool-generated agent instructions.
  agentRules: false,
  // The dev server refuses its own /_next resources from an origin it does not know, and
  // then the page never hydrates. Playwright and a reviewer both open 127.0.0.1.
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  // The run stream is Server-Sent Events through the /api rewrite; compression would
  // buffer it until the run ends.
  compress: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/api/:path*` }];
  },
};

export default nextConfig;
