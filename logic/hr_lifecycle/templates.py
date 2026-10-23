"""
templates.py -- the checklist templates (hr.templates, hr.template_items).

A template is versioned: a case copies the items of the version that was active when it opened, so changing a template
never changes an open case (Plan section 2). The seed file is 26-162 templates\\checklist_templates.csv, loaded by
migrations\\seed_hr_templates_from_csv.py through load_csv_rows() + publish() below.

applies_to holds entity codes; '*' means every diocese (the parish benefits templates, Jay 2026-10-22: all dioceses can
use the CPG forms).
"""
from __future__ import annotations

import csv

import db

from . import conditions
from .errors import HrError

KINDS = {
    "onboard": "Onboarding",
    "offboard": "Offboarding",
    "benefits_enroll": "Parish enrollment request",
    "benefits_end": "Parish end of coverage",
    "change": "Change",
}
SECTIONS = ["documents", "benefits", "payroll", "access", "orientation", "records"]
SECTION_LABELS = {"documents": "Documents", "benefits": "Benefits", "payroll": "Payroll", "access": "Access and IT",
                  "orientation": "Orientation", "records": "Records"}
OWNERS = ["HR", "Benefits", "Payroll", "IT", "Supervisor", "Parish"]
BASES = ["start_date", "separation_date", "benefits_effective", "opened"]

TEMPLATE_LABELS = {
    "diocesan_onboard": ("onboard", "Diocesan staff onboarding (EDOM and Claggett)"),
    "diocesan_offboard": ("offboard", "Diocesan staff offboarding (EDOM and Claggett)"),
    "parish_benefits_enroll": ("benefits_enroll", "Parish request: enroll clergy or staff in benefits"),
    "parish_benefits_end": ("benefits_end", "Parish request: end benefits coverage"),
}


# ------------------------------------------------------------------ reading

def active_template(kind: str, org_code: str) -> dict | None:
    """The active template of this kind for this entity (its own code first, then '*'), with its items in order."""
    rows = db.query(
        "SELECT * FROM hr.templates WHERE case_kind = %s AND is_active "
        "  AND (%s = ANY(applies_to) OR '*' = ANY(applies_to)) "
        "ORDER BY ('*' = ANY(applies_to)), version DESC LIMIT 1",
        (kind, org_code))
    if not rows:
        return None
    t = dict(rows[0])
    t["items"] = items_for(t["id"])
    return t


def items_for(template_id: int) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT * FROM hr.template_items WHERE template_id = %s ORDER BY sort_order, id", (template_id,))]


def list_templates(org_code: str | None = None, include_inactive: bool = False) -> list[dict]:
    rows = db.query(
        "SELECT t.*, (SELECT count(*) FROM hr.template_items i WHERE i.template_id = t.id) AS item_count "
        "FROM hr.templates t WHERE (%s OR t.is_active) "
        "  AND (%s::text IS NULL OR %s = ANY(t.applies_to) OR '*' = ANY(t.applies_to)) "
        "ORDER BY t.key, t.version DESC",
        (include_inactive, org_code, org_code))
    return [dict(r) for r in rows]


def get_template(template_id: int, org_code: str | None = None) -> dict | None:
    """One template with its items; with org_code, only if it applies to that entity (or to every diocese)."""
    t = db.query_one("SELECT * FROM hr.templates WHERE id = %s "
                     "AND (%s::text IS NULL OR %s = ANY(applies_to) OR '*' = ANY(applies_to))",
                     (template_id, org_code, org_code))
    if not t:
        return None
    t = dict(t)
    t["items"] = items_for(t["id"])
    return t


# ------------------------------------------------------------------ loading / publishing

def _int_or_none(v):
    v = (v or "").strip() if isinstance(v, str) else v
    return None if v in (None, "") else int(v)


