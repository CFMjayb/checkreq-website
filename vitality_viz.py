"""
vitality_viz.py -- the small drawings on the diocese's Congregational Vitality lists (26-XXX Congregational Vitality, phase 4).

One thing: a sparkline of a parish's last ten years of Sunday attendance, drawn on the server as plain SVG (no script, no library),
so it prints and works with scripts off. The line is one series, so it needs no legend; its direction is also written in words beside it
in the table, and its first and last values are in the picture's title and aria-label, so the picture is never the only carrier of the
information (color alone is never relied on).

Pure: numbers in, a markup-safe string out. Every number is formatted here; no user text goes into the SVG.
"""
from __future__ import annotations

from markupsafe import Markup

W, H, PAD = 96, 26, 3
LINE = "#2f5d3a"      # the same green the Neighborhood page's drive-time shapes use


def sparkline(series: list[tuple[int, float]], label: str = "Sunday attendance") -> Markup:
    """`series` = [(year, value), ...] oldest first, blanks already left out. Fewer than two points draws nothing (a line of one
    point says nothing), and a flat series is drawn as a flat line in the middle. Returns "" when there is nothing to draw."""
    pts = [(int(y), float(v)) for y, v in series if v is not None]
    if len(pts) < 2:
        return Markup("")
    years = [y for y, _ in pts]
    vals = [v for _, v in pts]
    y0, y1 = min(years), max(years)
    lo, hi = min(vals), max(vals)
    span_y = (y1 - y0) or 1
    span_v = (hi - lo)

    def xy(year: int, v: float) -> tuple[float, float]:
        x = PAD + (year - y0) / span_y * (W - 2 * PAD)
        y = (H / 2) if span_v == 0 else H - PAD - (v - lo) / span_v * (H - 2 * PAD)
        return round(x, 1), round(y, 1)

    coords = [xy(y, v) for y, v in pts]
    poly = " ".join(f"{x},{y}" for x, y in coords)
    ex, ey = coords[-1]
    text = f"{label}: {pts[0][1]:,.0f} in {pts[0][0]} to {pts[-1][1]:,.0f} in {pts[-1][0]}"
    return Markup(
        f'<svg class="vt-spark" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="{text}">'
        f"<title>{text}</title>"
        f'<polyline points="{poly}" fill="none" stroke="{LINE}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{ex}" cy="{ey}" r="2.6" fill="{LINE}" stroke="#fff" stroke-width="1"/>'
        f"</svg>"
    )
