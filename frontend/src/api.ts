/** Response shapes of the FastAPI service. */
export interface Status {
  is_running: boolean;
  mode: string;
  current_batch: number;
  total_batches: number;
  active_logs: string[];
  completed_logs: string[];
  total_completed: number;
}

export interface Doc {
  log_id: string;
  image_name: string | null;
  status: string | null;
  progress_percent: number;
  /** "done/total" */
  depth_progress: string;
  lith_progress: string;
}

export interface Dashboard {
  pipeline_state: {
    is_running: boolean;
    mode: string;
    /** "index/total" */
    current_batch: string;
    persisted_state: Record<string, unknown> | null;
  };
  /** Backlog per stream: lag (not yet delivered) + pending (delivered, not acked). */
  queues: {
    pending_depth_tasks: number;
    pending_lith_tasks: number;
    pending_merge_tasks: number;
  };
  queue_breakdown: Record<"depth" | "lithology" | "merge", { lag: number; pending: number }>;
  documents: Doc[];
}

export interface Health {
  status: string;
  redis: string;
}

/**
 * One row of the merger's CSV. It only exists once the merger has finished the log; depths can be
 * null when too few depth markers were read.
 */
export interface Interval {
  top_depth: number | null;
  bottom_depth: number | null;
  lithology: string;
  top_y: number;
  bottom_y: number;
  cluster_id: string | number;
  /** Written by the merger; absent in older CSVs. */
  confidence?: number | null;
}

export class ApiError extends Error {
  constructor(
    public code: number,
    message: string,
  ) {
    super(message);
  }
}

export interface Api {
  label: string;
  health: () => Promise<Health>;
  status: () => Promise<Status>;
  dashboard: () => Promise<Dashboard>;
  startLive: (file?: string) => Promise<unknown>;
  startBatch: (batchSize?: number) => Promise<unknown>;
  reset: (force: boolean) => Promise<unknown>;
  reconcile: () => Promise<unknown>;
  /** Log IDs found in the dataset folder, read by the Vite server (empty if unavailable). */
  logFiles: () => Promise<string[]>;
  /** CSV rows, or null when the merger has not written this log yet. */
  intervals: (logId: string) => Promise<Interval[] | null>;
}

function http(base: string): Api {
  const call = async <T>(path: string, body?: unknown): Promise<T> => {
    const res = await fetch(`${base}${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new ApiError(res.status, data.detail ?? res.statusText);
    return data as T;
  };
  return {
    label: base || "same origin (proxied to the dispatcher)",
    health: () => call("/health"),
    status: () => call("/status"),
    dashboard: () => call("/api/dashboard"),
    startLive: (file) => call("/live/start", file ? { file } : {}),
    startBatch: (batch_size) => call("/batch/start", batch_size === undefined ? {} : { batch_size }),
    reset: (force) => call("/reset", { force }),
    reconcile: () => call("/batch/reconcile", {}),
    logFiles: async () => {
      try {
        const res = await fetch(`/local/logs`);
        const data = res.ok ? await res.json() : [];
        return Array.isArray(data) ? data.map(String) : [];
      } catch {
        return [];
      }
    },
    intervals: async (id) => {
      const res = await fetch(`${base}/api/logs/${encodeURIComponent(id)}/intervals`);
      // 404 / 409: the merger has not written this log's CSV (or the route is not deployed yet).
      if (res.status === 404 || res.status === 409) return null;
      if (!res.ok) throw new ApiError(res.status, res.statusText);
      const text = await res.text();
      return /^\s*[\[{]/.test(text) ? (JSON.parse(text) as Interval[]) : parseCsv(text);
    },
  };
}

/** Splits CSV text into rows of cells. Handles quoted cells (commas, doubled quotes, newlines). */
export function splitCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  const endRow = () => {
    row.push(cell);
    cell = "";
    if (row.some((x) => x !== "")) rows.push(row);
    row = [];
  };
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        cell += '"';
        i++;
      } else if (c === '"') quoted = false;
      else cell += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") {
      row.push(cell);
      cell = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      endRow();
    } else cell += c;
  }
  endRow();
  return rows;
}

/** Rows of the merger's CSV: top_depth,bottom_depth,lithology,confidence,top_y,bottom_y,cluster_id */
function parseCsv(text: string): Interval[] {
  const [head, ...lines] = splitCsv(text);
  if (!head) return [];
  const cols = head.map((c) => c.trim());
  const num = (v: string | undefined) => (v === undefined || v.trim() === "" || Number.isNaN(Number(v)) ? null : Number(v));
  return lines.map((cells) => {
    const get = (name: string) => cells[cols.indexOf(name)];
    return {
      top_depth: num(get("top_depth")),
      bottom_depth: num(get("bottom_depth")),
      lithology: get("lithology") ?? "",
      top_y: Number(get("top_y")),
      bottom_y: Number(get("bottom_y")),
      cluster_id: get("cluster_id") ?? "",
      confidence: num(get("confidence")),
    };
  });
}

/**
 * The real API is the default: calls go to the same origin, and the Vite dev server proxies them to
 * the dispatcher. Set VITE_API_BASE to point somewhere else. Interval CSVs are not served by the
 * dispatcher: the Vite server (vite.config.ts) reads them straight from the merger's output folder.
 */
const base = ((import.meta.env.VITE_API_BASE as string | undefined) ?? "").replace(/\/$/, "");
export const api: Api = http(base);

export const parseProgress = (s: string) => {
  const [d, t] = s.split("/").map(Number);
  return { done: d || 0, total: t || 0 };
};
