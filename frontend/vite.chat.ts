import type { Plugin } from "vite";

/**
 * POST /local/chat: the dashboard's assistant. The browser never sees the OpenAI key; this dev/preview
 * server holds it (OPENAI_API_KEY, read from frontend/.env.local or the environment), forwards the
 * conversation to OpenAI and streams the reply back as plain text.
 */

const SYSTEM_PROMPT = `You are the built-in guide of a well-log lithology dashboard. Help geologists and first-time users understand the dashboard and the geology on it. Be concise (under 150 words unless asked for more), plain-spoken, and use short lists when they help.

What the dashboard is
- A pipeline reads scanned well logs (mudlogs) in two stages. The depth stage OCRs the printed depth labels; the lithology stage splits the interpreted-lithology column into windows and compares each window's texture, colour and edges with the pattern crops from the log's own legend. A merger then joins both into one interval table per log. Nothing is shown for a log until the merger finishes it.
- Left panel "Run": start a live run (one log: type an ID or part of a file name, or click one in the list), start a batch run (all logs, N at a time; 0 = all at once; you cannot pick a range), Reconcile (re-queue dropped work), Reset (flushes Redis; asks to confirm). The Status list filters the logs table.
- Top strip: mode, batch, completed/failed counts, and queue backlogs for depth, lithology and merge.
- Lithology result (top): a depth column of beds plus an interval table. Hover a bed or a row for details. Export CSV works once a log is complete.
- Logs table: every log in Redis with status, depth/lithology chunk progress and a progress bar. Click a row to select it.
- Map (bottom): where each rock type was logged. Its data is hardcoded and illustrative, not computed from the logs; say so if asked.

How to read the result
- Lithology label: the text printed next to the legend swatch, read by OCR, so it can be misspelled or odd (for example mud-log legend entries such as "Mud gain" are not rock types).
- Cluster (cluster_N): the index of the legend entry the bed was matched to. Beds with the same cluster matched the same legend pattern.
- Confidence (0 to 1): how closely the bed's pattern matched its legend pattern. Low means a weak match: worth checking against the original scan.
- Depths come from interpolating the OCR'd depth labels; they stay in the units printed on the log (not converted). A missing depth means too few depth labels were read there.
- Colours and textures in the column are assigned per log by this dashboard to tell labels apart. They are NOT industry-standard lithology symbols and can differ between logs.

Geology you can explain: shale, siltstone, sandstone, limestone, dolomite, anhydrite, salt (halite), claystone, marl, chalk, coal, conglomerate; reservoir, seal, source rock, porosity, permeability; mudlogs, cuttings, rate of penetration, shows, mud gain/loss, MD versus TVD.

Rules
- Use the CURRENT CONTEXT below when it answers the question (selected log, its beds, hovered rock). Never invent numbers, depths or lithologies that are not in it.
- If something cannot be known from the context, say so and say where in the dashboard to look.
- You interpret; the geologist decides. For interpretation questions, remind them to verify against the original log.
- If asked about something unrelated to this dashboard or geology, say so briefly and steer back.`;

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

const MAX_MESSAGES = 16;
const MAX_CHARS = 4000;
const MAX_CONTEXT = 6000;

function readBody(req: any, limit = 64_000): Promise<string> {
  return new Promise((resolve, reject) => {
    let size = 0;
    const parts: Buffer[] = [];
    req.on("data", (c: Buffer) => {
      size += c.length;
      if (size > limit) {
        reject(new Error("too large"));
        req.destroy();
      } else parts.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(parts).toString("utf8")));
    req.on("error", reject);
  });
}

const json = (res: any, status: number, body: unknown) => {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(body));
};

export function chatApi(env: Record<string, string>): Plugin {
  const apiKey = env.OPENAI_API_KEY || process.env.OPENAI_API_KEY || "";
  const model = env.OPENAI_MODEL || process.env.OPENAI_MODEL || "gpt-4o-mini";
  const base = (env.OPENAI_BASE_URL || process.env.OPENAI_BASE_URL || "https://api.openai.com/v1").replace(/\/$/, "");

  const handler = async (req: any, res: any, next: () => void) => {
    if ((req.url ?? "").split("?")[0] !== "/local/chat") return next();
    if (req.method === "GET") return json(res, 200, { ready: !!apiKey, model });
    if (req.method !== "POST") return json(res, 405, { error: "method_not_allowed" });
    if (!apiKey) return json(res, 503, { error: "no_key" });

    let messages: ChatMessage[];
    let context = "";
    try {
      const body = JSON.parse(await readBody(req));
      const raw = Array.isArray(body.messages) ? body.messages : [];
      messages = raw
        .filter((m: any) => (m?.role === "user" || m?.role === "assistant") && typeof m.content === "string" && m.content.trim())
        .slice(-MAX_MESSAGES)
        .map((m: any) => ({ role: m.role, content: String(m.content).slice(0, MAX_CHARS) }));
      context = typeof body.context === "string" ? body.context.slice(0, MAX_CONTEXT) : "";
      if (!messages.length || messages[messages.length - 1].role !== "user") throw new Error("no user message");
    } catch {
      return json(res, 400, { error: "bad_request" });
    }

    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 60_000);
    res.on("close", () => {
      clearTimeout(timer);
      abort.abort();
    });

    try {
      const upstream = await fetch(`${base}/chat/completions`, {
        method: "POST",
        signal: abort.signal,
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
        body: JSON.stringify({
          model,
          stream: true,
          max_completion_tokens: 700,
          messages: [{ role: "system", content: `${SYSTEM_PROMPT}\n\nCURRENT CONTEXT\n${context || "(nothing selected)"}` }, ...messages],
        }),
      });
      if (!upstream.ok || !upstream.body) {
        // Report OpenAI's own error message, never anything that could carry the key.
        let msg = `OpenAI returned ${upstream.status}`;
        try {
          const e = JSON.parse(await upstream.text());
          if (e?.error?.message) msg = String(e.error.message).slice(0, 300);
        } catch {
          /* keep the status-only message */
        }
        return json(res, 502, { error: "upstream", status: upstream.status, message: msg });
      }

      res.statusCode = 200;
      res.setHeader("Content-Type", "text/plain; charset=utf-8");
      res.setHeader("Cache-Control", "no-store");
      res.setHeader("X-Accel-Buffering", "no");

      const decoder = new TextDecoder();
      let buf = "";
      for await (const chunk of upstream.body as any) {
        buf += decoder.decode(chunk, { stream: true });
        let nl: number;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line.startsWith("data:")) continue;
          const data = line.slice(5).trim();
          if (data === "[DONE]") continue;
          try {
            const text = JSON.parse(data)?.choices?.[0]?.delta?.content;
            if (typeof text === "string" && text) res.write(text);
          } catch {
            /* ignore keep-alives and partial lines */
          }
        }
      }
      res.end();
    } catch (e: any) {
      if (res.headersSent) return res.end();
      json(res, 502, { error: "upstream", message: e?.name === "AbortError" ? "The request timed out." : "Could not reach OpenAI." });
    }
  };

  return {
    name: "dashboard-chat",
    configureServer: (s) => void s.middlewares.use(handler),
    configurePreviewServer: (s) => void s.middlewares.use(handler),
  };
}
