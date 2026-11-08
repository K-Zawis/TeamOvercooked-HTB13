import { useEffect, useState } from "react";

/** Log legends are read by OCR, so names arrive as free text. These aliases only decide whether a label
 * is a known rock type (for colour, hatch pattern and description); the label itself is never rewritten. */
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

/** The label exactly as the merger wrote it (trimmed). */
export function normLith(raw: string | null | undefined): string {
  return (raw ?? "").trim() || "Unclassified";
}

/** The known rock type a label refers to ("sst" -> "Sandstone"), or null for free-text legend labels. */
export function rockType(name: string | null | undefined): string | null {
  return ALIASES[(name ?? "").trim().toLowerCase()] ?? null;
}

/**
 * One palette for everything: muted hues of similar lightness and chroma, so no colour shouts over
 * another on the dark background. Texture (see Hatch.tsx) is a second, independent cue.
 */
export const PALETTE = ["#7f9fc4", "#6fb3a8", "#9bbf7a", "#cdbf6c", "#d79a6a", "#cc7f86", "#b08fc9", "#8aa0a8", "#c9a98b", "#7fb5d6"];
export const PATTERN_COUNT = 10;

export interface LithStyle {
  color: string;
  /** Index into the texture set defined in Hatch.tsx. */
  pattern: number;
}

/**
 * Style per distinct label, assigned in order of first appearance so the labels of one log are always
 * told apart. Past one full palette the pattern shifts, so (colour, texture) pairs keep differing.
 */
export function buildStyles(labels: string[]): Map<string, LithStyle> {
  const out = new Map<string, LithStyle>();
  for (const l of labels) {
    if (out.has(l)) continue;
    const i = out.size;
    out.set(l, { color: PALETTE[i % PALETTE.length], pattern: (i + Math.floor(i / PALETTE.length)) % PATTERN_COUNT });
  }
  return out;
}

export const FALLBACK_STYLE: LithStyle = { color: "#777777", pattern: 0 };

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
