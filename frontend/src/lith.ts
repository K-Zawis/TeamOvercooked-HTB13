import { useEffect, useState } from "react";

/** Log legends are read by OCR, so names arrive as free text. Fold the common spellings together. */
const ALIASES: Record<string, string> = {
  sh: "Shale",
  shale: "Shale",
  shl: "Shale",
  ss: "Sandstone",
  sst: "Sandstone",
  sand: "Sandstone",
  sandstone: "Sandstone",
  slt: "Siltstone",
  silt: "Siltstone",
  siltstone: "Siltstone",
  ls: "Limestone",
  lms: "Limestone",
  lime: "Limestone",
  limestone: "Limestone",
  dol: "Dolomite",
  dolomite: "Dolomite",
  anhy: "Anhydrite",
  anhydrite: "Anhydrite",
  salt: "Salt",
  halite: "Salt",
  clst: "Claystone",
  claystone: "Claystone",
  marl: "Marl",
  chalk: "Chalk",
  coal: "Coal",
  conglomerate: "Conglomerate",
};

export function normLith(raw: string | null | undefined): string {
  const t = (raw ?? "").trim();
  if (!t) return "Unclassified";
  const hit = ALIASES[t.toLowerCase()];
  return hit ?? t.charAt(0).toUpperCase() + t.slice(1).toLowerCase();
}

const COLORS: Record<string, string> = {
  Shale: "#6787a2",
  Siltstone: "#e08a1e",
  Sandstone: "#2fa98a",
  Limestone: "#cfc7a8",
  Dolomite: "#b9a1c9",
  Anhydrite: "#c98fa0",
  Salt: "#9fd0e6",
  Claystone: "#8a7a6a",
  Marl: "#a8b36b",
  Chalk: "#e6e1d3",
  Coal: "#555555",
  Conglomerate: "#b5784a",
  Unclassified: "#777777",
};
const EXTRA = ["#d46a6a", "#6aa6d4", "#c9b458", "#7ac18a", "#a58bd1", "#d49a6a"];

export function lithColor(name: string): string {
  if (COLORS[name]) return COLORS[name];
  return EXTRA[[...name].reduce((a, c) => a + c.charCodeAt(0), 0) % EXTRA.length];
}

export const PATTERNED = ["Shale", "Siltstone", "Sandstone", "Limestone"];
export const patternFill = (name: string) => (PATTERNED.includes(name) ? `url(#hatch-${name.toLowerCase()})` : lithColor(name));

/** Offline fallback for the hover card, used when Wikipedia cannot be reached. */
const FALLBACK: Record<string, string> = {
  Shale: "Fine-grained sedimentary rock made of compacted clay and silt that splits into thin layers. Often a source or a seal for hydrocarbons.",
  Siltstone: "Sedimentary rock of silt-sized grains, coarser than shale and finer than sandstone.",
  Sandstone: "Sedimentary rock of cemented sand-sized grains. A common reservoir rock because it is often porous and permeable.",
  Limestone: "Carbonate rock made mostly of calcite, usually formed from marine organisms. Can be a reservoir or a seal.",
};

export interface RockInfo {
  text: string;
  source: "Wikipedia" | "built-in";
  url?: string;
}

const cache = new Map<string, RockInfo>();
const pending = new Map<string, Promise<RockInfo>>();

const firstSentences = (s: string, max = 290) => {
  // Split where a sentence ends and a capital letter starts, so "e.g. kaolin" is not a boundary.
  const parts = s.split(/(?<=[.!?])\s+(?=[A-Z])/);
  let out = "";
  for (const p of parts) {
    if (out && (out + " " + p).length > max) break;
    out = out ? `${out} ${p}` : p;
  }
  return out.length > 360 ? `${out.slice(0, 357).trimEnd()}…` : out;
};

function fetchInfo(name: string): Promise<RockInfo> {
  const hit = pending.get(name);
  if (hit) return hit;
  const fallback: RockInfo = { text: FALLBACK[name] ?? "No description available offline.", source: "built-in" };
  const p = fetch(`https://en.wikipedia.org/api/rest_v1/page/summary/${encodeURIComponent(name)}`)
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
    .then((d): RockInfo => {
      if (d.type === "disambiguation" || !d.extract) return fallback;
      return { text: firstSentences(d.extract), source: "Wikipedia", url: d.content_urls?.desktop?.page };
    })
    .catch(() => fallback)
    .then((info) => {
      cache.set(name, info);
      return info;
    });
  pending.set(name, p);
  return p;
}

/** Rock description for the hover card. Fetched once per rock type, then cached. */
export function useRockInfo(name: string | null): RockInfo | null {
  const [info, setInfo] = useState<RockInfo | null>(name ? (cache.get(name) ?? null) : null);
  useEffect(() => {
    if (!name) return setInfo(null);
    const cached = cache.get(name);
    if (cached) return setInfo(cached);
    setInfo(null);
    let live = true;
    fetchInfo(name).then((i) => live && setInfo(i));
    return () => {
      live = false;
    };
  }, [name]);
  return info;
}

export const ft = (n: number | null | undefined, d = 1) =>
  n == null ? "—" : n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
