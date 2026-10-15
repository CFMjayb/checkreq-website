"""
neighborhood_viz.py -- small inline-SVG drawing helpers for the Neighborhood "Snapshot" (26-XXX Neighborhood Insights).

Pure functions: numbers in, SVG text out (marked safe for Jinja). No database, no web code, no chart library. Everything is drawn with
plain shapes so it prints, scales, and needs no scripts. Every shape carries a <title> so hovering shows the exact number.

Colors:
  * CATEGORICAL: eight colors for groups with no natural order (race, tenure...). Checked with the dataviz palette validator
    (color-blind separation, lightness band, chroma) on a white card; three of them are below 3:1 contrast on white, so every chart that
    uses them also prints the labels and numbers beside it (a legend with values), never the color alone.
  * GREEN_RAMP: one hue, light to dark, for ordered groups (generations youngest to oldest, schooling low to high). Validated as an
    ordinal ramp (light end 2.06:1 against white).
  * Color follows the group, never its rank: White is always CATEGORICAL[0] in the race chart, whatever its size.
"""
from __future__ import annotations

import math

from markupsafe import Markup, escape

CATEGORICAL = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948")
GREEN_RAMP = ("#9dbca8", "#79a38a", "#55896c", "#3b6f54", "#2a563f", "#1b3d2c")
NEUTRAL = "#c3c2b7"          # for "vacant", "other" and similar leftovers
TRACK = "#e8e4d8"            # the empty part of a bar
INK = "#1b3d2c"


def ramp_colors(n: int) -> list[str]:
    """n colors from GREEN_RAMP, evenly spaced, still light to dark (a subset of a validated ramp keeps its order and gaps)."""
    if n <= 1:
        return [GREEN_RAMP[-1]]
    if n >= len(GREEN_RAMP):
        return list(GREEN_RAMP[:n]) if n == len(GREEN_RAMP) else list(GREEN_RAMP)
    return [GREEN_RAMP[round(i * (len(GREEN_RAMP) - 1) / (n - 1))] for i in range(n)]


# ------------------------------------------------------------------------------------------------ icons

