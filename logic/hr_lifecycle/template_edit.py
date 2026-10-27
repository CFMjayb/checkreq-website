"""
template_edit.py -- editing a checklist from the screen (26-162, 2026-10-25; Jay: "I don't see a place where I can actually build
the flow of onboarding and offboarding. And we're going to need that.").

The checklists already live in hr.templates / hr.template_items and are VERSIONED (templates.py): publishing a changed
checklist makes the next version, the old version stays, and a case keeps the snapshot of the items it opened with. This module
is the screen's side of that: it checks a whole edited checklist, and publishes it through templates.publish(), the same
function the CSV seed uses, so a save that changes nothing makes no new version.

Nothing here touches an open case, and no item name is special to the code (checked 2026-10-25: no logic refers to an item_key),
so any item can be edited, moved or removed. Hours, wording and dates only: never pay.
"""
from __future__ import annotations

import re

import db

from . import assignees, conditions, templates
from .errors import HrError

MAX_ITEMS = 150
MAX_LABEL = 200
MAX_HELP = 1000
MAX_NAME = 120
MAX_DAYS = 730
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

BENEFITS_KINDS = ("benefits_enroll", "benefits_end")
# Who may change a checklist (Jay's recommendation accepted by default, 2026-10-25): the HR roles for the staff checklists,
# the benefits roles for the parish benefits checklists. Beacon Admin may change any of them.
EDIT_ROLES_STAFF = ["hr_admin", "beacon_admin"]
EDIT_ROLES_BENEFITS = ["benefits_admin", "beacon_admin"]

# the "Applies when" choices the screen offers; anything else is typed by hand and checked by conditions.validate()
CONDITION_PRESETS = [
    ("", "Everyone"),
    ("clergy", "Clergy only"),
    ("lay", "Lay staff only"),
    ("health_eligible", "Health-eligible people"),
    ("pension_eligible", "Pension-eligible people"),
    ("housing_provided", "People given housing"),
]
BASIS_LABELS = {"start_date": "hire date", "separation_date": "last day", "benefits_effective": "benefits start",
                "opened": "the day the case opens"}


def edit_roles(case_kind: str) -> list[str]:
    return EDIT_ROLES_BENEFITS if case_kind in BENEFITS_KINDS else EDIT_ROLES_STAFF


# ------------------------------------------------------------------ reading

def doc_types() -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT key, label, sensitivity FROM hr.doc_types WHERE is_active ORDER BY sort_order, label")]


def history(key: str) -> list[dict]:
    """Every version of a checklist, newest first: who published it and when."""
    return [dict(r) for r in db.query(
        "SELECT t.id, t.version, t.is_active, t.published_at, coalesce(u.display_name, u.email) AS published_by, "
        "  (SELECT count(*) FROM hr.template_items i WHERE i.template_id = t.id) AS item_count "
        "FROM hr.templates t LEFT JOIN checkreq.app_users u ON u.id = t.published_by_user_id "
        "WHERE t.key = %s ORDER BY t.version DESC", (key,))]


def active_id(key: str) -> int | None:
    row = db.query_one("SELECT id FROM hr.templates WHERE key = %s AND is_active", (key,))
    return row["id"] if row else None


def editor_items(template_id: int) -> list[dict]:
    """The items as the screen edits them: exactly the fields an item has, nothing else."""
    out = []
    for it in templates.items_for(template_id):
        out.append({
            "item_key": it["item_key"], "label": it["label"], "help_text": it["help_text"] or "",
            "section": it["section"], "owner_group": it["owner_group"], "due_basis": it["due_basis"],
            "due_offset_days": it["due_offset_days"], "business_days": bool(it["business_days"]),
            "hard_deadline_days": it["hard_deadline_days"], "required": bool(it["required"]),
            "doc_type_key": it["doc_type_key"] or "", "condition": it["condition"] or "",
            "condition_label": conditions.describe(it["condition"]),
            "assignee_user_id": it.get("assignee_user_id"),
        })
    return out


