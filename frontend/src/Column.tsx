import type { Interval } from "./api";
import { FALLBACK_STYLE, ft, rockType, useRockInfo, type LithStyle } from "./lith";
import { useSize } from "./useSize";

export interface BedHover {
  idx: number;
  x: number;
  y: number;
}

export type Styles = Map<string, LithStyle>;

const TOP = 22;
const AXIS = 54;
const COL_X = 62;
const COL_W = 120;
const LABEL_W = 70;
/** Width the column needs so the axis, the beds and the cluster numbers all fit. */
export const COLUMN_WIDTH = COL_X + COL_W + LABEL_W;

/** "cluster_8" -> "8". Anything else is shown as given. */
const clusterNo = (id: string | number) => String(id).replace(/^cluster[_\s-]*/i, "");

export function LithColumn({
  rows,
  styles,
  hoverLith,
  selected,
  onHover,
}: {
  rows: Interval[];
  styles: Styles;
  hoverLith: string | null;
  selected: number | null;
  onHover: (h: BedHover | null) => void;
}) {
  const [ref, { h }] = useSize<HTMLDivElement>();
  if (!rows.length)
    return (
      <div ref={ref} className="colwrap">
        <div className="nodata">No beds yet</div>
      </div>
    );

  const y0 = rows[0].top_y;
  const y1 = rows[rows.length - 1].bottom_y;
  const k = Math.max((h - TOP - 12) / (y1 - y0), 0.03);
  const Y = (y: number) => TOP + (y - y0) * k;

  const ticks: Array<{ y: number; d: number }> = [];
  for (const r of rows) {
    if (r.top_depth == null) continue;
    const last = ticks[ticks.length - 1];
    if (!last || Y(r.top_y) - Y(last.y) >= 16) ticks.push({ y: r.top_y, d: r.top_depth });
  }

  return (
    <div ref={ref} className="colwrap">
      <svg width={COLUMN_WIDTH} height={Y(y1) + 12} onMouseLeave={() => onHover(null)}>
        <text x={AXIS} y="14" textAnchor="end" className="col-h">
          DEPTH
        </text>
        <text x={COL_X + COL_W + 6} y="14" className="col-h">
          CLUSTER
        </text>
        <rect x={COL_X} y={TOP} width={COL_W} height={Y(y1) - TOP} fill="#141414" stroke="#555" />
        {ticks.map((t) => (
          <g key={t.y}>
            <line x1={AXIS + 2} x2={COL_X} y1={Y(t.y)} y2={Y(t.y)} stroke="#777" />
            <text x={AXIS} y={Y(t.y) + 3} textAnchor="end" className="ax">
              {Math.round(t.d).toLocaleString()}
            </text>
          </g>
        ))}
        {rows.map((r, i) => {
          const top = Y(r.top_y);
          const hh = Math.max(1, Y(r.bottom_y) - top);
          const dim = hoverLith != null && r.lithology !== hoverLith;
          const st = styles.get(r.lithology) ?? FALLBACK_STYLE;
          return (
            <g key={i} className="bed" opacity={dim ? 0.28 : 1} onMouseMove={(e) => onHover({ idx: i, x: e.clientX, y: e.clientY })}>
              <rect x={COL_X} y={top} width={COL_W} height={hh} fill={st.color} stroke="#000" strokeOpacity=".6" strokeWidth=".6" />
              <rect x={COL_X} y={top} width={COL_W} height={hh} fill={`url(#pat-${st.pattern})`} pointerEvents="none" />
              {hh >= 9 && (
                <text x={COL_X + COL_W + 6} y={top + hh / 2 + 3} className="cl">
                  {clusterNo(r.cluster_id)}
                </text>
              )}
              {selected === i && <rect x={COL_X - 1} y={top} width={COL_W + 2} height={Math.max(2, hh)} className="selbed" />}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

/** Side pane next to the depth column. Fills in for whichever bed (or table row) is hovered. */
export function HoverCard({ rows, idx, styles }: { rows: Interval[]; idx: number | null; styles: Styles }) {
  const bed = idx != null ? rows[idx] : undefined;
  const known = rockType(bed?.lithology);
  const info = useRockInfo(known);
  if (!bed)
    return (
      <aside className="infopane empty">
        <div className="gh">Bed details</div>
        <p>Hover a bed in the column, or a row in the table, to see its depth, thickness and how often this lithology occurs.</p>
      </aside>
    );

  const st = styles.get(bed.lithology) ?? FALLBACK_STYLE;
  const thick = bed.top_depth != null && bed.bottom_depth != null ? bed.bottom_depth - bed.top_depth : null;

  // Everything below comes from the rows themselves.
  const same = rows.filter((r) => r.lithology === bed.lithology);
  const sameThick = same.reduce((a, r) => (r.top_depth != null && r.bottom_depth != null ? a + (r.bottom_depth - r.top_depth) : a), 0);
  const totalPx = rows[rows.length - 1].bottom_y - rows[0].top_y;
  const samePx = same.reduce((a, r) => a + (r.bottom_y - r.top_y), 0);
  const share = totalPx > 0 ? Math.round((samePx / totalPx) * 100) : null;
  const above = idx != null && idx > 0 ? rows[idx - 1].lithology : null;
  const below = idx != null && idx < rows.length - 1 ? rows[idx + 1].lithology : null;

  return (
    <aside className="infopane">
      <div className="hc-title">
        <i style={{ background: st.color }} />
        {bed.lithology}
      </div>
      <dl>
        <dt>Depth</dt>
        <dd>{bed.top_depth != null ? `${ft(bed.top_depth)} – ${ft(bed.bottom_depth)}` : "not read yet"}</dd>
        <dt>Thickness</dt>
        <dd>{ft(thick)}</dd>
        <dt>Cluster</dt>
        <dd>{bed.cluster_id}</dd>
        {bed.confidence != null && (
          <>
            <dt>Confidence</dt>
            <dd>{Math.round(bed.confidence * 100)}%</dd>
          </>
        )}
        <dt>Bed</dt>
        <dd>
          {(idx ?? 0) + 1} of {rows.length}
        </dd>
        <dt>Above</dt>
        <dd>{above ?? "—"}</dd>
        <dt>Below</dt>
        <dd>{below ?? "—"}</dd>
      </dl>
      <div className="gh">This lithology in the log</div>
      <dl>
        <dt>Beds</dt>
        <dd>{same.length}</dd>
        {sameThick > 0 && (
          <>
            <dt>Total thickness</dt>
            <dd>{ft(sameThick)}</dd>
          </>
        )}
        {share != null && (
          <>
            <dt>Share of column</dt>
            <dd>{share}%</dd>
          </>
        )}
      </dl>
      {known && <p>{info ? info.text : "Loading description…"}</p>}
      {known && info && <div className="src">Source: {info.source}</div>}
    </aside>
  );
}
