import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api, type Doc, type Interval, type Status } from "./api";
import { HoverCard, LithColumn, type BedHover } from "./Column";
import { Chat } from "./Chat";
import { GeoMap } from "./GeoMap";
import { HatchDefs } from "./Hatch";
import { FALLBACK_STYLE, buildStyles, ft, normLith, rockType } from "./lith";
import { usePipeline } from "./usePipeline";

const STATUSES = ["PENDING", "PROCESSING", "MERGING", "COMPLETED", "FAILED", "CORRUPTED"];

const statusColor = (s?: string | null) => {
  switch ((s ?? "").toUpperCase()) {
    case "COMPLETED":
      return "#3fae6a";
    case "PROCESSING":
      return "#e8c82e";
    case "MERGING":
      return "#d98b2b";
    case "FAILED":
    case "CORRUPTED":
      return "#e05a5a";
    default:
      return "#4a8fd8";
  }
};
const cap = (s?: string | null) => (s ? s.charAt(0) + s.slice(1).toLowerCase() : "—");
const running = (s?: string | null) => /processing|merging/i.test(s ?? "");

/* ---------------- left column ---------------- */

function Listbox({
  title,
  items,
  hidden,
  toggle,
}: {
  title: string;
  items: Array<{ key: string; label: string; color?: string; mono?: boolean }>;
  hidden: Set<string>;
  toggle: (key: string) => void;
}) {
  return (
    <div className="group">
      <div className="gh">{title}</div>
      <ul className="list">
        {items.map((it) => (
          <li key={it.key}>
            <label>
              <input type="checkbox" checked={!hidden.has(it.key)} onChange={() => toggle(it.key)} />
              {it.color && <i style={{ background: it.color }} />}
              <span className={it.mono ? "mono" : ""}>{it.label}</span>
            </label>
          </li>
        ))}
        {items.length === 0 && <li className="none">none</li>}
      </ul>
    </div>
  );
}

type Msg = { kind: "ok" | "bad"; text: string } | null;

function RunControls({ status, onDone }: { status: Status | null; onDone: () => void }) {
  const [file, setFile] = useState("");
  const [size, setSize] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [msg, setMsg] = useState<Msg>(null);
  const busy = !!status && (status.is_running || status.mode.toUpperCase() !== "IDLE");
  const [logs, setLogs] = useState<string[]>([]);
  useEffect(() => {
    api.logFiles().then(setLogs);
  }, []);
  const needle = file.trim().toLowerCase();
  const shown = needle ? logs.filter((id) => id.toLowerCase().includes(needle)) : logs;

  const run = async (label: string, fn: () => Promise<unknown>) => {
    try {
      await fn();
      setMsg({ kind: "ok", text: label });
    } catch (e) {
      setMsg({ kind: "bad", text: e instanceof ApiError ? `${e.code}: ${e.message}` : "Could not reach the API" });
    }
    setConfirm(false);
    onDone();
  };

  return (
    <div className="group">
      <div className="gh">Run</div>
      <input value={file} onChange={(e) => setFile(e.target.value)} placeholder="Log ID or file name" disabled={busy} />
      <p className="hint">Enter a file ID or part of the file name.</p>
      {logs.length > 0 && (
        <ul className="list files" aria-label="Logs in the dataset">
          {shown.map((id) => (
            <li key={id}>
              <button type="button" className={`mono ${id === file ? "on" : ""}`} title={id} disabled={busy} onClick={() => setFile(id)}>
                {id}
              </button>
            </li>
          ))}
          {shown.length === 0 && <li className="none">no match</li>}
        </ul>
      )}
      <button className="btn" disabled={busy} onClick={() => run("Live run started", () => api.startLive(file.trim() || undefined))}>
        Start live run
      </button>
      <input
        value={size}
        onChange={(e) => setSize(e.target.value.replace(/\D/g, ""))}
        placeholder="Batch size (blank = default, 0 = all)"
        inputMode="numeric"
        disabled={busy}
      />
      <button className="btn" disabled={busy} onClick={() => run("Batch run started", () => api.startBatch(size === "" ? undefined : Number(size)))}>
        Start batch run
      </button>
      <button className="btn" title="Re-enqueue dropped slices and unstick merge barriers" onClick={() => run("Reconcile finished", () => api.reconcile())}>
        Reconcile
      </button>
      {confirm ? (
        <div className="confirm">
          <span>{status?.is_running ? "Pipeline is running. Force reset?" : "Flush Redis and reset?"}</span>
          <button className="btn danger" onClick={() => run("Pipeline reset", () => api.reset(!!status?.is_running))}>
            {status?.is_running ? "Force reset" : "Reset"}
          </button>
          <button className="btn" onClick={() => setConfirm(false)}>
            Cancel
          </button>
        </div>
      ) : (
        <button className="btn" onClick={() => setConfirm(true)}>
          Reset…
        </button>
      )}
      {msg && <div className={`msg ${msg.kind}`}>{msg.text}</div>}
    </div>
  );
}

