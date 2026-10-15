"""
sustain_viz.py -- inline-SVG charts and tables for the Parish Sustainability Review (26-XXX Congregational Vitality).

Pure functions: numbers in, HTML/SVG text out (marked safe for Jinja). No database, no web code, no chart library, no scripts needed to
read a chart; every point carries a <title> so hovering shows the exact number. Same approach as neighborhood_viz.py.

Rules followed (dataviz skill):
  * Series colors are categorical slots 1-3 (blue, orange, aqua), validated together on white (worst color-blind separation 9.2, normal
    vision 27.6). Aqua is under 3:1 on white, so every chart also has a legend with the latest value, an end label, and a Table button.
  * Color is never the only channel: each series has its own dash pattern and marker shape, and the legend key repeats both.
  * 2px lines, markers of 8px or more with a 2px white ring, hairline solid gridlines, text in ink (never in the series color).
  * One axis per chart. Two measures of different scale are two charts.
  * A gray band marks 2020 and 2021 (pandemic years); a dashed vertical line marks 2024 (the parochial-report form changed).
"""
from __future__ import annotations

import math

from markupsafe import Markup, escape

BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
SURFACE, GRID, AXIS, MUTED, INK, INK2 = "#ffffff", "#e1e0d9", "#c3c2b7", "#898781", "#1c2621", "#52514e"
BAND = "#ece8dc"
GOLD = "#c9a227"
FORM_CHANGE_YEAR = 2024
PANDEMIC_YEARS = (2020, 2021)

SERIES_STYLES = (  # (color, dash, marker): a second channel for every series
    (BLUE, None, "circle"),
    (ORANGE, "6 4", "square"),
    (AQUA, "2 4", "diamond"),
)

W, H = 330, 176
L, R, T, B = 46, 46, 16, 24


# ------------------------------------------------------------------------------------------------ number formats

def money_short(v: float) -> str:
    """$281k, $1.2M, $950, -$12k"""
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1_000_000:
        s = f"{a / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    elif a >= 1000:
        s = f"{a / 1000:.0f}k"
    else:
        s = f"{a:.0f}"
    return f"{sign}${s}"


def money_full(v: float) -> str:
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def count_fmt(v: float) -> str:
    return f"{v:,.0f}"


FORMATS = {"money": (money_short, money_full), "count": (count_fmt, count_fmt)}


def nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    """Round-number tick values covering [lo, hi]."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.001:
        ticks.append(round(v, 10))
        v += step
    if ticks[-1] < hi:
        ticks.append(round(ticks[-1] + step, 10))
    return ticks


def _marker(kind: str, x: float, y: float, color: str, title: str) -> str:
    ring = f'fill="{color}" stroke="{SURFACE}" stroke-width="2"'
    if kind == "square":
        shape = f'<rect x="{x - 4:.1f}" y="{y - 4:.1f}" width="8" height="8" rx="1" {ring}/>'
    elif kind == "diamond":
        shape = f'<polygon points="{x:.1f},{y - 5.5:.1f} {x + 5.5:.1f},{y:.1f} {x:.1f},{y + 5.5:.1f} {x - 5.5:.1f},{y:.1f}" {ring}/>'
    else:
        shape = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" {ring}/>'
    return f"{shape}<title>{escape(title)}</title>"


def _key(color: str, dash: str | None, marker: str) -> str:
    """The little line-and-marker sample in a legend entry."""
    line = f'<line x1="1" x2="23" y1="7" y2="7" stroke="{color}" stroke-width="2" stroke-linecap="round"' + (f' stroke-dasharray="{dash}"' if dash else "") + "/>"
    return f'<svg class="sv-key" width="24" height="14" viewBox="0 0 24 14" aria-hidden="true">{line}{_marker(marker, 12, 7, color, "")}</svg>'


# ------------------------------------------------------------------------------------------------ line chart

def line_chart(series: list[dict], years: list[int], *, fmt: str = "count", label: str, show_markers: bool | None = None) -> Markup:
    """series: [{"name", "values": {year: number | None}, "style": index into SERIES_STYLES}]. A missing year breaks the line (a gap,
    never a zero). Years must be consecutive. Returns one <svg>."""
    short, full = FORMATS[fmt]
    xs = {y: (L + (W - L - R) * (i / (len(years) - 1) if len(years) > 1 else 0.5)) for i, y in enumerate(years)}
    step = (W - L - R) / (len(years) - 1) if len(years) > 1 else 40
    vals = [v for s in series for y, v in s["values"].items() if y in xs and v is not None]
    lo = min(0.0, min(vals)) if vals else 0.0
    hi = max(vals) if vals else 1.0
    ticks = nice_ticks(lo, hi * 1.04 if hi > 0 else 1.0)
    ymin, ymax = ticks[0], ticks[-1]

    def ypos(v: float) -> float:
        return T + (H - T - B) * (1 - (v - ymin) / (ymax - ymin))

    marks_every_point = (len(years) <= 12) if show_markers is None else show_markers
    out: list[str] = []

    if all(y in xs for y in PANDEMIC_YEARS):  # gray band, half a step either side so it covers the two year columns
        x0, x1 = max(L, xs[PANDEMIC_YEARS[0]] - step / 2), min(W - R, xs[PANDEMIC_YEARS[1]] + step / 2)
        out.append(f'<rect x="{x0:.1f}" y="{T}" width="{x1 - x0:.1f}" height="{H - T - B}" fill="{BAND}"><title>2020 and 2021: the pandemic years</title></rect>')

    for t in ticks:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{ypos(t):.1f}" y2="{ypos(t):.1f}" stroke="{AXIS if t == 0 else GRID}" stroke-width="1"/>'
                   f'<text x="{L - 6}" y="{ypos(t) + 3.5:.1f}" text-anchor="end" class="sv-ax">{escape(short(t))}</text>')

    if FORM_CHANGE_YEAR in xs:
        out.append(f'<line x1="{xs[FORM_CHANGE_YEAR]:.1f}" x2="{xs[FORM_CHANGE_YEAR]:.1f}" y1="{T}" y2="{H - B}" stroke="{MUTED}" stroke-width="1" stroke-dasharray="3 3"><title>2024: the parochial report form changed</title></line>'
                   f'<text x="{xs[FORM_CHANGE_YEAR]:.1f}" y="{T - 5}" text-anchor="middle" class="sv-ax">2024 form</text>')

    last_year = years[-1]
    label_step = 2 if len(years) <= 12 else 5
    for y in years:
        if (last_year - y) % label_step == 0:
            out.append(f'<text x="{xs[y]:.1f}" y="{H - 7}" text-anchor="middle" class="sv-ax">{y}</text>')

    placed_labels: list[float] = []
    for s in series:
        color, dash, marker = SERIES_STYLES[s["style"]]
        pts = [(y, s["values"].get(y)) for y in years]
        segment: list[tuple[float, float]] = []
        for y, v in pts + [(None, None)]:
            if v is not None:
                segment.append((xs[y], ypos(v)))
                continue
            if len(segment) > 1:
                d = " ".join(f"{x:.1f},{py:.1f}" for x, py in segment)
                out.append(f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
                           + (f' stroke-dasharray="{dash}"' if dash else "") + "/>")
            segment = []
        have = [(y, v) for y, v in pts if v is not None]
        for i, (y, v) in enumerate(have):
            is_last = i == len(have) - 1
            tip = f"{y} {s['name']}: {full(v)}"
            if marks_every_point or is_last:
                out.append(_marker(marker, xs[y], ypos(v), color, tip))
            else:  # a transparent target bigger than the dot, so every year can be hovered
                out.append(f'<circle cx="{xs[y]:.1f}" cy="{ypos(v):.1f}" r="7" fill="transparent"><title>{escape(tip)}</title></circle>')
        if have:  # end label, in ink; skipped when it would land on another series' label
            y, v = have[-1]
            py = ypos(v)
            if all(abs(py - p) >= 12 for p in placed_labels):
                placed_labels.append(py)
                out.append(f'<text x="{xs[y] + 9:.1f}" y="{py + 3.5:.1f}" class="sv-end">{escape(short(v))}</text>')
    svg = (f'<svg class="sv-svg" viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="{escape(label)}">' + "".join(out) + "</svg>")
    return Markup(svg)


def legend(series: list[dict], years: list[int], fmt: str) -> Markup:
    """One entry per series that has a value in the shown years: line-and-marker key, name, latest value. None for a single series."""
    _, full = FORMATS[fmt]
    shown = []
    for s in series:
        have = [(y, s["values"].get(y)) for y in years if s["values"].get(y) is not None]
        if have:
            shown.append((s, have[-1]))
    if len(shown) < 2:
        return Markup("")
    items = "".join(
        f'<li>{_key(*SERIES_STYLES[s["style"]])}<span class="sv-lg-name">{escape(s["name"])}</span>'
        f'<span class="sv-lg-val">{escape(full(v))} <span class="sv-lg-yr">({y})</span></span></li>'
        for s, (y, v) in shown)
    return Markup(f'<ul class="sv-legend">{items}</ul>')


def data_table(years: list[int], columns: list[tuple[str, dict]], fmt: str) -> Markup:
    """The Table view: one row per year (newest first), one column per series. 'n/a' for a year with no number."""
    _, full = FORMATS[fmt]
    head = "".join(f"<th scope=\"col\" class=\"num\">{escape(name)}</th>" for name, _ in columns)
    body = "".join(
        f'<tr><th scope="row">{y}</th>' + "".join(f'<td class="num">{escape(full(v)) if (v := vals.get(y)) is not None else "n/a"}</td>' for _, vals in columns) + "</tr>"
        for y in reversed(years))
    return Markup(f'<table class="sv-table-el"><thead><tr><th scope="col">Year</th>{head}</tr></thead><tbody>{body}</tbody></table>')


def chart_card(uid: str, title: str, series: list[dict], years_all: list[int], *, fmt: str, note: str = "", latest_year: int | None = None) -> Markup:
    """One chart block: title, a 10 years / All years switch, a Table button, the chart(s), legend, table, and a note.
    Both ranges are drawn; the page script shows one at a time (without it, the 10-year view shows and the rest stays hidden)."""
    latest_year = latest_year or years_all[-1]
    years_ten = [y for y in years_all if y >= latest_year - 10]
    views = []
    for key, yrs in (("ten", years_ten), ("all", years_all)):
        cols = [(s["name"], s["values"]) for s in series if any(s["values"].get(y) is not None for y in yrs)]
        views.append(
            f'<div class="sv-view" data-view="{key}"{"" if key == "ten" else " hidden"}>'
            f'<div class="sv-plot">{line_chart(series, yrs, fmt=fmt, label=f"{title}, {yrs[0]} to {yrs[-1]}")}</div>'
            f'{legend(series, yrs, fmt)}'
            f'<div class="sv-table" hidden>{data_table(yrs, cols, fmt)}</div></div>')
    switch = ('<div class="sv-seg" role="group" aria-label="Years shown">'
              '<button type="button" class="sv-range is-active" data-range="ten" aria-pressed="true">10 years</button>'
              '<button type="button" class="sv-range" data-range="all" aria-pressed="false">All years</button></div>')
    return Markup(
        f'<section class="card sv-chart" data-range="ten" data-mode="chart" aria-labelledby="{escape(uid)}-h">'
        f'<div class="sv-chart-head"><h3 id="{escape(uid)}-h">{escape(title)}</h3>'
        f'<div class="sv-tools">{switch}<button type="button" class="sv-tbtn" aria-pressed="false">Table</button></div></div>'
        + "".join(views) + (f'<p class="sub sv-note">{escape(note)}</p>' if note else "") + "</section>")


# ------------------------------------------------------------------------------------------------ the projection chart

def projection_chart(rows: list[dict], safe_draw: float) -> Markup:
    """Yearly surplus or deficit as bars from a zero line (up = surplus, down = deficit), with the safe-draw level as a dashed line:
    a deficit that reaches below it is more than a safe endowment draw would cover."""
    pw, ph, left, right, top, bottom = 560, 230, 54, 14, 12, 28
    nets = [r["net"] for r in rows]
    hi = max([0.0] + nets)
    lo = min([0.0, -safe_draw] + nets)
    ticks = nice_ticks(lo * 1.08, max(hi * 1.08, 1000.0))
    ymin, ymax = ticks[0], ticks[-1]

    def ypos(v: float) -> float:
        return top + (ph - top - bottom) * (1 - (v - ymin) / (ymax - ymin))

    slot = (pw - left - right) / len(rows)
    barw = min(24.0, slot * 0.5)
    out = []
    for t in ticks:
        out.append(f'<line x1="{left}" x2="{pw - right}" y1="{ypos(t):.1f}" y2="{ypos(t):.1f}" stroke="{AXIS if t == 0 else GRID}" stroke-width="1"/>'
                   f'<text x="{left - 6}" y="{ypos(t) + 3.5:.1f}" text-anchor="end" class="sv-ax">{escape(money_short(t))}</text>')
    for i, r in enumerate(rows):
        cx = left + slot * (i + 0.5)
        y0, y1 = ypos(0), ypos(r["net"])
        deficit = r["net"] < 0
        color = ORANGE if deficit else BLUE
        top_y, h = min(y0, y1), max(2.0, abs(y1 - y0))
        # 4px rounded data end, square at the zero line: a rounded rect plus a square block over the baseline end
        out.append(f'<rect x="{cx - barw / 2:.1f}" y="{top_y:.1f}" width="{barw:.1f}" height="{h:.1f}" rx="4" fill="{color}"/>'
                   f'<rect x="{cx - barw / 2:.1f}" y="{(top_y if deficit else top_y + h - 4):.1f}" width="{barw:.1f}" height="4" fill="{color}"/>'
                   f'<rect x="{cx - slot / 2:.1f}" y="{top}" width="{slot:.1f}" height="{ph - top - bottom}" fill="transparent">'
                   f'<title>{r["year"]}: {escape(("deficit " if deficit else "surplus ") + money_full(abs(r["net"])))}</title></rect>')
        # direct label at the bar's free end (value, in ink)
        ly = y1 + 13 if deficit else y1 - 5
        out.append(f'<text x="{cx:.1f}" y="{ly:.1f}" text-anchor="middle" class="sv-end">{escape(money_short(r["net"]))}</text>')
        out.append(f'<text x="{cx:.1f}" y="{ph - 9}" text-anchor="middle" class="sv-ax">{r["year"]}</text>')
    if safe_draw > 0:
        sy = ypos(-safe_draw)
        out.append(f'<line x1="{left}" x2="{pw - right}" y1="{sy:.1f}" y2="{sy:.1f}" stroke="{GOLD}" stroke-width="2" stroke-dasharray="6 4"><title>A safe endowment draw covers deficits down to {escape(money_full(-safe_draw))}</title></line>')
    label = "Projected surplus or deficit by year, with the safe endowment draw"
    return Markup(f'<svg class="sv-svg" viewBox="0 0 {pw} {ph}" width="100%" role="img" aria-label="{label}">' + "".join(out) + "</svg>")


def projection_legend(safe_draw: float) -> Markup:
    items = (f'<li><svg class="sv-key" width="24" height="14" viewBox="0 0 24 14" aria-hidden="true"><rect x="6" y="2" width="12" height="10" rx="3" fill="{ORANGE}"/></svg>'
             '<span class="sv-lg-name">Deficit: expenses above revenue (bar points down)</span></li>'
             f'<li><svg class="sv-key" width="24" height="14" viewBox="0 0 24 14" aria-hidden="true"><rect x="6" y="2" width="12" height="10" rx="3" fill="{BLUE}"/></svg>'
             '<span class="sv-lg-name">Surplus (bar points up)</span></li>')
    if safe_draw > 0:
        items += (f'<li><svg class="sv-key" width="24" height="14" viewBox="0 0 24 14" aria-hidden="true"><line x1="1" x2="23" y1="7" y2="7" stroke="{GOLD}" stroke-width="2" stroke-dasharray="6 4"/></svg>'
                  f'<span class="sv-lg-name">Safe endowment draw, {escape(money_full(safe_draw))} a year: a deficit below this line is more than it covers</span></li>')
    return Markup(f'<ul class="sv-legend">{items}</ul>')


def projection_table(rows: list[dict]) -> Markup:
    head = "".join(f'<th scope="col" class="num">{h}</th>' for h in ("Revenue", "Priest cost", "Other costs", "Surplus or deficit"))
    body = "".join(
        f'<tr><th scope="row">{r["year"]}</th><td class="num">{escape(money_full(r["revenue"]))}</td><td class="num">{escape(money_full(r["priest"]))}</td>'
        f'<td class="num">{escape(money_full(r["other_exp"]))}</td><td class="num">{escape(money_full(r["net"]))}</td></tr>' for r in rows)
    return Markup(f'<table class="sv-table-el"><thead><tr><th scope="col">Year</th>{head}</tr></thead><tbody>{body}</tbody></table>')