def assignee_names(user_ids) -> dict[int, str]:
    """{user id: name} for the people named as a step's default assignee (so the editor can show a person who is not in this
    entity's pick list)."""
    ids = sorted({int(u) for u in user_ids if u})
    if not ids:
        return {}
    return {r["id"]: r["name"] for r in db.query(
        "SELECT id, coalesce(nullif(display_name, ''), email) AS name FROM checkreq.app_users WHERE id = ANY(%s)", (ids,))}


# ------------------------------------------------------------------ checking

def slug_key(label: str, taken: set[str]) -> str:
    """A short, unique item_key made from the wording of a new item ('Signed offer letter' -> 'signed_offer_letter')."""
    base = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")[:34] or "item"
    if not base[0].isalpha():
        base = "item_" + base
    key, n = base[:40], 2
    while key in taken:
        suffix = f"_{n}"
        key = base[: 40 - len(suffix)] + suffix
        n += 1
    return key


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(v)


def _whole(v, where: str, what: str, lo: int, hi: int, problems: list[str], blank=None):
    if v is None or (isinstance(v, str) and not v.strip()):
        return blank
    if isinstance(v, bool) or isinstance(v, float) and not float(v).is_integer():
        problems.append(f"{where}: {what} must be a whole number.")
        return blank
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        problems.append(f"{where}: {what} must be a whole number.")
        return blank
    if not lo <= n <= hi:
        problems.append(f"{where}: {what} must be between {lo} and {hi}.")
        return blank
    return n


def normalize(raw_items, existing_keys: set[str]) -> tuple[list[dict], list[str]]:
    """Turn what the screen posted into items ready for templates.publish(), and list EVERY problem found (not just the first),
    each naming the item. existing_keys = the item_keys of the version being edited (a new item gets a fresh key)."""
    problems: list[str] = []
    if not isinstance(raw_items, list):
        return [], ["The checklist items were not sent in a form I can read."]
    if not raw_items:
        return [], ["A checklist needs at least one item."]
    if len(raw_items) > MAX_ITEMS:
        return [], [f"A checklist can have at most {MAX_ITEMS} items."]
    known_docs = {r["key"] for r in db.query("SELECT key FROM hr.doc_types")}
    posted_people = [r.get("assignee_user_id") for r in raw_items if isinstance(r, dict)]
    known_people = {r["id"] for r in db.query(
        "SELECT id FROM checkreq.app_users WHERE is_active AND id = ANY(%s)",
        ([int(u) for u in posted_people if str(u or "").strip().isdigit()],))}
    taken = {str(r.get("item_key") or "").strip() for r in raw_items if isinstance(r, dict) and str(r.get("item_key") or "").strip()}
    taken |= set()
    items: list[dict] = []
    seen: set[str] = set()
    for n, r in enumerate(raw_items, start=1):
        if not isinstance(r, dict):
            problems.append(f"Item {n}: not readable.")
            continue
        label = str(r.get("label") or "").strip()
        where = f"Item {n}" + (f" ({label[:40]})" if label else "")
        if not label:
            problems.append(f"{where}: the wording is required.")
        elif len(label) > MAX_LABEL:
            problems.append(f"{where}: the wording is longer than {MAX_LABEL} characters.")
        help_text = str(r.get("help_text") or "").strip() or None
        if help_text and len(help_text) > MAX_HELP:
            problems.append(f"{where}: the help text is longer than {MAX_HELP} characters.")
        section = str(r.get("section") or "").strip()
        if section not in templates.SECTIONS:
            problems.append(f"{where}: choose a section.")
        owner = str(r.get("owner_group") or "").strip()
        if owner not in templates.OWNERS:
            problems.append(f"{where}: choose who owns the item.")
        basis = str(r.get("due_basis") or "").strip()
        if basis not in templates.BASES:
            problems.append(f"{where}: choose what the due date counts from.")
        offset = _whole(r.get("due_offset_days"), where, "the due days", -MAX_DAYS, MAX_DAYS, problems, blank=0)
        hard = _whole(r.get("hard_deadline_days"), where, "the hard deadline", 0, MAX_DAYS, problems, blank=None)
        doc = str(r.get("doc_type_key") or "").strip() or None
        if doc and doc not in known_docs:
            problems.append(f"{where}: that document type does not exist.")
        cond = " ".join(str(r.get("condition") or "").split()) or None
        try:
            conditions.validate(cond)
        except conditions.ConditionError as e:
            problems.append(f"{where}: {e}")
        who_raw = r.get("assignee_user_id")
        who = None
        if str(who_raw if who_raw is not None else "").strip():
            if not str(who_raw).strip().isdigit() or int(str(who_raw).strip()) not in known_people:
                problems.append(f"{where}: the person assigned is not an active Beacon user.")
            else:
                who = int(str(who_raw).strip())
        key = str(r.get("item_key") or "").strip()
        if key:
            if not KEY_RE.match(key):
                problems.append(f"{where}: the item's internal name '{key}' is not valid.")
        else:
            key = slug_key(label, taken | seen)
        if key in seen:
            problems.append(f"{where}: another item already has the internal name '{key}'.")
        seen.add(key)
        taken.add(key)
        items.append({
            "item_key": key, "section": section, "label": label, "help_text": help_text, "owner_group": owner,
            "due_basis": basis, "due_offset_days": offset or 0, "business_days": _truthy(r.get("business_days")),
            "hard_deadline_days": hard, "required": _truthy(r.get("required")), "doc_type_key": doc, "condition": cond,
            "sort_order": n * 10, "assignee_user_id": who,
        })
    return items, problems


