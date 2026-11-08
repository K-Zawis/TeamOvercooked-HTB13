import { existsSync, readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv, type Plugin } from "vite";
import { chatApi } from "./vite.chat";

// The dispatcher (FastAPI) listens on :8000. In dev the browser talks to this server and the
// routes below are forwarded to it, so no CORS setup is needed on the backend.
// /api/logs/<id>/intervals is NOT forwarded: the dispatcher has no such route, so this server
// reads the merger's CSV (<OUTPUT_DIR>/<log_id>.csv) directly from disk.
// /local/logs lists the log IDs found in <DATASET_DIR>/bbox_annotation (the dispatcher's own source),
// so the Run panel can show which files exist.
function intervals(outputDir: string, datasetDir: string): Plugin {
  const handler = (req: { url?: string }, res: any, next: () => void) => {
    if ((req.url ?? "").split("?")[0] === "/local/logs") {
      const dirs = [resolve(datasetDir, "bbox_annotation"), datasetDir];
      const dir = dirs.find((d) => existsSync(d) && readdirSync(d).some((f) => f.endsWith(".json")));
      const ids = dir ? readdirSync(dir).filter((f) => f.endsWith(".json")).map((f) => f.slice(0, -5)).sort() : [];
      res.setHeader("Content-Type", "application/json");
      res.setHeader("Cache-Control", "no-store");
      return res.end(JSON.stringify(ids));
    }
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
  const datasetDir = resolve(env.DATASET_DIR || "../dataset");
  const routes =["/health", "/status", "/live", "/batch", "/reset", "/api"];
  const proxy = Object.fromEntries(routes.map((r) => [r, { target, changeOrigin: true }]));
  return {
    plugins: [intervals(outputDir, datasetDir), chatApi(env), react(), tailwindcss()],
    server: { port: 5173, proxy },
    preview: { proxy },
  };
});
