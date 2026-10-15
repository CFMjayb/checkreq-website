"""
neighborhood_pews.py -- the "In the pews and in the neighborhood" panel on the Neighborhood View (Story tab)
(26-XXX Congregational Vitality plan, mockup B; the rules are in 26-XXX Neighborhood Insights\\cowork_handoff\\returns\\pews_vs_neighborhood.md).

Pure functions: numbers in, a small dict of display strings out. No database, no web code.

Three pairs side by side: age, race, and people. Two different sources, so there is never a "difference" number between them:
  * Age:    the parish's reported average age against the neighborhood's AVERAGE age (estimated from the Census age groups, with the
            median as a note). Average against average, never against a median.
  * White:  the share the parish reported white against White, not Hispanic, in the Census.
  * People: average Sunday attendance (members in small type) against the people living within the drive. No ratio: members often
            live outside the area, so attendance as a share of residents says little and can sound like a grade.
No giving figure appears here, ever. The parish's age and race fields exist from the 2024 form on and are blank when the section was not
filled in (logic.sustain.harmonize); a blank shows "Not reported", never a zero.
"""
from __future__ import annotations

import re

import neighborhood_insights as ins

_BAND = re.compile(r"(\d+)(?:-(\d+)|\+)")
OPEN_ENDED_MIDPOINT = 90.0   # "85 and over" counted as 90


def neighborhood_average_age(age_rows) -> float | None:
    """The mean age of everyone in the area, from rows like {"component": "Male 25-29", "value": 1234}. Each band counts at its middle
    (0-4 is 2.5; 85+ is 90). None when there are no usable rows."""
    people = weighted = 0.0
    for r in age_rows or ():
        m = _BAND.search(r.get("component") or "")
        v = r.get("value")
        if not m or v is None:
            continue
        mid = (int(m.group(1)) + int(m.group(2)) + 1) / 2 if m.group(2) else OPEN_ENDED_MIDPOINT
        people += float(v)
        weighted += float(v) * mid
    return weighted / people if people else None


def neighborhood_white_share(race_rows) -> float | None:
    """White (not Hispanic) as a percent of everyone counted in the race and ethnicity groups."""
    total = white = 0.0
    for r in race_rows or ():
        v = r.get("value")
        if v is None:
            continue
        total += float(v)
        if r.get("component") == "White":
            white = float(v)
    return white / total * 100 if total else None


def _first(rows, component=""):
    for r in rows or ():
        if (r.get("component") or "") == component and r.get("value") is not None:
            return float(r["value"])
    return None


def build(parochial: dict | None, values: dict, minutes: int) -> dict:
    """The panel. `parochial` is the parish's latest harmonized filing row (sustain.parish_year_v, via sustain_data) or None;
    `values` is the area's profile values by metric key ({"age_by_sex": [rows], "race_ethnicity": [...], "median_age": [...], ...}).

    {"has_report": bool, "year", "minutes", "rows": [{"label", "pews": {"value", "note"}, "nbhd": {"value", "note"}}, ...],
     "reference": one line of neighborhood numbers for a parish with no report, "prompt": the closing question}"""
    nb_age = neighborhood_average_age(values.get("age_by_sex"))
    median = _first(values.get("median_age"))
    nb_white = neighborhood_white_share(values.get("race_ethnicity"))
    people = _first(values.get("total_population"))

    nb_age_text = f"about {nb_age:.0f}" if nb_age is not None else "n/a"
    nb_age_note = (f"estimated from age groups; half are under {median:.0f}" if median is not None else "estimated from age groups")
    nb_white_text = f"{nb_white:.0f}%" if nb_white is not None else "n/a"
    nb_people_text = ins.fmt_value("count", people) if people is not None else "n/a"

    reference = []
    if nb_age is not None:
        reference.append(f"average age about {nb_age:.0f}" + (f" (half are under {median:.0f})" if median is not None else ""))
    if nb_white is not None:
        reference.append(f"about {nb_white:.0f}% White, not Hispanic")
    out = {
        "minutes": minutes, "has_report": parochial is not None, "year": parochial.get("year") if parochial else None,
        "reference": "; ".join(reference), "rows": [],
        "caption_pews": "In the pews", "caption_nbhd": "In the neighborhood",
        "prompt": "Worth asking: who in the neighborhood is not yet in the pews, and what would help them feel at home?",
        "caution": ("These are different measures from different sources. The parish numbers are the parish's own count or estimate. The neighborhood numbers "
                    "are Census estimates for everyone living nearby. Members may live outside this area, and the race questions are worded differently."),
    }
    if parochial is None:
        return out

    raw = parochial.get("raw") or {}
    age, white = raw.get("avg_age"), raw.get("race_ethnicity_white_percent")
    attendance, members = raw.get("asa_in_person"), raw.get("members")
    people_note = "average Sunday attendance" + (f"; {round(float(members)):,} members" if members else "")
    out["rows"] = [
        {"label": "Average age",
         "pews": {"value": f"{float(age):.0f}" if age is not None else "Not reported", "note": "as reported by the parish" if age is not None else "the parish left this blank"},
         "nbhd": {"value": nb_age_text, "note": nb_age_note}},
        {"label": "White",
         "pews": {"value": f"{float(white):.0f}%" if white is not None else "Not reported", "note": "share reported white" if white is not None else "the parish left this blank"},
         "nbhd": {"value": nb_white_text, "note": "White, not Hispanic"}},
        {"label": "People",
         "pews": {"value": f"{round(float(attendance)):,}" if attendance is not None else "Not reported", "note": people_note if attendance is not None else "no attendance on the report"},
         "nbhd": {"value": nb_people_text, "note": "people living within the drive"}},
    ]
    return out
