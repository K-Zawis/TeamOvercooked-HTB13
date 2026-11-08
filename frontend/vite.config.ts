import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv, type Plugin } from "vite";

// The dispatcher (FastAPI) listens on :8000. In dev the browser talks to this server and the
// routes below are forwarded to it, so no CORS setup is needed on the backend.
// /api/logs/<id>/intervals is NOT forwarded: the dispatcher has no such route, so this server
// reads the merger's CSV (<OUTPUT_DIR>/<log_id>.csv) directly from disk.
function intervals(outputDir: string): Plugin {
  const handler = (req: { url?: string }, res: any, next: () => void) => {
    const m = /^\/api\/logs\/([^/]+)\/intervals\/?(?:\?.*)?$/.exec(req.url ?? "");
    if (!m) return next();
    const id = decodeURIComponent(m[1]);
    const file = resolve(outputDir, `${id}.csv`);
    // Reject path traversal; only files directly inside the output folder are served.
    if (id.includes("/") || id.includes("\\") || id.includes("..") || !existsSync(file)) {
      res.statusCode = 404;
      return res.end("not found");
    }
    res.setHeader("Content-Type", "text/csv; charset=utf-8");
    res.setHeader("Cache-Control", "no-store");
    res.end(readFileSync(file));
  };
  return {
    name: "merger-csv-intervals",
    configureServer: (s) => void s.middlewares.use(handler),
    configurePreviewServer: (s) => void s.middlewares.use(handler),
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const target = env.API_PROXY_TARGET || "http://localhost:8000";
  const outputDir = resolve(env.OUTPUT_DIR || "../output");
  const routes = ["/health", "/status", "/live", "/batch", "/reset", "/api"];
  const proxy = Object.fromEntries(routes.map((r) => [r, { target, changeOrigin: true }]));
  return {
    plugins: [intervals(outputDir), react(), tailwindcss()],
    server: { port: 5173, proxy },
    preview: { proxy },
  };
});
