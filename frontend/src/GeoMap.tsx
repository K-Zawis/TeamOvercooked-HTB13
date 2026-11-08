import { geoGraticule10, geoMercator, geoPath } from "d3-geo";
import type { Feature, FeatureCollection, Geometry } from "geojson";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { feature } from "topojson-client";
import { buildStyles } from "./lith";
import { LOCATIONS, ROCKS, SUMMARY } from "./mapData";
import { useSize } from "./useSize";

type Countries = FeatureCollection<Geometry>;
interface View {
  k: number;
  x: number;
  y: number;
}
const HOME: View = { k: 1, x: 0, y: 0 };
const MAX_ZOOM = 60;

/** One style per rock type for the whole map, so a rock keeps its colour and texture everywhere. */
export const ROCK_STYLES = buildStyles([...ROCKS]);

function useCountries() {
  const [c, setC] = useState<Countries | null>(null);
  useEffect(() => {
    let live = true;
    import("world-atlas/countries-50m.json").then((m) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const topo = (m.default ?? m) as any;
      if (live) setC(feature(topo, topo.objects.countries) as unknown as Countries);
    });
    return () => {
      live = false;
    };
  }, []);
  return c;
}

// rock -> log -> thickness, from the hardcoded summary
const BY_ROCK = (() => {
  const m = new Map<string, Map<string, number>>();
  for (const s of SUMMARY) {
    const inner = m.get(s.lithology) ?? new Map<string, number>();
    inner.set(s.log_id, (inner.get(s.log_id) ?? 0) + s.total_thickness_ft);
    m.set(s.lithology, inner);
  }
  return m;
})();
const AREAS = [...new Set(LOCATIONS.map((l) => l.name))].sort();