/* ---------------- logs table ---------------- */

function LogsTable({ docs, selected, onSelect, active }: { docs: Doc[]; selected: string | null; onSelect: (id: string) => void; active: Set<string> }) {
  return (
    <section className="panel logs">
      <h3>Logs</h3>
      <div className="tscroll">
        <table>
          <thead>
            <tr>
              <th>LOG_ID</th>
              <th>IMAGE_NAME</th>
              <th>STATUS</th>
              <th className="num">DEPTH</th>
              <th className="num">LITHOLOGY</th>
              <th className="pcol">PROGRESS</th>
            </tr>
          </thead>
          <tbody>
            {docs.map((d) => (
              <tr key={d.log_id} className={`${selected === d.log_id ? "sel" : ""} ${active.has(d.log_id) ? "act" : ""}`} onClick={() => onSelect(d.log_id)}>
                <td>{d.log_id}</td>
                <td>{d.image_name ?? "—"}</td>
                <td style={{ color: statusColor(d.status) }}>{cap(d.status)}</td>
                <td className="num">{d.depth_progress}</td>
                <td className="num">{d.lith_progress}</td>
                <td className="pcol">
                  <div className="pb">
                    <i style={{ width: `${Math.min(100, d.progress_percent)}%`, background: statusColor(d.status) }} />
                  </div>
                  <span>{d.progress_percent.toFixed(0)}%</span>
                </td>
              </tr>
            ))}
            {docs.length === 0 && (
              <tr>
                <td colSpan={6} className="none">
                  No documents in Redis
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/* ---------------- result view ---------------- */

const q = (v: unknown) => {
  const t = String(v ?? "");
  return /[",\r\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
};
const csvOf = (rows: Interval[]) =>
  ["top_depth,bottom_depth,lithology,confidence,top_y,bottom_y,cluster_id", ...rows.map((r) => [r.top_depth, r.bottom_depth, r.lithology, r.confidence, r.top_y, r.bottom_y, r.cluster_id].map(q).join(","))].join("\n") + "\n";

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  a.click();
  URL.revokeObjectURL(url);
}

function ResultView({ doc, onRock }: { doc: Doc | undefined; onRock: (rock: string | null) => void }) {
  const [rows, setRows] = useState<Interval[]>([]);
  const [hover, setHover] = useState<BedHover | null>(null);
  const [rowHover, setRowHover] = useState<number | null>(null);
  const [failed, setFailed] = useState(false);
  const id = doc?.log_id;
  const status = doc?.status ?? "";
  const live = running(status);
  const done = /complete/i.test(status);
  const chunks = doc ? `${doc.depth_progress}|${doc.lith_progress}` : "";

  // The merger writes the CSV once, when it finishes the log, so there is nothing partial to show before that.
  useEffect(() => {
    setFailed(false);
    if (!id || !(live || done)) return setRows([]);
    let cancelled = false;
    api
      .intervals(id)
      .then((r) => !cancelled && setRows((r ?? []).map((x) => ({ ...x, lithology: normLith(x.lithology) }))))
      .catch(() => {
        if (cancelled) return;
        setRows([]);
        setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [id, live, done, chunks]);

  useEffect(() => setHover(null), [id]);

  const hoverIdx = hover?.idx ?? rowHover;
  const bedRock = hoverIdx != null ? rockType(rows[hoverIdx]?.lithology) : null;
  useEffect(() => {
    onRock(bedRock);
    return () => onRock(null);
  }, [bedRock, onRock]);

  if (!doc)
    return (
      <section className="panel result">
        <div className="nodata">Select a log to see its lithology column.</div>
      </section>
    );

  const bed = hoverIdx != null ? rows[hoverIdx] : undefined;
  const styles = buildStyles(rows.map((r) => r.lithology));

  return (
    <section className="panel result">
      <div className="rhead">
        <h3>
          <span className="mono" title={doc.log_id}>
            {doc.log_id}
          </span>
          <small style={{ color: statusColor(status) }}>{cap(status)}</small>
        </h3>
        <span className="sp" />
        <span className="muted">
          Depth {doc.depth_progress} · Lithology {doc.lith_progress}
        </span>
      </div>
      <div className="rbody">
        <div className="colbox">
          <LithColumn rows={rows} styles={styles} hoverLith={bed?.lithology ?? null} selected={hoverIdx} onHover={setHover} />
          {live && <div className="tag">Intervals appear when the merger finishes</div>}
          <HoverCard rows={rows} idx={hoverIdx} styles={styles} />
        </div>
        <div className="intervals">
          <div className="ihead">
            <strong>Intervals</strong>
            <span className="muted">{rows.length ? `${rows.length} rows` : ""}</span>
            <span className="sp" />
            <button className="btn small" disabled={!done || !rows.length} onClick={() => download(`intervals_${doc.log_id}.csv`, csvOf(rows))}>
              Export CSV
            </button>
          </div>
          <div className="tscroll">
            <table onMouseLeave={() => setRowHover(null)}>
              <thead>
                <tr>
                  <th className="num">TOP_DEPTH</th>
                  <th className="num">BOTTOM_DEPTH</th>
                  <th>LITHOLOGY</th>
                  <th className="num">CONF.</th>
                  <th className="num">TOP_Y</th>
                  <th className="num">BOTTOM_Y</th>
                  <th>CLUSTER</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i} className={hoverIdx === i ? "sel" : ""} onMouseEnter={() => setRowHover(i)}>
                    <td className="num">{ft(r.top_depth, 2)}</td>
                    <td className="num">{ft(r.bottom_depth, 2)}</td>
                    <td>
                      <i className="sw" style={{ background: (styles.get(r.lithology) ?? FALLBACK_STYLE).color }} />
                      {r.lithology}
                    </td>
                    <td className="num">{r.confidence != null ? r.confidence.toFixed(2) : "—"}</td>
                    <td className="num">{r.top_y}</td>
                    <td className="num">{r.bottom_y}</td>
                    <td>{r.cluster_id}</td>
                  </tr>
                ))}
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={7} className="none">
                      {failed
                        ? "Could not load intervals from the API."
                        : live
                          ? "Waiting for the merger to write this log's intervals…"
                          : done
                            ? "No intervals yet: the merger has not written this log's CSV to the output folder."
                            : "Intervals appear as this log is processed."}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </section>
  );
}

/* ---------------- app ---------------- */

export default function App() {
  const { health, status, dash, offline, refresh } = usePipeline();
  const [hiddenStatus, setHiddenStatus] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [bedRock, setBedRock] = useState<string | null>(null);
  const [legendRock, setLegendRock] = useState<string | null>(null);
  const hoverRock = legendRock ?? bedRock;
  const mode = (status?.mode ?? dash?.pipeline_state.mode ?? "").toUpperCase();
  const all = dash?.documents ?? [];
  const active = useMemo(() => new Set(status?.active_logs ?? []), [status]);

  // In live mode, follow the log that is being processed.
  const prevMode = useRef("");
  useEffect(() => {
    if (mode === "LIVE" && prevMode.current !== "LIVE" && status?.active_logs[0]) setSelected(status.active_logs[0]);
    prevMode.current = mode;
  }, [mode, status]);
  useEffect(() => {
    if (!selected && status?.active_logs[0]) setSelected(status.active_logs[0]);
  }, [mode, selected, status]);

  const toggle = (set: Set<string>, put: (s: Set<string>) => void) => (key: string) => {
    const next = new Set(set);
    next.has(key) ? next.delete(key) : next.add(key);
    put(next);
  };

  const docs = useMemo(
    () =>
      [...all]
        .filter((d) => !hiddenStatus.has((d.status ?? "").toUpperCase()))
        .sort((a, b) => a.log_id.localeCompare(b.log_id)),
    [all, hiddenStatus],
  );

  const redisOk = health !== null && health !== "down" && health.redis === "connected";
  const failed = all.filter((d) => /fail|corrupt/i.test(d.status ?? "")).length;
  const q = dash?.queue_breakdown;
  const backlog = (k: "depth" | "lithology" | "merge") => (q ? q[k].lag + q[k].pending : 0);

  const facts: Array<[string, string]> = [
    ["Mode", mode || "—"],
    ["Batch", mode === "BATCH" ? `${status?.current_batch ?? 0} of ${status?.total_batches ?? 0}` : "—"],
    ["Logs completed", `${status?.total_completed ?? 0} of ${all.length}`],
    ["In flight", String(status?.active_logs.length ?? 0)],
    ["Failed", String(failed)],
    ["Backlog depth", String(backlog("depth"))],
    ["Backlog lithology", String(backlog("lithology"))],
    ["Backlog merge", String(backlog("merge"))],
  ];

  return (
    <div className="app">
      <HatchDefs />
      <Chat doc={all.find((d) => d.log_id === selected)} status={status} hoverRock={hoverRock} />
      <header className="top">
        <span className="sp" />
        <span className={offline || health === "down" ? "bad" : ""}>
          Redis: {offline ? "API unreachable" : health === "down" ? "unreachable" : redisOk ? "connected" : "checking…"}
        </span>
      </header>

      <div className="facts">
        {facts.map(([k, v]) => (
          <span key={k} className={k === "Failed" && failed ? "bad" : ""}>
            {k}
            <b>{v}</b>
          </span>
        ))}
      </div>

      <div className="body">
        <aside className="side">
          <RunControls status={status} onDone={refresh} />
          <Listbox
            title="Status"
            items={STATUSES.map((s) => ({ key: s, label: cap(s), color: statusColor(s) }))}
            hidden={hiddenStatus}
            toggle={toggle(hiddenStatus, setHiddenStatus)}
          />
        </aside>

        <main className="page">
          {offline && !dash ? (
            <div className="nodata big">Cannot reach the API ({api.label}). Check that the dispatcher is running.</div>
          ) : (
            <>
              <ResultView doc={all.find((d) => d.log_id === selected)} onRock={setBedRock} />
              <LogsTable docs={docs} selected={selected} onSelect={setSelected} active={active} />
              <section className="panel mapbox">
                <div className="rhead">
                  <h3>Where each rock type was logged</h3>
                </div>
                <GeoMap hoverRock={hoverRock} setLegendRock={setLegendRock} selected={selected} onSelect={setSelected} />
              </section>
            </>
          )}
        </main>
      </div>

      <footer className="foot">
        <span>{offline ? "offline" : "polling every 1 s"}</span>
        <span>{api.label}</span>
      </footer>
    </div>
  );
}