# ------------------------------------------------------------------ publishing

def _assignee_scope_problems(template_id: int, items: list[dict], org_id: int) -> list[str]:
    """A default person may only be NEWLY chosen from this entity's pick list. A person already saved on the item (perhaps chosen in
    another entity that shares this checklist) is left alone."""
    saved = {i["item_key"]: i.get("assignee_user_id") for i in templates.items_for(template_id)}
    people = None
    out = []
    for n, it in enumerate(items, start=1):
        uid = it.get("assignee_user_id")
        if not uid or saved.get(it["item_key"]) == uid:
            continue
        if people is None:
            people = {p["id"] for p in assignees.people_for(org_id)}
        if uid not in people:
            out.append(f"Item {n} ({it['label'][:40]}): that person cannot be assigned at this entity.")
    return out


def publish_edit(template_id: int, base_version: int, name: str, raw_items, actor_id: int, org_id: int | None = None) -> dict:
    """Publish an edited checklist as the next version (or report 'unchanged'). Raises HrError('invalid', every problem) for bad
    input and HrError('refused') when somebody else published a newer version since the screen opened. One transaction; the active
    row is locked while the new version is written."""
    t = db.query_one("SELECT * FROM hr.templates WHERE id = %s", (template_id,))
    if not t:
        raise HrError("not_found", "That checklist was not found.")
    if not t["is_active"]:
        raise HrError("refused", "That is an older version of the checklist. Open the current version to change it.")
    name = " ".join((name or "").split())
    problems: list[str] = []
    if not name:
        problems.append("The checklist needs a name.")
    elif len(name) > MAX_NAME:
        problems.append(f"The checklist name is longer than {MAX_NAME} characters.")
    existing = {i["item_key"] for i in templates.items_for(template_id)}
    items, more = normalize(raw_items, existing)
    problems += more
    if org_id and not more:
        problems += _assignee_scope_problems(template_id, items, org_id)
    if problems:
        raise HrError("invalid", "\n".join(problems))
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, version FROM hr.templates WHERE key = %s AND is_active FOR UPDATE", (t["key"],))
            cur_row = cur.fetchone()
            if not cur_row or cur_row["id"] != template_id or cur_row["version"] != int(base_version or 0):
                raise HrError("refused", "Somebody else published a newer version of this checklist while you were editing. "
                                         "Open it again to see their version; your changes were not saved.")
            spec = {"case_kind": t["case_kind"], "applies_to": list(t["applies_to"]), "label": name, "items": items}
            status, version = templates.publish(cur, t["key"], spec, actor_id)
            cur.execute("SELECT id FROM hr.templates WHERE key = %s AND version = %s", (t["key"], version))
            new_id = cur.fetchone()["id"]
    return {"status": status, "version": version, "template_id": new_id, "key": t["key"]}
