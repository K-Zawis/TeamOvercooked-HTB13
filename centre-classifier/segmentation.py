"""Refine coarse intervals at color and motif changes before classification."""

import math
from statistics import median

import numpy as np

import interval_legend as base


def _runs(values):
    start = 0
    for end in range(1, len(values) + 1):
        if end == len(values) or values[end] != values[start]:
            yield start, end, values[start]
            start = end


def _backgrounds(read, left, right, top, bottom):
    # Horizontal quantiles reject most black symbol ink without smoothing Y:
    # even a one-row colored interbed remains present in this signal.
    result = np.empty((bottom - top, 3), dtype=np.float32)
    for y in range(top, bottom, 2048):
        end = min(bottom, y + 2048)
        pixels = np.asarray(read([left, y, right, end]).convert('RGB'))
        result[y - top:end - top] = np.quantile(pixels, .8, axis=1) / 255
    # A one/two-row dark motif stroke is not a new lithology. Repair it only
    # when its two neighbors have the same background; retain longer dark beds.
    dark = np.max(result, axis=1) < .25
    for a, b, value in _runs(dark):
        if value and b - a <= 2 and a > 0 and b < len(result):
            if np.linalg.norm(result[a - 1] - result[b]) < .15:
                result[a:b] = (result[a - 1] + result[b]) / 2
    return result


def _color_groups(refs):
    colors = []
    for ref in refs:
        color = np.quantile(np.asarray(ref['im']), .8, axis=(0, 1)) / 255
        if not any(np.linalg.norm(color - previous) < .12 for previous in colors):
            colors.append(color)
    return np.asarray(colors)


def _texture_boundaries(read, left, right, top, bottom, refs, motif_height):
    """Locate motif changes within one background-color run using local windows."""
    if bottom - top < 2 * motif_height:
        return [top, bottom]
    half = motif_height // 2
    centres = list(range(top + half, bottom - motif_height + half + 1,
                         max(1, motif_height // 4)))
    if centres[-1] != bottom - motif_height + half:
        centres.append(bottom - motif_height + half)
    winners = []
    for centre in centres:
        start = centre - half
        image = read([left, start, right, start + motif_height])
        winners.append(base.classify([image], refs)['proposal'])
    # Suppress isolated phase-dependent motif votes, without touching color
    # boundaries (which were found independently at native row resolution).
    original = list(winners)
    for a, b, _ in _runs(original):
        if b - a == 1 and a > 0 and b < len(original) and original[a - 1] == original[b]:
            winners[a] = original[a - 1]
    boundaries = [top]
    for a, _, _ in list(_runs(winners))[1:]:
        boundaries.append((centres[a - 1] + centres[a]) // 2)
    return boundaries + [bottom]


def _depth(parent, y):
    if y == parent['top_y']:
        return parent.get('top_depth', '')
    if y == parent['bottom_y']:
        return parent.get('bottom_depth', '')
    try:
        top, bottom = float(parent['top_depth']), float(parent['bottom_depth'])
    except (KeyError, TypeError, ValueError):
        return ''
    if not math.isfinite(top) or not math.isfinite(bottom) or bottom <= top:
        return ''
    fraction = (y - parent['top_y']) / (parent['bottom_y'] - parent['top_y'])
    return f'{top + fraction * (bottom - top):.3f}'


def refine_intervals(rows, box, read, refs):
    """Cover every input row with independently classifiable child segments.

    Color boundaries use individual source rows; texture boundaries use local
    motif-height windows. No minimum bed thickness is imposed on color changes.
    """
    base.validate_rows(rows)
    x0, y0, x1, y1 = box
    trim = max(1, round((x1 - x0) * .08))
    left, right = x0 + trim, x1 - trim
    if right <= left:
        raise ValueError('track too narrow')
    colors = _color_groups(refs)
    height = max(1, round(median(ref['im'].height for ref in refs)))
    refined = []
    parents = []
    cursor, last_depth = y0, ''
    for index, row in enumerate(rows):
        if not y0 <= row['top_y'] < row['bottom_y'] <= y1:
            raise ValueError('interval outside track')
        if row['top_y'] > cursor:
            parents.append((f'gap{index:05d}', dict(top_y=cursor, bottom_y=row['top_y'],
                top_depth=last_depth, bottom_depth=row.get('top_depth', ''),
                lithology='unknown', segmentation_lithology='unknown', confidence='', cluster_id='')))
        parents.append((f'p{index:05d}', row))
        cursor, last_depth = row['bottom_y'], row.get('bottom_depth', '')
    if cursor < y1:
        parents.append(('gap_end', dict(top_y=cursor, bottom_y=y1,
            top_depth=last_depth, bottom_depth='', lithology='unknown',
            segmentation_lithology='unknown', confidence='', cluster_id='')))
    for parent_id, row in parents:
        top, bottom = row['top_y'], row['bottom_y']
        if not y0 <= top < bottom <= y1:
            raise ValueError('interval outside track')
        backgrounds = _backgrounds(read, left, right, top, bottom)
        groups = np.argmin(np.linalg.norm(backgrounds[:, None, :] - colors[None, :, :], axis=2), axis=1)
        boundaries = [top]
        for a, b, _ in _runs(groups):
            local = _texture_boundaries(read, left, right, top + a, top + b, refs, height)
            boundaries.extend(local[1:])
        for start, end in zip(boundaries, boundaries[1:]):
            child = dict(row, top_y=start, bottom_y=end,
                         parent_interval_id=parent_id, parent_top_y=top, parent_bottom_y=bottom)
            if 'top_depth' in row or 'bottom_depth' in row:
                child.update(top_depth=_depth(row, start), bottom_depth=_depth(row, end),
                             parent_top_depth=row.get('top_depth', ''),
                             parent_bottom_depth=row.get('bottom_depth', ''),
                             depth_method='parent_endpoints' if start == top and end == bottom
                             else 'linear_within_parent' if _depth(row, start) != '' and _depth(row, end) != ''
                             else 'unsupported_internal_depth')
            refined.append(child)
    base.validate_rows(refined)
    return refined
