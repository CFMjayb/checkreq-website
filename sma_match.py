"""
sma_match.py -- 26-129 SMA letters (plan revision 12): matches each row of the Task Force model to a Beacon parish.

The model names a congregation by its Parish UEID (the General Convention parochial-report id, which Beacon keeps in
portal.parishes.parochial_report_id), a short label, the name on the parochial report, and a city. Matching order:
  1. UEID equal to a parish's parochial_report_id.            -> 'ueid'
  2. Else the parochial-report name and the city both match a parish's name or legal/report name and city
     (case, punctuation and filler words ignored), and it is the ONLY such parish.        -> 'name'
  3. Else unmatched. A person matches it by hand on the check sheet ('manual').
One parish is matched to one model row at most: if two rows would claim the same parish, the second stays
unmatched, and both are reported so a human decides.

Pure functions over dicts (no database).
"""
from __future__ import annotations

import re

_FILLER = {"the", "of", "and", "episcopal", "church", "parish", "vestry", "in", "at", "for"}
_SAINT = re.compile(r"\b(st|saint)\b\.?")


def norm_name(s) -> str:
    s = str(s or "").lower().replace("&", " and ").replace("'", "").replace("’", "")
    s = _SAINT.sub("saint", s)
    words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", s).split() if w not in _FILLER]
    return " ".join(words)


def norm_city(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _names(p: dict) -> set[str]:
    return {n for n in (norm_name(p.get(k)) for k in ("name", "pr_name", "legal_name")) if n}


def match_rows(rows: list[dict], parishes: list[dict]) -> list[dict]:
    """-> one dict per row: {"row": <model row>, "parish_id": int|None, "status": "ueid"|"name"|"unmatched",
    "note": str}. `parishes` are dicts with id, name, pr_name, legal_name, city, parochial_report_id."""
    by_ueid: dict[str, dict] = {}
    for p in parishes:
        u = (p.get("parochial_report_id") or "").strip()
        if u:
            by_ueid.setdefault(u, p)
    claimed: dict[int, str] = {}
    out: list[dict] = [{"row": row, "parish_id": None, "status": "unmatched", "note": ""} for row in rows]
    # Pass 1: the UEID is the strongest evidence, so every UEID match is settled BEFORE any name is tried. Otherwise a
    # row with a mistyped UEID but a plausible name could take a parish away from the row that holds its real UEID.
    for m in out:
        ueid = (m["row"].get("ueid") or "").strip()
        if ueid and ueid in by_ueid:
            parish = by_ueid[ueid]
            if parish["id"] in claimed:
                m["note"] = f"Beacon parish already matched to {claimed[parish['id']]}."
            else:
                claimed[parish["id"]] = m["row"].get("parish") or ""
                m["parish_id"], m["status"] = parish["id"], "ueid"
    # Pass 2: name and city, only for rows with no UEID match, and only for parishes nothing has claimed.
    for m in out:
        if m["status"] != "unmatched" or m["note"]:
            continue
        row = m["row"]
        want = {norm_name(row.get("pr_name")), norm_name(row.get("parish"))} - {""}
        city = norm_city(row.get("city"))
        hits = [p for p in parishes if want & _names(p) and city and norm_city(p.get("city")) == city]
        if len(hits) == 1:
            parish = hits[0]
            if parish["id"] in claimed:
                m["note"] = f"Beacon parish already matched to {claimed[parish['id']]}."
            else:
                claimed[parish["id"]] = row.get("parish") or ""
                m["parish_id"], m["status"] = parish["id"], "name"
        elif len(hits) > 1:
            m["note"] = "More than one Beacon parish has that name and city."
    return out


def unmatched_active_parishes(matches: list[dict], parishes: list[dict]) -> list[dict]:
    """Active Beacon parishes that no model row matched (new congregations the model did not list)."""
    matched = {m["parish_id"] for m in matches if m["parish_id"]}
    return [p for p in parishes if p.get("is_active") and p["id"] not in matched]
