/**
 * Texture overlays for the lithology column, referenced as `url(#pat-<n>)`. They are transparent with dark
 * strokes, so they sit on top of any palette colour: the column draws the colour first, then the texture.
 */
const ink = "#0b0f14";

export function HatchDefs() {
  const p = { patternUnits: "userSpaceOnUse" as const };
  const st = { stroke: ink, strokeOpacity: 0.6, fill: "none", strokeWidth: 1.1 };
  return (
    <svg width="0" height="0" style={{ position: "absolute" }} aria-hidden>
      <defs>
        {/* 0 horizontal dashes */}
        <pattern id="pat-0" width="10" height="6" {...p}>
          <path d="M0 1.5h6M5 4.5h5" {...st} />
        </pattern>
        {/* 1 dots */}
        <pattern id="pat-1" width="7" height="7" {...p}>
          <circle cx="1.8" cy="1.8" r="1" fill={ink} fillOpacity=".6" />
          <circle cx="5.3" cy="5.3" r="1" fill={ink} fillOpacity=".6" />
        </pattern>
        {/* 2 bricks */}
        <pattern id="pat-2" width="12" height="8" {...p}>
          <path d="M0 4h12M3 0v4M9 4v4" {...st} />
        </pattern>
        {/* 3 diagonal */}
        <pattern id="pat-3" width="7" height="7" {...p}>
          <path d="M-1 8L8 -1M-1 1L1 -1M6 8L8 6" {...st} />
        </pattern>
        {/* 4 cross-hatch */}
        <pattern id="pat-4" width="8" height="8" {...p}>
          <path d="M0 0L8 8M8 0L0 8" {...st} />
        </pattern>
        {/* 5 vertical lines */}
        <pattern id="pat-5" width="6" height="6" {...p}>
          <path d="M1.5 0v6" {...st} />
        </pattern>
        {/* 6 chevrons */}
        <pattern id="pat-6" width="10" height="7" {...p}>
          <path d="M0 1l5 4 5-4" {...st} />
        </pattern>
        {/* 7 rings */}
        <pattern id="pat-7" width="10" height="10" {...p}>
          <circle cx="5" cy="5" r="2.6" {...st} />
        </pattern>
        {/* 8 waves */}
        <pattern id="pat-8" width="12" height="6" {...p}>
          <path d="M0 3q3-3 6 0t6 0" {...st} />
        </pattern>
        {/* 9 plus */}
        <pattern id="pat-9" width="9" height="9" {...p}>
          <path d="M4.5 2v5M2 4.5h5" {...st} />
        </pattern>
      </defs>
    </svg>
  );
}