def load_csv_rows(path: str) -> dict[str, dict]:
    """Read the seed CSV into {template_key: {"case_kind", "applies_to", "label", "items": [...]}} and validate it.
    Raises HrError('invalid') listing every problem found (all of them, not just the first)."""
    problems: list[str] = []
    out: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for n, r in enumerate(csv.DictReader(f), start=2):
            key = (r.get("template_key") or "").strip()
            kind = (r.get("case_kind") or "").strip()
            if kind not in KINDS:
                problems.append(f"line {n}: unknown case_kind '{kind}'")
                continue
            t = out.setdefault(key, {"case_kind": kind,
                                     "applies_to": [c.strip() for c in (r.get("applies_to") or "").split(";") if c.strip()],
                                     "label": TEMPLATE_LABELS.get(key, (kind, key))[1], "items": []})
            item = {
                "item_key": (r.get("item_key") or "").strip(),
                "section": (r.get("section") or "").strip(),
                "label": (r.get("label") or "").strip(),
                "help_text": (r.get("notes") or "").strip() or None,
                "owner_group": (r.get("owner_group") or "").strip(),
                "due_basis": (r.get("due_basis") or "").strip(),
                "due_offset_days": _int_or_none(r.get("due_offset_days")) or 0,
                "hard_deadline_days": _int_or_none(r.get("hard_deadline_days")),
                "required": (r.get("required") or "Y").strip().upper() == "Y",
                "doc_type_key": (r.get("doc_type") or "").strip() or None,
                "condition": (r.get("condition") or "").strip() or None,
                "business_days": (r.get("business_days") or "N").strip().upper() == "Y",
            }
            where = f"line {n} ({key}/{item['item_key']})"
            if not item["item_key"] or not item["label"]:
                problems.append(f"{where}: item_key and label are required")
            if item["section"] not in SECTIONS:
                problems.append(f"{where}: unknown section '{item['section']}'")
            if item["owner_group"] not in OWNERS:
                problems.append(f"{where}: unknown owner_group '{item['owner_group']}'")
            if item["due_basis"] not in BASES:
                problems.append(f"{where}: unknown due_basis '{item['due_basis']}'")
            try:
                conditions.validate(item["condition"])
            except conditions.ConditionError as e:
                problems.append(f"{where}: {e}")
            if any(i["item_key"] == item["item_key"] for i in t["items"]):
                problems.append(f"{where}: duplicate item_key")
            item["sort_order"] = (len(t["items"]) + 1) * 10
            t["items"].append(item)
    if problems:
        raise HrError("invalid", "The checklist file has problems:\n" + "\n".join(problems))
    return out


def same_items(a: list[dict], b: list[dict]) -> bool:
    keys = ["item_key", "section", "label", "help_text", "owner_group", "due_basis", "due_offset_days",
            "hard_deadline_days", "required", "doc_type_key", "condition", "business_days", "sort_order"]
    if len(a) != len(b):
        return False
    return all(all((x.get(k) if x.get(k) != "" else None) == (y.get(k) if y.get(k) != "" else None) for k in keys)
               for x, y in zip(a, b))


def check_doc_types(cur, spec: dict[str, dict]) -> None:
    """Every document type an item names must exist in hr.doc_types (a foreign key), or the whole load fails half way with a
    database error. Checked up front, naming every item with a type the migration does not have."""
    cur.execute("SELECT key FROM hr.doc_types")
    known = {r["key"] for r in cur.fetchall()}
    bad = [f"{key}/{it['item_key']} uses '{it['doc_type_key']}'" for key, t in spec.items() for it in t["items"]
           if it["doc_type_key"] and it["doc_type_key"] not in known]
    if bad:
        raise HrError("invalid", "The checklist file names document types that do not exist in hr.doc_types:\n"
                      + "\n".join(bad) + "\nAdd the type to the migration, or clear the doc_type column for that item.")


def publish(cur, key: str, spec: dict, actor_id: int | None) -> tuple[str, int]:
    """Publish spec as the next version of template `key`, unless the active version already matches it.
    Returns ("unchanged" | "published", version). Uses the caller's cursor (one transaction for the whole seed)."""
    cur.execute("SELECT id, version FROM hr.templates WHERE key = %s AND is_active ORDER BY version DESC LIMIT 1", (key,))
    cur_row = cur.fetchone()
    if cur_row:
        cur.execute("SELECT * FROM hr.template_items WHERE template_id = %s ORDER BY sort_order, id", (cur_row["id"],))
        existing = [dict(r) for r in cur.fetchall()]
        same_targets = True
        cur.execute("SELECT applies_to, case_kind, label FROM hr.templates WHERE id = %s", (cur_row["id"],))
        meta = cur.fetchone()
        if (sorted(meta["applies_to"]) != sorted(spec["applies_to"]) or meta["case_kind"] != spec["case_kind"]
                or meta["label"] != spec["label"]):
            same_targets = False
        if same_targets and same_items(existing, spec["items"]):
            return "unchanged", cur_row["version"]
        cur.execute("UPDATE hr.templates SET is_active = FALSE WHERE id = %s", (cur_row["id"],))
    cur.execute("SELECT coalesce(max(version), 0) AS v FROM hr.templates WHERE key = %s", (key,))
    version = cur.fetchone()["v"] + 1
    cur.execute(
        "INSERT INTO hr.templates (key, version, label, case_kind, applies_to, is_active, published_by_user_id) "
        "VALUES (%s, %s, %s, %s, %s, TRUE, %s) RETURNING id",
        (key, version, spec["label"], spec["case_kind"], spec["applies_to"], actor_id))
    tid = cur.fetchone()["id"]
    for it in spec["items"]:
        cur.execute(
            "INSERT INTO hr.template_items (template_id, item_key, section, label, help_text, owner_group, due_basis, "
            " due_offset_days, business_days, hard_deadline_days, required, doc_type_key, condition, sort_order) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tid, it["item_key"], it["section"], it["label"], it["help_text"], it["owner_group"], it["due_basis"],
             it["due_offset_days"], it["business_days"], it["hard_deadline_days"], it["required"], it["doc_type_key"],
             it["condition"], it["sort_order"]))
    return "published", version
