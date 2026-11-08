import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, type Doc, type Interval, type Status } from "./api";
import { normLith } from "./lith";

interface Msg {
  role: "user" | "assistant";
  content: string;
  error?: boolean;
}

const SUGGESTIONS = [
  "How do I use this dashboard?",
  "What do cluster and confidence mean?",
  "Summarise the selected log",
  "What makes a good reservoir rock?",
];

const NO_KEY = "The assistant needs an OpenAI key. Create frontend/.env.local with a line OPENAI_API_KEY=your-key, then restart the dev server (pnpm dev).";

/** A compact, factual description of what the user is looking at, sent along with each question. */
function describe(doc: Doc | undefined, status: Status | null, hoverRock: string | null, rows: Interval[] | null): string {
  const out: string[] = [];
  if (status) out.push(`Pipeline mode ${status.mode}; ${status.active_logs.length} log(s) in flight; ${status.total_completed} completed.`);
  if (hoverRock) out.push(`The user is hovering the rock type ${hoverRock}.`);
  if (!doc) {
    out.push("No log is selected.");
    return out.join("\n");
  }
  out.push(`Selected log: ${doc.log_id} (status ${doc.status ?? "unknown"}, depth chunks ${doc.depth_progress}, lithology chunks ${doc.lith_progress}).`);
  if (!rows?.length) {
    out.push("No interval table is available for it yet (the merger writes it when the log completes).");
    return out.join("\n");
  }

  const thick = (r: Interval) => (r.top_depth != null && r.bottom_depth != null ? r.bottom_depth - r.top_depth : 0);
  const depths = rows.flatMap((r) => [r.top_depth, r.bottom_depth]).filter((d): d is number => d != null);
  const noDepth = rows.filter((r) => r.top_depth == null || r.bottom_depth == null).length;
  const conf = rows.map((r) => r.confidence).filter((c): c is number => c != null);
  const mean = conf.length ? conf.reduce((a, b) => a + b, 0) / conf.length : null;
  out.push(
    `Intervals: ${rows.length} beds; depth ${depths.length ? `${Math.min(...depths)} to ${Math.max(...depths)} (units as printed on the log)` : "not read"}; ${noDepth} without depth; mean confidence ${mean != null ? mean.toFixed(2) : "n/a"}.`,
  );

  const by = new Map<string, { n: number; t: number; c: number[] }>();
  for (const r of rows) {
    const k = normLith(r.lithology);
    const e = by.get(k) ?? { n: 0, t: 0, c: [] };
    e.n += 1;
    e.t += thick(r);
    if (r.confidence != null) e.c.push(r.confidence);
    by.set(k, e);
  }
  const total = [...by.values()].reduce((a, e) => a + e.t, 0);
  out.push("By label:");
  for (const [k, e] of [...by.entries()].sort((a, b) => b[1].t - a[1].t).slice(0, 12)) {
    const mc = e.c.length ? ` mean confidence ${(e.c.reduce((a, b) => a + b, 0) / e.c.length).toFixed(2)}` : "";
    out.push(`- ${k}: ${e.n} bed(s), thickness ${e.t.toFixed(1)}${total > 0 ? ` (${Math.round((e.t / total) * 100)}%)` : ""},${mc}`);
  }
  const weakest = rows
    .filter((r) => r.confidence != null)
    .sort((a, b) => a.confidence! - b.confidence!)
    .slice(0, 3);
  if (weakest.length) out.push("Lowest-confidence beds: " + weakest.map((r) => `${normLith(r.lithology)} ${r.top_depth ?? "?"}-${r.bottom_depth ?? "?"} (${r.confidence!.toFixed(2)})`).join("; "));
  return out.join("\n");
}