_ICONS = {
    "people": '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19c0-3.300 2.500-5.500 5.500-5.500s5.500 2.200 5.500 5.500"/><circle cx="17" cy="9" r="2.500"/><path d="M16 14c2.600 0 4.500 1.800 4.500 4.500"/>',
    "growth": '<polyline points="3,17 9,11 13,15 21,7"/><polyline points="15,7 21,7 21,13"/>',
    "house": '<path d="M3 11.500 12 4l9 7.500"/><path d="M5.500 10v10h13V10"/><path d="M10 20v-5h4v5"/>',
    "pair": '<circle cx="8" cy="7" r="2.600"/><circle cx="16" cy="7" r="2.600"/><path d="M3.500 20v-4.500a4.500 4.500 0 0 1 9 0V20"/><path d="M11.500 15.500a4.500 4.500 0 0 1 9 0V20"/>',
    "dollar": '<circle cx="12" cy="12" r="9"/><path d="M12 6.500v11"/><path d="M15 9.300c-.6-.8-1.700-1.300-3-1.300-1.700 0-3 .8-3 2.100 0 3 6 1.500 6 4.300 0 1.300-1.300 2.100-3 2.100-1.400 0-2.500-.5-3.100-1.300"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c3 3 3 15 0 18-3-3-3-15 0-18"/>',
    "speech": '<path d="M4 5h16v10H9l-4 4v-4H4z"/>',
    "cap": '<path d="M2 9l10-5 10 5-10 5z"/><path d="M6 11.500V16c0 1.500 3 3 6 3s6-1.500 6-3v-4.500"/>',
    "briefcase": '<rect x="3" y="8" width="18" height="11" rx="1.500"/><path d="M9 8V5h6v3"/><path d="M3 13h18"/>',
    "car": '<path d="M4 14l1.800-5h12.400L20 14"/><rect x="3" y="14" width="18" height="4.500" rx="1"/><circle cx="7" cy="18.500" r="1.300" fill="currentColor"/><circle cx="17" cy="18.500" r="1.300" fill="currentColor"/>',
    "heart": '<path d="M12 20s-7-4.400-7-10a4 4 0 0 1 7-2.600A4 4 0 0 1 19 10c0 5.600-7 10-7 10z"/>',
    "plus": '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
    "access": '<circle cx="12" cy="5" r="1.700"/><path d="M12 8.500V14"/><path d="M6.500 10.500 12 11.500l5.500-1"/><path d="M9 21l3-7 3 7"/>',
    "wifi": '<path d="M2.500 9a15 15 0 0 1 19 0"/><path d="M5.500 12.500a10 10 0 0 1 13 0"/><path d="M8.800 16a5 5 0 0 1 6.400 0"/><circle cx="12" cy="19.500" r="1" fill="currentColor"/>',
    "percent": '<path d="M5 19 19 5"/><circle cx="7" cy="7" r="2.200"/><circle cx="17" cy="17" r="2.200"/>',
    "school": '<path d="M3 21h18"/><path d="M5 21V9l7-5 7 5v12"/><path d="M10 21v-6h4v6"/><path d="M12 9.500v.1"/>',
    "coin_down": '<circle cx="12" cy="12" r="9"/><path d="M12 7.500v8"/><path d="M9 13l3 3 3-3"/>',
    "building": '<path d="M4 21V6l8-3v18"/><path d="M12 9h8v12"/><path d="M7 9v.1M7 13v.1M7 17v.1M16 13v.1M16 17v.1"/>',
    "pin": '<path d="M12 21s-6-5.500-6-10a6 6 0 0 1 12 0c0 4.500-6 10-6 10z"/><circle cx="12" cy="11" r="2.200"/>',
    "church": '<path d="M12 2.500v4M10 4.500h4"/><path d="M5 21V12l7-4 7 4v9"/><path d="M10 21v-5h4v5"/>',
    "cart": '<path d="M3 4h2.500l2 11h10l2-8H7"/><circle cx="9" cy="19" r="1.300" fill="currentColor"/><circle cx="17" cy="19" r="1.300" fill="currentColor"/>',
    "tooth": '<path d="M7 4c-2.500 0-4 2-4 4.500 0 3 1.500 4 2 7 .4 2.500 1 4.500 2.300 4.500 1.600 0 1.200-4 3.700-4s2.100 4 3.700 4c1.300 0 1.900-2 2.300-4.500.5-3 2-4 2-7C21 6 19.500 4 17 4c-1.700 0-2.500 1-5 1S8.700 4 7 4z"/>',
}


