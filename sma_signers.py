"""
sma_signers.py -- 26-129 SMA letters (plan revision 12): who signs a parish's allocation letter.

THE RULE (Jay, 2026-10-08): every parish needs at least one signer, and at least one signer must be clergy or the
Senior Warden. A Treasurer may sign too but can never be the only signer. "Clergy" means any clergy person (a
deacon counts); the Rector, Vicar or Priest-in-Charge is only who is pre-filled on the Rector/Vicar signature line.

WHERE THEY COME FROM: Beacon's own parish roles (parish_clergy, parish_srwarden, parish_treasurer, live grants to an
active login with an email). The clergy role holds every clergy member (a parish often has several), so the title in
the clergy directory (portal.congregation_cache, matched to the login by email, else by name) decides who is the
Rector/Vicar-line signer. RANK below orders the titles, lowest first. A titles file from the Compensation & Benefits
TAC model, offered by Jay, can refine it: edit RANK_BY_ROLE.

A signer record (stored in portal.sma_letters.signers, a JSON list):
    {"role": "clergy" | "srwarden" | "treasurer", "name": "...", "email": "...", "user_id": 12 or null,
     "title": "Rector" or "", "source": "beacon" | "manual", "chosen": true | false}
chosen=false are alternates a person can swap in. The numbers signer1..signer3 are given consecutively, in role
order (clergy, Senior Warden, Treasurer), among the CHOSEN signers only, when a signing form is built.

select_signers() is a pure function (no database) so the rule is easy to test; load_candidates() reads Beacon.
"""
from __future__ import annotations

import re

ROLE_ORDER = ("clergy", "srwarden", "treasurer")
ROLE_LABEL = {"clergy": "Rector/Vicar", "srwarden": "Senior Warden", "treasurer": "Treasurer"}
ROLE_KEY = {"parish_clergy": "clergy", "parish_srwarden": "srwarden", "parish_treasurer": "treasurer"}
ELIGIBLE_ROLES = ("clergy", "srwarden")          # at least one chosen signer must hold one of these

# Lower is better. Anything not listed ranks 5.
RANK_BY_ROLE = {
    "rector": 0, "vicar": 0, "priest in charge": 0, "priest-in-charge": 0, "co-vicar": 0, "co-rector": 0,
    "bridge pastor": 0, "pastor and mission developer": 0, "missioner": 0,
    "interim rector": 1, "interim vicar": 1, "interim priest in charge": 1, "long-term supply": 1,
    "associate rector": 2, "associate": 2, "assistant": 2, "assisting clergy": 2, "curate": 2, "dean": 2,
    "deacon": 3, "deacon associate": 3,
}
DEFAULT_RANK = 5
RANK_BY_ROLE.update({"interim missioner": 1, "priest associate": 2, "associate priest": 2, "interim associate": 2})
_RANKS = {}                                       # filled below, once _title_key exists


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _title_key(s) -> str:
    """A directory title reduced to what ranks it: lower case, hyphens as spaces, a trailing '(part-time)' dropped."""
    t = re.sub(r"\([^)]*\)", " ", str(s or "").lower()).replace("-", " ").replace("/", " ")
    return re.sub(r"\s+", " ", t).strip()


def rank_of(role_label: str) -> int:
    return _RANKS.get(_title_key(role_label), DEFAULT_RANK)


def _full_name(u: dict) -> str:
    first, last = (u.get("first_name") or "").strip(), (u.get("last_name") or "").strip()
    if first or last:
        return f"{first} {last}".strip()
    return (u.get("display_name") or u.get("email") or "").strip()


_RANKS.update({_title_key(k): v for k, v in RANK_BY_ROLE.items()})


def _addresses(value) -> set[str]:
    """The directory sometimes holds several addresses in one field ('a@x.org;b@y.org')."""
    return {a for a in (_norm(p) for p in re.split(r"[;,\s]+", str(value or ""))) if a}


def _title_for(user: dict, cache_rows: list[dict]) -> str:
    """The clergy directory role (Rector, Deacon, ...) for this login: by email when the directory row has one,
    else by first and last name. '' when the directory has no matching row."""
    email = _norm(user.get("email"))
    first, last = _norm(user.get("first_name")), _norm(user.get("last_name"))
    for row in cache_rows:
        if email and email in _addresses(row.get("email")):
            return (row.get("role") or "").strip()
    if first and last:
        for row in cache_rows:
            if _norm(row.get("first_name")) == first and _norm(row.get("last_name")) == last:
                return (row.get("role") or "").strip()
    return ""


def select_signers(role_rows: list[dict], cache_rows: list[dict]) -> list[dict]:
    """The parish's signer records, chosen and alternate.

    role_rows: one dict per live parish-role grant to an active login with an email, with role_key (parish_clergy,
        parish_srwarden or parish_treasurer), user_id, email, display_name, first_name, last_name, and granted_at
        (used only to break ties, earliest first).
    cache_rows: the parish's clergy directory rows (role, email, first_name, last_name).

    One signer is chosen per role: the clergy person with the best title rank, else the earliest grant."""
    by_role: dict[str, list[dict]] = {r: [] for r in ROLE_ORDER}
    seen: set[tuple[str, str]] = set()
    for g in role_rows:
        role = ROLE_KEY.get(g.get("role_key"))
        email = _norm(g.get("email"))
        if not role or not email or (role, email) in seen:
            continue
        seen.add((role, email))
        by_role[role].append({
            "role": role, "name": _full_name(g), "email": (g.get("email") or "").strip(), "user_id": g.get("user_id"),
            "title": _title_for(g, cache_rows) if role == "clergy" else "", "source": "beacon", "chosen": False,
            "_granted": str(g.get("granted_at") or ""),
        })
    out: list[dict] = []
    for role in ROLE_ORDER:
        cands = by_role[role]
        if role == "clergy":
            cands.sort(key=lambda c: (rank_of(c["title"]), c["_granted"], c["email"].lower()))
        else:
            cands.sort(key=lambda c: (c["_granted"], c["email"].lower()))
        for i, c in enumerate(cands):
            c["chosen"] = i == 0
            c.pop("_granted", None)
            out.append(c)
    return out


