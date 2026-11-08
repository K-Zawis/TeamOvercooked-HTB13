"""Render classified intervals over the original scan, in bounded-memory bands."""

import colorsys
import hashlib
import struct
import zlib

from PIL import ImageDraw, ImageFont

import interval_legend as base

VERSION = "labelled-overlay-1"
BAND_HEIGHT = 512


def label_color(label):
    if label.lower() in ("unknown", "mixed", "no_reliable_match", "blank_or_operational"):
        return (128, 128, 128)
    hue = int.from_bytes(hashlib.sha256(label.encode("utf-8")).digest()[:2], "big") / 65536
    return tuple(round(v * 255) for v in colorsys.hsv_to_rgb(hue, .65, .9))


def _chunk(file, kind, data):
    file.write(struct.pack(">I", len(data)))
    file.write(kind)
    file.write(data)
    file.write(struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))


def _font():
    try:
        return ImageFont.truetype("DejaVuSans.ttf", 18)
    except OSError:
        return ImageFont.load_default(size=18)


def render_overlay(source, rows, box, destination):
    """Write a full-resolution RGB PNG; mask only the annotated lithology track.

    The PNG encoder streams unfiltered scanlines through zlib, so even long
    TIFFs do not require a full-size decoded image or mask in memory.
    """
    base.validate_rows(rows)
    raster = base.Raster(source)
    try:
        width, height = raster.size
        x0, y0, x1, y1 = box
        if not 0 <= x0 < x1 <= width or not 0 <= y0 < y1 <= height:
            raise ValueError("overlay track lies outside source image")
        font = _font()
        labels = []
        for row in rows:
            top, bottom = row["top_y"], row["bottom_y"]
            if not y0 <= top < bottom <= y1:
                raise ValueError("overlay interval lies outside lithology track")
            name = row.get("lithology") or "unknown"
            bounds = font.getbbox(name)
            text_width, text_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
            label_width, label_height = text_width + 12, text_height + 8
            left = x1 + 12
            if left + label_width > width:
                left = max(0, x0 - label_width - 12)
            left = max(0, min(left, width - label_width))
            label_top = max(0, min((top + bottom - label_height) // 2, height - label_height))
            labels.append((name, bounds, left, label_top, label_width, label_height,
                           label_color(name), top, bottom))

        with open(destination, "wb") as file:
            file.write(b"\x89PNG\r\n\x1a\n")
            _chunk(file, b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            compressor = zlib.compressobj(level=6)
            for band_top in range(0, height, BAND_HEIGHT):
                band_bottom = min(height, band_top + BAND_HEIGHT)
                image = raster.read([0, band_top, width, band_bottom]).convert("RGB")
                draw = ImageDraw.Draw(image, "RGBA")
                for name, bounds, left, label_top, lw, lh, color, top, bottom in labels:
                    if top < band_bottom and bottom > band_top:
                        # Pillow rectangles have inclusive ends; interval bounds do not.
                        draw.rectangle((x0, top - band_top, x1 - 1, bottom - 1 - band_top),
                                       fill=(*color, 85), outline=(*color, 230), width=1)
                    if label_top < band_bottom and label_top + lh > band_top:
                        centre_y = (top + bottom) // 2 - band_top
                        anchor = x1 if left >= x1 else x0
                        label_edge = left if left >= x1 else left + lw
                        draw.line((anchor, centre_y, label_edge, centre_y), fill=(*color, 255), width=1)
                        draw.rectangle((left, label_top - band_top, left + lw - 1, label_top + lh - 1 - band_top),
                                       fill=(255, 255, 255, 235), outline=(*color, 255), width=1)
                        draw.text((left + 6 - bounds[0], label_top + 4 - bounds[1] - band_top),
                                  name, font=font, fill=(0, 0, 0, 255))
                pixels = image.tobytes()
                stride = width * 3
                scanlines = b"".join(b"\0" + pixels[start:start + stride]
                                      for start in range(0, len(pixels), stride))
                data = compressor.compress(scanlines)
                if data:
                    _chunk(file, b"IDAT", data)
            _chunk(file, b"IDAT", compressor.flush())
            _chunk(file, b"IEND", b"")
    finally:
        raster.close()
