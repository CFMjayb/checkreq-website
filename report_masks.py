"""
report_masks.py -- 26-149 Phase 3 (2026-09-23): account-mask logic for the
Report Template editor (report_template_editor.py).

Three jobs, all pure functions over a chart-of-accounts list (no DB, no HTTP):
  * validate_mask()   -- is a line's mask well-formed before it is saved
  * preview_lines()   -- which accounts each line matches, exactly the way the
                         real report engine will assign them
  * starter_lines()   -- draft lines from the entity's top-level accounts
                         ("Start from chart of accounts")

The matching rules are a deliberate, line-for-line PORT of qbo-mcp-server's
reports/mask_utils.py + the line-assignment loop in reports/soa_bva.py
(different deployment, so it can't be imported). If either of those changes,
change this file the same way, or the preview will stop telling the truth:
  - glob masks via fnmatch, with '#' meaning any single character
  - ranges "3100-3199" / "31##-3199" compared numerically ('#' = 0 low, 9 high)
  - a line may hold several comma-separated masks
  - an account is counted once per line; if two DIFFERENT lines match it, the
    first line (in sort order) gets it and the overlap is warned about
  - every account with a number is considered, active or inactive, of any
    classification -- the engine does not filter by Revenue/Expense when
    assigning lines, so the preview flags a mismatch instead of hiding it
"""
from __future__ import annotations

import fnmatch
import re

_RANGE_PART = re.compile(r"^[\d.#]+$")
_GLOB_OK = re.compile(r"^[\d.#*?]+$")


# ── mask_utils.py port ────────────────────────────────────────────────────────

def _acct_num_value(acct: str):
    m = re.match(r"^[\d.]+", acct.strip())
    if m:
        try:
            return float(m.group())
        except ValueError:
            pass
    return None