def chosen(signers: list[dict]) -> list[dict]:
    return [s for s in signers if s.get("chosen")]


def numbered(signers: list[dict]) -> list[dict]:
    """The chosen signers with signer_no 1..n, consecutive, in role order (clergy, Senior Warden, Treasurer)."""
    picked = []
    for role in ROLE_ORDER:
        picked += [s for s in chosen(signers) if s.get("role") == role][:1]
    return [dict(s, signer_no=i) for i, s in enumerate(picked, 1)]


def has_eligible_signer(signers: list[dict]) -> bool:
    """The rule: at least one chosen signer is clergy or the Senior Warden, and has an email."""
    return any(s.get("role") in ELIGIBLE_ROLES and (s.get("email") or "").strip() for s in chosen(signers))


def signature_tags(signers: list[dict]) -> dict[str, str]:
    """The signing form's merge fields for the signature tags and the hidden signer fields (plan section 4).
    A role with no chosen signer gets an empty tag, so its printed line stays blank for a paper signature."""
    nums = {s["role"]: s for s in numbered(signers)}
    tags = {}
    for role, field in (("clergy", "ClergyTag"), ("srwarden", "SrWardenTag"), ("treasurer", "TreasurerTag")):
        tags[field] = f"[sig|req|signer{nums[role]['signer_no']}]" if role in nums else ""
    first = min((s["signer_no"] for s in nums.values()), default=None)
    tags["DateTag"] = f"[date|req|signer{first}]" if first else ""
    by_no = {s["signer_no"]: s for s in nums.values()}
    for n in (1, 2, 3):
        tags[f"Signer{n}Name"] = by_no[n]["name"] if n in by_no else ""
        tags[f"Signer{n}Email"] = by_no[n]["email"] if n in by_no else ""
    return tags


def validate_signers(signers: list[dict]) -> list[str]:
    """Reasons these signers cannot be used, in words for the check sheet. Empty list = fine."""
    problems = []
    picked = chosen(signers)
    if not picked:
        problems.append("No signer is chosen.")
    elif not has_eligible_signer(signers):
        problems.append("At least one signer must be clergy or the Senior Warden, with an email address.")
    for s in picked:
        if not (s.get("email") or "").strip():
            problems.append(f"{ROLE_LABEL.get(s.get('role'), 'A signer')} has no email address.")
        elif not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", s["email"].strip()):
            problems.append(f"{ROLE_LABEL.get(s.get('role'), 'A signer')} has an invalid email address.")
    roles = [s.get("role") for s in picked]
    for role in ROLE_ORDER:
        if roles.count(role) > 1:
            problems.append(f"More than one {ROLE_LABEL[role]} is chosen. Choose one.")
    return problems


def describe(signers: list[dict], *, emails: bool = False) -> str:
    """A short line for notes: 'Rector/Vicar Jane Doe, Treasurer Sam Lee'. With emails=True each name is followed by
    the address the signing request goes to, so correcting only an address still shows up in the notes."""
    parts = [f"{ROLE_LABEL.get(s['role'], s['role'])} {s.get('name') or s.get('email')}"
             + (f" <{s.get('email')}>" if emails and s.get("email") else "") for s in numbered(signers)]
    return ", ".join(parts) or "no signer"


# ---------------------------------------------------------------------------------------------
# Beacon lookups
# ---------------------------------------------------------------------------------------------
def load_candidates(parish_ids: list[int]) -> dict[int, list[dict]]:
    """parish_id -> signer records (chosen and alternates) for each parish, in two queries."""
    import db   # imported here so the pure functions above never touch the database layer
    ids = [int(i) for i in parish_ids if i]
    if not ids:
        return {}
    roles = db.query(
        "SELECT pur.parish_id, pur.role_key, pur.granted_at, u.id AS user_id, u.email, u.display_name, "
        "       u.first_name, u.last_name "
        "FROM portal.parish_user_roles pur JOIN checkreq.app_users u ON u.id = pur.user_id "
        "WHERE pur.parish_id = ANY(%s) AND pur.revoked_at IS NULL AND u.is_active "
        "  AND COALESCE(u.email, '') <> '' AND pur.role_key = ANY(%s) "
        "ORDER BY pur.parish_id, pur.granted_at, u.id", (ids, list(ROLE_KEY)))
    cache = db.query(
        "SELECT parish_id, role, email, first_name, last_name FROM portal.congregation_cache "
        "WHERE parish_id = ANY(%s) AND role_category = 'clergy'", (ids,))
    r_by: dict[int, list[dict]] = {}
    c_by: dict[int, list[dict]] = {}
    for r in roles:
        r_by.setdefault(r["parish_id"], []).append(r)
    for c in cache:
        c_by.setdefault(c["parish_id"], []).append(c)
    return {pid: select_signers(r_by.get(pid, []), c_by.get(pid, [])) for pid in ids}