/** Renders the little markdown the assistant uses (paragraphs, bullets, **bold**, `code`) without raw HTML. */
function Rich({ text }: { text: string }) {
  const inline = (s: string): ReactNode[] =>
    s.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((p, i) =>
      p.startsWith("**") && p.endsWith("**") ? <b key={i}>{p.slice(2, -2)}</b> : p.startsWith("`") && p.endsWith("`") ? <code key={i}>{p.slice(1, -1)}</code> : p,
    );
  const blocks: ReactNode[] = [];
  let list: string[] = [];
  const flush = () => {
    if (list.length)
      blocks.push(
        <ul key={`l${blocks.length}`}>
          {list.map((x, i) => (
            <li key={i}>{inline(x)}</li>
          ))}
        </ul>,
      );
    list = [];
  };
  for (const line of text.split("\n")) {
    const m = /^\s*(?:[-*]|\d+\.)\s+(.*)$/.exec(line);
    if (m) list.push(m[1]);
    else {
      flush();
      if (line.trim()) blocks.push(<p key={`p${blocks.length}`}>{inline(line.replace(/^#+\s*/, ""))}</p>);
    }
  }
  flush();
  return <>{blocks}</>;
}

export function Chat({ doc, status, hoverRock }: { doc: Doc | undefined; status: Status | null; hoverRock: string | null }) {
  const [open, setOpen] = useState(false);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState<boolean | null>(null);
  const abort = useRef<AbortController | null>(null);
  const log = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!open || ready !== null) return;
    fetch("/local/chat")
      .then((r) => r.json())
      .then((d) => setReady(!!d.ready))
      .catch(() => setReady(false));
  }, [open, ready]);

  useEffect(() => {
    if (open) field.current?.focus();
  }, [open]);
  // Keep the newest text in view by moving the chat log itself. (Not scrollIntoView: it returns a Promise in
  // current Chrome, which React would treat as a cleanup function, and it can scroll the whole page.)
  useEffect(() => {
    const el = log.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs, open]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);
  useEffect(() => () => abort.current?.abort(), []);

  const send = async (text: string) => {
    const q = text.trim();
    if (!q || busy) return;
    setInput("");
    setBusy(true);
    const history: Msg[] = [...msgs.filter((m) => !m.error), { role: "user", content: q }];
    setMsgs([...msgs, { role: "user", content: q }, { role: "assistant", content: "" }]);

    const ctl = new AbortController();
    abort.current = ctl;
    const fail = (content: string) =>
      setMsgs((m) => {
        const next = m.slice();
        next[next.length - 1] = { role: "assistant", content, error: true };
        return next;
      });
    try {
      let rows: Interval[] | null = null;
      if (doc && /complete/i.test(doc.status ?? "")) rows = await api.intervals(doc.log_id).catch(() => null);
      const res = await fetch("/local/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: ctl.signal,
        body: JSON.stringify({ messages: history.map(({ role, content }) => ({ role, content })), context: describe(doc, status, hoverRock, rows) }),
      });
      if (!res.ok || !res.body) {
        const d = await res.json().catch(() => ({}));
        setReady(res.status === 503 ? false : ready);
        return fail(d.error === "no_key" ? NO_KEY : `The assistant could not answer: ${d.message ?? res.statusText}`);
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let got = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        got += dec.decode(value, { stream: true });
        const snapshot = got;
        setMsgs((m) => {
          const next = m.slice();
          next[next.length - 1] = { role: "assistant", content: snapshot };
          return next;
        });
      }
      if (!got.trim()) fail("The assistant sent an empty reply. Try asking again.");
    } catch (e) {
      if ((e as Error).name !== "AbortError") fail("Could not reach the assistant. Is the dev server running?");
    } finally {
      setBusy(false);
      abort.current = null;
    }
  };

  const stop = () => abort.current?.abort();

  return (
    <>
      {open && (
        <section className="chatbox" role="dialog" aria-label="Dashboard guide">
          <header>
            <strong>Guide</strong>
            <span className="muted">{doc ? `Looking at ${doc.log_id}` : "No log selected"}</span>
            <span className="sp" />
            {msgs.length > 0 && (
              <button className="btn small" onClick={() => (stop(), setMsgs([]))} title="Clear this conversation">
                Clear
              </button>
            )}
            <button className="btn small" onClick={() => setOpen(false)} aria-label="Close the guide">
              Close
            </button>
          </header>

          <div className="chatlog" aria-live="polite" ref={log}>
            {msgs.length === 0 && (
              <div className="chathello">
                <p>Ask me how the dashboard works, or what a rock type or a number means. I can also read the log you have selected.</p>
                {ready === false && <p className="chatwarn">{NO_KEY}</p>}
                <div className="chips">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} onClick={() => send(s)} disabled={busy}>
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {msgs.map((m, i) => (
              <div key={i} className={`bubble ${m.role} ${m.error ? "err" : ""}`}>
                {m.role === "assistant" && !m.error ? m.content ? <Rich text={m.content} /> : <span className="typing">Thinking…</span> : m.content}
              </div>
            ))}
          </div>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              send(input);
            }}
          >
            <textarea
              ref={field}
              rows={2}
              value={input}
              placeholder="Ask about the dashboard or the geology"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send(input);
                }
              }}
            />
            {busy ? (
              <button type="button" className="btn" onClick={stop}>
                Stop
              </button>
            ) : (
              <button type="submit" className="btn" disabled={!input.trim()}>
                Send
              </button>
            )}
          </form>
        </section>
      )}
      <button className="chatfab" onClick={() => setOpen(!open)} aria-expanded={open} aria-label={open ? "Close the guide" : "Open the guide"} title={open ? "Close the guide" : "Ask the guide"}>
        {open ? (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" aria-hidden>
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        ) : (
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" aria-hidden>
            <path d="M4 5h16a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H10l-4.5 4v-4H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z" />
          </svg>
        )}
      </button>
    </>
  );
}