export function GeoMap({
  hoverRock,
  setLegendRock,
  selected,
  onSelect,
}: {
  hoverRock: string | null;
  setLegendRock: (r: string | null) => void;
  selected: string | null;
  onSelect: (logId: string) => void;
}) {
  const [ref, { w, h }] = useSize<HTMLDivElement>();
  const svgRef = useRef<SVGSVGElement>(null);
  const countries = useCountries();
  const [view, setView] = useState<View>(HOME);
  const [area, setArea] = useState<string | null>(null);
  // Smooth the camera only for "zoom to" jumps; wheel and drag must follow the pointer exactly.
  const [glide, setGlide] = useState(false);
  const [grabbing, setGrabbing] = useState(false);
  const drag = useRef<{ x: number; y: number; vx: number; vy: number; moved: boolean } | null>(null);

  const projection = useMemo(() => {
    if (w < 50 || h < 50) return null;
    return geoMercator().fitExtent(
      [
        [40, 30],
        [w - 40, h - 30],
      ],
      { type: "MultiPoint", coordinates: LOCATIONS.map((l) => [l.lon, l.lat]) },
    );
  }, [w, h]);
  const path = useMemo(() => (projection ? geoPath(projection) : null), [projection]);

  // The projection changed (first layout, resize, expand): the old camera no longer matches it.
  useEffect(() => {
    setView(HOME);
    setArea(null);
    setGlide(false);
  }, [projection]);

  // The basemap is the heavy part (hundreds of country outlines). Build it once per projection, as a single
  // path, so panning, zooming and hovering only move one transform and never recompute geometry.
  const basemap = useMemo(() => {
    if (!path || !countries) return null;
    const land = countries.features.map((f: Feature<Geometry>) => path(f) ?? "").join("");
    return (
      <>
        <path d={path(geoGraticule10()) ?? ""} fill="none" stroke="#2f2f2f" strokeWidth=".5" vectorEffect="non-scaling-stroke" />
        <path d={land} fill="#2d2d2d" stroke="#4a4a4a" strokeWidth=".6" vectorEffect="non-scaling-stroke" />
      </>
    );
  }, [path, countries]);

  const zoomAround = useCallback(
    (factor: number, cx: number, cy: number) =>
      setView((v) => {
        const k = Math.min(MAX_ZOOM, Math.max(1, v.k * factor));
        const f = k / v.k;
        return { k, x: cx - (cx - v.x) * f, y: cy - (cy - v.y) * f };
      }),
    [],
  );

  // Wheel zoom needs a non-passive listener so the page does not scroll as well.
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      setGlide(false);
      const r = el.getBoundingClientRect();
      zoomAround(Math.exp(-e.deltaY * 0.0018), e.clientX - r.left, e.clientY - r.top);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomAround, projection]);

  const pickArea = (name: string | null) => {
    setArea(name);
    setGlide(true);
    if (name === null || !projection) return setView(HOME);
    const xy = LOCATIONS.filter((l) => l.name === name).map((l) => projection([l.lon, l.lat])!);
    const xs = xy.map((p) => p[0]);
    const ys = xy.map((p) => p[1]);
    const bw = Math.max(20, Math.max(...xs) - Math.min(...xs));
    const bh = Math.max(20, Math.max(...ys) - Math.min(...ys));
    const k = Math.min(MAX_ZOOM, Math.max(1, Math.min((w * 0.7) / bw, (h * 0.6) / bh)));
    const cx = (Math.max(...xs) + Math.min(...xs)) / 2;
    const cy = (Math.max(...ys) + Math.min(...ys)) / 2;
    setView({ k, x: w / 2 - k * cx, y: h / 2 - k * cy });
  };

  const weights = hoverRock ? BY_ROCK.get(hoverRock) : undefined;
  const maxW = weights ? Math.max(...weights.values(), 1) : 1;
  const color = hoverRock ? (ROCK_STYLES.get(hoverRock)?.color ?? "#7f9fc4") : "#7f9fc4";
  const k = view.k;

  return (
    <div className="geo">
      <div className="geobar">
        <div className="rocks">
          {ROCKS.map((r) => {
            const m = BY_ROCK.get(r)!;
            const total = [...m.values()].reduce((a, b) => a + b, 0);
            const st = ROCK_STYLES.get(r)!;
            return (
              <div
                key={r}
                className={`lrow ${hoverRock === r ? "on" : ""}`}
                title={`${m.size} logs · ${Math.round(total).toLocaleString()} ft`}
                onMouseEnter={() => setLegendRock(r)}
                onMouseLeave={() => setLegendRock(null)}
              >
                <svg width="12" height="12">
                  <rect width="12" height="12" rx="2" fill={st.color} />
                  <rect width="12" height="12" rx="2" fill={`url(#pat-${st.pattern})`} stroke="#000" strokeOpacity=".5" />
                </svg>
                <span>{r}</span>
                <small>{m.size}</small>
              </div>
            );
          })}
        </div>
        <div className="areas">
          <span>Zoom to</span>
          <button className={area === null ? "on" : ""} onClick={() => pickArea(null)}>
            All
          </button>
          {AREAS.map((a) => (
            <button key={a} className={area === a ? "on" : ""} onClick={() => pickArea(a)}>
              {a}
            </button>
          ))}
          <span className="sp" />
          <button title="Zoom out" aria-label="Zoom out" onClick={() => (setGlide(true), zoomAround(1 / 1.6, w / 2, h / 2))}>
            −
          </button>
          <button title="Zoom in" aria-label="Zoom in" onClick={() => (setGlide(true), zoomAround(1.6, w / 2, h / 2))}>
            +
          </button>
        </div>
        <div className="maptag">Hardcoded illustrative data. Scroll to zoom, drag to pan.</div>
      </div>

      <div ref={ref} className="geowrap">
        {path && projection && (
          <svg
            ref={svgRef}
            width={w}
            height={h}
            className={grabbing ? "grab" : ""}
            onPointerDown={(e) => {
              (e.target as Element).setPointerCapture?.(e.pointerId);
              setGlide(false);
              drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false };
            }}
            onPointerMove={(e) => {
              const d = drag.current;
              if (!d) return;
              const dx = e.clientX - d.x;
              const dy = e.clientY - d.y;
              if (!d.moved && Math.abs(dx) + Math.abs(dy) > 3) {
                d.moved = true;
                setGrabbing(true);
              }
              if (d.moved) setView((v) => ({ ...v, x: d.vx + dx, y: d.vy + dy }));
            }}
            onPointerUp={() => {
              setGrabbing(false);
              // Keep `moved` visible to the click that follows the release, then clear it.
              setTimeout(() => (drag.current = null), 0);
            }}
          >
            <g
              style={{
                transform: `translate(${view.x}px, ${view.y}px) scale(${k})`,
                transformOrigin: "0 0",
                transition: glide ? "transform .45s cubic-bezier(.2,.7,.2,1)" : "none",
              }}
            >
              {basemap}
              {LOCATIONS.map((l) => {
                const p = projection([l.lon, l.lat]);
                if (!p) return null;
                const v = weights?.get(l.log_id);
                const match = !hoverRock || v != null;
                const sel = selected === l.log_id;
                // With a rock type hovered, each matching log is drawn at a size that follows its thickness of that rock.
                const r = hoverRock && v != null ? 4 + 8 * Math.sqrt(v / maxW) : match ? 4 : 2.2;
                return (
                  <g
                    key={l.log_id}
                    className="pt"
                    onClick={() => {
                      if (!drag.current?.moved) onSelect(l.log_id);
                    }}
                  >
                    {sel && <circle cx={p[0]} cy={p[1]} r={(r + 4) / k} fill="none" stroke="#e8c82e" strokeWidth="1.6" vectorEffect="non-scaling-stroke" />}
                    <circle
                      cx={p[0]}
                      cy={p[1]}
                      r={r / k}
                      fill={hoverRock && v != null ? color : "#9fb8d4"}
                      fillOpacity={match ? 0.92 : 0.3}
                      stroke="#111"
                      strokeWidth=".8"
                      vectorEffect="non-scaling-stroke"
                    />
                    <title>{`${l.name} · ${l.log_id}${v != null ? ` · ${Math.round(v)} ft ${hoverRock}` : ""}`}</title>
                  </g>
                );
              })}
            </g>
          </svg>
        )}

      </div>
    </div>
  );
}
