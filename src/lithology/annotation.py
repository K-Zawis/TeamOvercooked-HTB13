"""Diagnostic lithology interval overlay."""

from collections.abc import Iterable, Mapping

lazy from PIL import Image, ImageDraw, ImageFont

from .types import LithologyInterval


PALETTE = (
    (40, 160, 60),
    (160, 100, 40),
    (240, 190, 0),
    (150, 60, 170),
    (0, 150, 150),
    (230, 110, 20),
    (110, 110, 110),
)


def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _colors(intervals: tuple[LithologyInterval, ...]):
    labels = []
    for interval in intervals:
        label = interval.lithology or "UNKNOWN"
        if label not in labels:
            labels.append(label)
    return {label: PALETTE[index % len(PALETTE)] for index, label in enumerate(labels)}


def annotate_lithology(
    image,
    intervals: Iterable[LithologyInterval],
    column_box: tuple[int, int, int, int],
    *,
    origin: tuple[int, int] = (0, 0),
    colors: Mapping[str, tuple[int, int, int]] | None = None,
    opacity: int = 100,
) -> Image.Image:
    """Overlay classified intervals on a full image or an image crop.

    ``column_box`` and interval coordinates are global. ``origin`` is the global
    coordinate of the supplied image's top-left corner.
    """
    rows = tuple(intervals)
    if not rows:
        raise ValueError("intervals cannot be empty")
    if any(not isinstance(interval, LithologyInterval) for interval in rows):
        raise ValueError("intervals must contain LithologyInterval values")
    if not isinstance(column_box, tuple) or len(column_box) != 4:
        raise ValueError("column_box must be an (x0, y0, x1, y1) tuple")
    if any(isinstance(value, bool) or not isinstance(value, int)
           for value in (*column_box, *origin)):
        raise ValueError("column_box and origin coordinates must be integers")
    left, top, right, bottom = column_box
    if left < 0 or top < 0 or right <= left or bottom <= top:
        raise ValueError("column_box must be a positive max-exclusive range")
    if isinstance(opacity, bool) or not isinstance(opacity, int) or not 0 <= opacity <= 255:
        raise ValueError("opacity must be an integer in [0, 255]")

    base = image.copy().convert("RGBA") if isinstance(image, Image.Image) else Image.fromarray(image).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _font(12)
    color_map = dict(colors) if colors is not None else _colors(rows)
    x0, x1 = left - origin[0], right - origin[0]
    for interval in rows:
        label = interval.lithology or "UNKNOWN"
        if label not in color_map:
            raise ValueError(f"missing annotation color for {label!r}")
        color = tuple(color_map[label])
        if len(color) != 3 or any(not isinstance(value, int) or not 0 <= value <= 255 for value in color):
            raise ValueError("annotation colors must be RGB integer triples")
        y0, y1 = interval.top_y - origin[1], interval.bottom_y - origin[1]
        if y1 <= 0 or y0 >= base.height:
            continue
        y0, y1 = max(0, y0), min(base.height, y1)
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=color + (opacity,))
        draw.line((x0, y0, x1 - 1, y0), fill=color + (255,), width=1)
        text = f"{label}  {interval.confidence:.2f}"
        draw.text((x1 + 4, y0 + 2), text, fill=color + (255,), font=font,
                  stroke_width=2, stroke_fill=(255, 255, 255, 255))
    return Image.alpha_composite(base, overlay).convert("RGB")
