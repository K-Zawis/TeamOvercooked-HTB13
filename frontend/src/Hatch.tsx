import { lithColor } from "./lith";

/** Printed-legend style fills for the lithology column. Referenced by id (`hatch-<name>`). */
export function HatchDefs() {
  const c = (n: string) => lithColor(n);
  return (
    <svg width="0" height="0" style={{ position: "absolute" }} aria-hidden>
      <defs>
        <pattern id="hatch-shale" width="8" height="5" patternUnits="userSpaceOnUse">
          <rect width="8" height="5" fill={c("Shale")} />
          <path d="M0 1.2h3.4M4.6 3.7H8" stroke="#0b1218" strokeWidth="1" opacity=".7" />
        </pattern>
        <pattern id="hatch-siltstone" width="8" height="6" patternUnits="userSpaceOnUse">
          <rect width="8" height="6" fill={c("Siltstone")} />
          <path d="M0 1.6h5" stroke="#2b1700" strokeWidth="1" opacity=".75" />
          <circle cx="6.6" cy="4.4" r=".8" fill="#2b1700" opacity=".85" />
          <circle cx="2" cy="4.6" r=".7" fill="#2b1700" opacity=".6" />
        </pattern>
        <pattern id="hatch-sandstone" width="6" height="6" patternUnits="userSpaceOnUse">
          <rect width="6" height="6" fill={c("Sandstone")} />
          <circle cx="1.5" cy="1.5" r=".8" fill="#012219" opacity=".8" />
          <circle cx="4.5" cy="4.2" r=".8" fill="#012219" opacity=".8" />
          <circle cx="4.7" cy="1" r=".45" fill="#012219" opacity=".5" />
        </pattern>
        <pattern id="hatch-limestone" width="12" height="8" patternUnits="userSpaceOnUse">
          <rect width="12" height="8" fill={c("Limestone")} />
          <path d="M0 4h12M3 0v4M9 4v4" stroke="#37331f" strokeWidth=".9" opacity=".7" fill="none" />
        </pattern>
      </defs>
    </svg>
  );
}
