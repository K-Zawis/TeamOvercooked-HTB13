import type { Interval } from "./api";
import { ft, lithColor, patternFill, useRockInfo } from "./lith";
import { useSize } from "./useSize";

export interface BedHover {
  idx: number;
  x: number;
  y: number;
}

const TOP = 22;
const AXIS = 54;
const COL_X = 62;
const COL_W = 96;

export function LithColumn({
  rows,
  hoverLith,
  selected,
  onHover,
}: {
  rows: Interval[];
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
      <svg width={COL_X + COL_W + 40} height={Y(y1) + 12} onMouseLeave={() => onHover(null)}>
        <text x={AXIS} y="14" textAnchor="end" className="col-h">
          DEPTH (ft)
        </text>
        <text x={COL_X + COL_W + 6} y="14" className="col-h">
          CL
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
          return (
            <g
              key={i}
              className="bed"
              opacity={dim ? 0.28 : 1}
              onMouseMove={(e) => onHover({ idx: i, x: e.clientX, y: e.clientY })}
            >
              <rect x={COL_X} y={top} width={COL_W} height={hh} fill={patternFill(r.lithology)} stroke="#000" strokeOpacity=".6" strokeWidth=".6" />
              {hh > 11 && (
                <text x={COL_X + COL_W + 6} y={top + hh / 2 + 3} className="cl">
                  {r.cluster_id}
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
export function HoverCard({ bed }: { bed?: Interval }) {
  const info = useRockInfo(bed?.lithology ?? null);
  if (!bed)
    return (
      <aside className="infopane empty">
        <div className="gh">Rock type</div>
        <p>Hover a bed in the column, or a row in the table, to see its rock type, depth and description here.</p>
      </aside>
    );
  const thick = bed.top_depth != null && bed.bottom_depth != null ? bed.bottom_depth - bed.top_depth : null;
  return (
    <aside className="infopane">
      <div className="hc-title">
        <i style={{ background: lithColor(bed.lithology) }} />
        {bed.lithology}
      </div>
      <dl>
        <dt>Depth</dt>
        <dd>{bed.top_depth != null ? `${ft(bed.top_depth)} – ${ft(bed.bottom_depth)} ft` : "not read yet"}</dd>
        <dt>Thickness</dt>
        <dd>{thick != null ? `${ft(thick)} ft` : `${Math.round((bed.bottom_y - bed.top_y) / 40)} ft (est.)`}</dd>
        <dt>Cluster</dt>
        <dd>{bed.cluster_id}</dd>
        {bed.confidence != null && (
          <>
            <dt>Confidence</dt>
            <dd>{Math.round(bed.confidence * 100)}%</dd>
          </>
        )}
      </dl>
      <p>{info ? info.text : "Loading description…"}</p>
      {info && <div className="src">Source: {info.source}</div>}
    </aside>
  );
}