def _is_range_mask(mask: str) -> bool:
    if "-" not in mask:
        return False
    parts = mask.split("-", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False
    return bool(_RANGE_PART.match(parts[0])) and bool(_RANGE_PART.match(parts[1]))


def _match_range(acct_num: str, mask: str) -> bool:
    start_pat, end_pat = mask.split("-", 1)
    start_str = start_pat.replace("#", "0")
    end_str = end_pat.replace("#", "9")
    acct_val = _acct_num_value(acct_num)
    start_val = _acct_num_value(start_str)
    end_val = _acct_num_value(end_str)
    if acct_val is not None and start_val is not None and end_val is not None:
        return start_val <= acct_val <= end_val
    return start_str <= acct_num.strip() <= end_str


def mask_matches(acct_num: str, mask: str) -> bool:
    if _is_range_mask(mask):
        return _match_range(acct_num, mask)
    return fnmatch.fnmatch(acct_num, mask.replace("#", "?"))


def split_masks(account_mask: str) -> list[str]:
    return [p.strip() for p in str(account_mask or "").split(",") if p.strip()]


# ── validation ────────────────────────────────────────────────────────────────

def validate_mask(account_mask: str) -> str | None:
    """None when every comma-separated part is a real glob or range mask,
    otherwise a plain-English reason. Stricter than the engine (which would
    just match nothing): a typo like '66 7*' or '6670..6679' is caught here
    instead of silently producing an empty report line."""
    parts = split_masks(account_mask)
    if not parts:
        return "An account mask is required."
    for p in parts:
        if _is_range_mask(p):
            lo = _acct_num_value(p.split("-", 1)[0].replace("#", "0"))
            hi = _acct_num_value(p.split("-", 1)[1].replace("#", "9"))
            if lo is not None and hi is not None and lo > hi:
                return f"'{p}': the range starts after it ends."
            continue
        if not _GLOB_OK.match(p):
            return (f"'{p}' isn't a valid mask. Use account numbers with * or # "
                    "(e.g. 667*, 4###) or a range (e.g. 4000-4099), separated by commas.")
    return None


# ── preview ───────────────────────────────────────────────────────────────────

def preview_lines(accounts: list[dict], lines: list[dict], full_entity: bool = False) -> dict:
    """lines: [{key, section, line_label, account_mask}] in report (sort) order --
    `key` is whatever the caller uses to tie a result back to its grid row.

    Returns {lines: {key: {matches: [...], count, section_mismatch}},
             overlaps: [{acct_num, name, lines: [labels]}],
             unmapped: [...] (full-entity templates only)}.
    Mirrors soa_bva.py: only ACTIVE lines should be passed in."""
    per_line: dict = {}
    for ln in lines:
        per_line[ln["key"]] = {"matches": [], "count": 0, "section_mismatch": 0}
    assigned: set[str] = set()
    overlaps = []
    for a in accounts:
        num = (a.get("acct_num") or "").strip()
        if not num:
            continue
        hits = [ln for ln in lines
                if any(mask_matches(num, m) for m in split_masks(ln["account_mask"]))]
        if not hits:
            continue
        first = hits[0]
        row = {
            "acct_num": num, "name": a.get("name", ""),
            "classification": a.get("classification", ""),
            "active": bool(a.get("active", True)),
            "parent_acct_num": a.get("parent_acct_num", ""),
            "wrong_section": a.get("classification") != first["section"],
        }
        bucket = per_line[first["key"]]
        bucket["matches"].append(row)
        bucket["count"] += 1
        if row["wrong_section"]:
            bucket["section_mismatch"] += 1
        assigned.add(num)
        if len(hits) > 1:
            overlaps.append({"acct_num": num, "name": a.get("name", ""),
                             "lines": [h["line_label"] for h in hits]})
    unmapped = []
    if full_entity:
        unmapped = [{"acct_num": (a.get("acct_num") or "").strip(), "name": a.get("name", ""),
                     "classification": a.get("classification", ""),
                     "active": bool(a.get("active", True))}
                    for a in accounts
                    if a.get("classification") in ("Revenue", "Expense")
                    and (a.get("acct_num") or "").strip() not in assigned]
    return {"lines": per_line, "overlaps": overlaps, "unmapped": unmapped}


# ── starter lines ─────────────────────────────────────────────────────────────

def compress_to_masks(wanted: set[str], all_nums: set[str]) -> str:
    """The shortest comma-separated mask list matching EXACTLY `wanted` out of
    the whole chart (`all_nums`, active and inactive -- the engine sees both).
    For each wanted number, use the shortest prefix-glob 'p*' that matches no
    account outside `wanted`; fall back to the bare number. So EDOM's Diocesan
    House subtree becomes '667*' rather than 25 listed numbers, and a glob can
    never quietly pull in someone else's account."""
    picks: set[str] = set()
    for n in sorted(wanted):
        chosen = n
        for k in range(1, len(n) + 1):
            p = n[:k]
            hits = {x for x in all_nums if x.startswith(p)}
            if hits <= wanted:
                chosen = p + "*" if hits != {n} else n
                break
        picks.add(chosen)
    # Drop anything already covered by a broader glob in the set.
    globs = [p[:-1] for p in picks if p.endswith("*")]
    keep = [p for p in picks
            if not any((p[:-1] if p.endswith("*") else p).startswith(g) and p != g + "*" for g in globs)]
    return ", ".join(sorted(keep))


def starter_lines(accounts: list[dict], sections: tuple[str, ...] = ("Revenue", "Expense"),
                  prefix: str = "", existing_masks: list[str] | None = None,
                  include_inactive: bool = False) -> list[dict]:
    """One suggested line per top-level account in the chosen section(s),
    optionally limited to account numbers starting with `prefix` (top-level
    meaning the highest account inside that scope -- see in_scope below).

    The mask covers the account and its whole sub-account tree. Usually that
    is just '6670*'; when QBO has a sub-account whose number doesn't start with
    its parent's (real EDOM case: 6671.20-6671.80 sit under 6670), those numbers
    are listed explicitly after it so nothing is silently left out.
    Accounts already matched by one of `existing_masks` are skipped."""
    existing = [m for em in (existing_masks or []) for m in split_masks(em)]
    by_id = {a["id"]: a for a in accounts}
    all_nums = {(a.get("acct_num") or "").strip() for a in accounts} - {""}
    children: dict[str, list[dict]] = {}
    for a in accounts:
        if a.get("parent_id"):
            children.setdefault(a["parent_id"], []).append(a)

    def descendants(aid: str) -> list[dict]:
        out, stack = [], list(children.get(aid, []))
        while stack:
            d = stack.pop()
            out.append(d)
            stack.extend(children.get(d["id"], []))
        return out

    def in_scope(acct: dict | None) -> bool:
        n = ((acct or {}).get("acct_num") or "").strip()
        return bool(acct) and bool(n) and n.startswith(prefix) and acct.get("classification") in sections

    out = []
    for a in accounts:
        num = (a.get("acct_num") or "").strip()
        if not in_scope(a):
            continue
        # "Top-level" = the highest account WITHIN the chosen scope: its parent
        # is missing or falls outside the prefix/section. With no prefix that is
        # a true QBO root; with prefix "667" it is 6670, 6671, ... even though
        # QBO nests them under a parent numbered outside 667.
        if in_scope(by_id.get(a.get("parent_id"))):
            continue
        if not include_inactive and not a.get("active", True):
            continue
        if any(mask_matches(num, m) for m in existing):
            continue
        desc = [d for d in descendants(a["id"]) if (d.get("acct_num") or "").strip()]
        # Inactive sub-accounts stay in: the engine reports them too (their
        # history still belongs to this line). include_inactive only decides
        # whether an inactive TOP account gets a line of its own.
        mask = compress_to_masks({num} | {d["acct_num"].strip() for d in desc}, all_nums)
        out.append({"section": a["classification"], "line_label": a.get("name", num),
                    "account_mask": mask, "acct_num": num, "sub_accounts": len(desc)})
    out.sort(key=lambda s: (0 if s["section"] == "Revenue" else 1, s["acct_num"]))
    for i, s in enumerate(out, start=1):
        s["sort_order"] = i * 10
    return out