def icon(name: str, size: int = 36) -> Markup:
    body = _ICONS.get(name, _ICONS["people"])
    return Markup(f'<svg class="nb-icon" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                  f'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">{body}</svg>')


# ------------------------------------------------------------------------------------------------ donut


def donut(parts: list[dict], center_big: str = "", center_small: str = "", size: int = 190, ring: int = 30, label: str = "") -> Markup:
    """A ring chart. parts = [{label, value, color, display}] (value > 0 only); returns an SVG with the middle text.
    A 2px gap in the surface color separates the slices; each slice's <title> is its hover text."""
    parts = [p for p in parts if p.get("value") and p["value"] > 0]
    total = sum(p["value"] for p in parts)
    r = (size - ring) / 2 - 1
    c = 2 * math.pi * r
    cx = cy = size / 2
    out = [f'<svg class="nb-donut" viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" aria-label="{escape(label)}">']
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{r:.2f}" fill="none" stroke="{TRACK}" stroke-width="{ring}"/>')
    acc = 0.0
    gap = 2.0 if len(parts) > 1 else 0.0
    for p in parts:
        frac = p["value"] / total
        length = max(frac * c - gap, 0.5)
        dash = f"{length:.2f} {c - length:.2f}"
        offset = -(acc * c) + (gap / 2)
        pct = frac * 100
        tip = f'{p["label"]}: {p.get("display", "")} ({pct:.0f}%)'.replace(": ,", ":")
        out.append(f'<circle cx="{cx}" cy="{cy}" r="{r:.2f}" fill="none" stroke="{p["color"]}" stroke-width="{ring}" '
                   f'stroke-dasharray="{dash}" stroke-dashoffset="{offset:.2f}" transform="rotate(-90 {cx} {cy})"><title>{escape(tip)}</title></circle>')
        acc += frac
    if center_big:
        out.append(f'<text x="{cx}" y="{cy + (2 if center_small else 9)}" text-anchor="middle" class="nb-donut-big">{escape(center_big)}</text>')
    if center_small:
        out.append(f'<text x="{cx}" y="{cy + 20}" text-anchor="middle" class="nb-donut-small">{escape(center_small)}</text>')
    out.append("</svg>")
    return Markup("".join(out))


# ------------------------------------------------------------------------------------------------ waffle


def allocate_cells(values: list[float], cells: int = 100) -> list[int]:
    """Whole cells per part, adding up to exactly `cells` (largest remainder), so a 10 by 10 grid is always full and always honest."""
    total = sum(v for v in values if v and v > 0)
    if total <= 0:
        return [0] * len(values)
    raw = [(v / total * cells) if v and v > 0 else 0.0 for v in values]
    base = [int(x) for x in raw]
    short = cells - sum(base)
    order = sorted(range(len(values)), key=lambda i: (raw[i] - base[i]), reverse=True)
    for i in order[:short]:
        base[i] += 1
    return base


_SHAPES = {
    "square": lambda x, y: f'<rect x="{x}" y="{y}" width="12" height="12" rx="2"/>',
    "person": lambda x, y: (f'<circle cx="{x + 6}" cy="{y + 2.6}" r="2.2"/>'
                            f'<path d="M{x + 1.8} {y + 12}V{y + 8.6}a4.2 4.2 0 0 1 8.4 0V{y + 12}z"/>'),
    "house": lambda x, y: (f'<path d="M{x + 6} {y + 0.4}L{x + 11.8} {y + 5.6}H{x + 0.2}z"/>'
                           f'<rect x="{x + 1.8}" y="{y + 5.6}" width="8.4" height="6.4"/>'),
}


def waffle(parts: list[dict], shape: str = "square", label: str = "") -> Markup:
    """A 10 by 10 grid, one cell = 1% of the whole. parts = [{label, value, color, display}] in the order they fill (row by row).
    Each part is one group with its own <title>, so hovering any cell of a group names it."""
    cell, gap = 12, 3
    side = 10 * cell + 9 * gap
    counts = allocate_cells([p.get("value") or 0 for p in parts])
    draw = _SHAPES.get(shape, _SHAPES["square"])
    groups, idx = [], 0
    for p, n in zip(parts, counts):
        cells = []
        for _ in range(n):
            row, col = divmod(idx, 10)
            cells.append(draw(col * (cell + gap), row * (cell + gap)))
            idx += 1
        tip = f'{p["label"]}: {p.get("display", "")} ({n}%)'.replace(": ,", ":")
        groups.append(f'<g fill="{p["color"]}"><title>{escape(tip)}</title>{"".join(cells)}</g>')
    return Markup(f'<svg class="nb-waffle" viewBox="0 0 {side} {side}" width="{side}" height="{side}" role="img" aria-label="{escape(label)}">'
                  + "".join(groups) + "</svg>")


# ------------------------------------------------------------------------------------------------ bullet


def bullet_scale(value: float | None, benches: list[float | None]) -> float:
    """The full width of a bullet bar: room for the biggest of the numbers shown, plus a little air."""
    nums = [abs(x) for x in [value, *benches] if x is not None]
    top = max(nums) if nums else 1.0
    return (top * 1.3) or 1.0


def bullet(value: float | None, benches: dict[str, float | None], vmax: float, label: str = "") -> Markup:
    """A thin bar for the parish's own number with one tick for each comparison (U.S., state, county). The ticks carry the class
    nb-tick-us / -state / -county so the page can show only the one the reader picked. value or a bench of None draws nothing."""
    if value is None or vmax <= 0:
        return Markup("")
    def x(v):
        return max(0.0, min(100.0, v / vmax * 100.0))
    out = [f'<svg class="nb-bullet" viewBox="0 0 100 12" preserveAspectRatio="none" width="100%" height="12" role="img" aria-label="{escape(label)}">',
           f'<rect x="0" y="4" width="100" height="4" rx="2" fill="{TRACK}"/>',
           f'<rect x="0" y="3" width="{x(value):.2f}" height="6" rx="3" fill="{INK}"><title>{escape(label)}</title></rect>']
    for level, b in benches.items():
        if b is None:
            continue
        out.append(f'<line class="nb-tick nb-tick-{level}" x1="{x(b):.2f}" x2="{x(b):.2f}" y1="0" y2="12" stroke="#eb6834" stroke-width="2" vector-effect="non-scaling-stroke"/>')
    out.append("</svg>")
    return Markup("".join(out))
