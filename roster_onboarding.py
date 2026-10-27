"""
roster_onboarding.py -- 26-158 / 26-162: "Move to Onboarding" after Approve on a new-hire request in Roster Review.

Jay (2026-10-27): "a button that says move to onboarding ... probably a check box 'Move to Onboarding' after you press the accept
button." Approving a pending ADD (a new hire) already puts the person on the roster. This module adds the optional second step:
open an onboarding case (26-162) for that person, at that department, linked to the roster row Roster Review just created.

The rules, in one place (Roster Review's two files only call into here):

  * OFFERED only for a pending add, where the diocese has the `hr_lifecycle` switch on AND an active onboarding checklist. Everywhere
    else (production today has neither) nothing is offered and nothing changes.
  * The check box is UNTICKED unless the reviewer ticks it, and the SERVER decides again when the form arrives: a ticked box that is
    not allowed (crafted post, feature off, not a new hire) is ignored with a plain sentence, never an error.
  * THE APPROVAL ALWAYS WINS. This runs after approve_change() has finished. Whatever goes wrong here (a duplicate case, no
    checklist, a department that cannot be an employing unit, a name onboarding refuses, any unexpected error) leaves the approval
    exactly as it is and becomes ONE sentence for the page (REASONS below); nothing is rolled back.
  * The case is linked to the new roster row (hr.cases.staff_id) so that completing it does not add a second roster entry
    (logic/hr_lifecycle/roster_link.on_complete: "if case.staff_id: already on the roster").
  * Only HR Admin / Beacon Admin of the diocese can approve (Roster Review's own gate); this module widens nobody's reach. The page
    shows only a case NUMBER and a link; the case page keeps its own gate.

PRODUCTION HAS NO logic/hr_lifecycle. Nothing from it is imported at module level: _hr() imports it on demand, inside a
try/except, and any failure reads as "not offered". test_roster_review_onboarding.py proves the module still imports and Roster
Review still works when that import fails.

Hours only, never pay; no name, number or address is ever printed or logged here (counts and ids only).
"""
from __future__ import annotations

import json

import db
import org_features
import roster_auto_case

FEATURE = "hr_lifecycle"
FORM_FIELD = "move_to_onboarding"

# One plain sentence per reason code (the code travels in the redirect, never the sentence: nothing typed in a link is shown back).
REASONS = {
    "not_offered": "onboarding is not offered for this request",
    "automatic": "a case is opened automatically when a new hire is approved",
    "not_available": "onboarding is not turned on here",
    "no_checklist": "there is no active onboarding checklist for this entity",
    "duplicate": "this person already has an open onboarding case",
    "unit": "this department cannot be used as an employing unit for onboarding",
    "details": "onboarding did not accept the details on the request (for example the name)",
    "refused": "onboarding could not open it",
    "error": "something went wrong while opening it",
}


def sentence(code) -> str | None:
    """The page sentence for a reason code, or None for a code we do not know (a made-up link shows nothing)."""
    reason = REASONS.get(code or "")
    if not reason:
        return None
    return f"Approved and added to the roster, but no onboarding case was opened: {reason}."


def ticked(form) -> bool:
    """Did the reviewer tick Move to Onboarding? A plain POST with no such field is False (today's behaviour)."""
    try:
        return str(form.get(FORM_FIELD) or "").strip().lower() in ("1", "on", "yes", "true", "y")
    except Exception:
        return False


# ------------------------------------------------------------------ the onboarding engine, imported only when asked for

def _hr():
    """(cases, templates, HrError) or None. Production has no logic/hr_lifecycle: any failure to import it means 'not offered'."""
    try:
        from logic.hr_lifecycle import cases, templates
        from logic.hr_lifecycle.errors import HrError
    except Exception:  # ImportError, or anything the package raises while it loads
        return None
    return cases, templates, HrError


def _why_not_available(org_id) -> str | None:
    """None when the diocese can open onboarding cases; else the reason code."""
    if not org_id or not org_features.is_enabled(org_id, FEATURE):
        return "not_available"
    hr = _hr()
    if hr is None:
        return "not_available"
    cases, templates, _err = hr
    try:
        if templates.active_template("onboard", cases.org_code(org_id)) is None:
            return "no_checklist"
    except Exception:
        return "not_available"
    return None


def why_not_offered(org_id, change) -> str | None:
    """None when the pop-up / the tick is allowed for this request; else the reason code."""
    if not change or change.get("change_type") != "add" or change.get("status", "pending") != "pending":
        return "not_offered"
    if change.get("existing_staff_id"):
        # the page's own "already on the roster" flow (Reactivate / Add anyway) is left exactly as it is
        return "not_offered"
    if roster_auto_case.applies(org_id, change.get("parish_id")) is None:
        # 26-162 (Jay, 2026-10-27: "every roster add makes a case"): approving a new hire opens the case itself, so there is nothing
        # to ask. This box and pop-up are kept only for a request the automatic rule does not cover.
        return "automatic"
    return _why_not_available(org_id)


def offered(org_id, change) -> bool:
    return why_not_offered(org_id, change) is None


def offered_ids(org_id, changes) -> set:
    """The ids of the pending requests the page gives the Move to Onboarding pop-up (one availability check for the whole page)."""
    try:
        # a request the automatic rule covers (every roster add makes a case) is not asked about
        adds = [c for c in (changes or []) if c.get("change_type") == "add" and not c.get("existing_staff_id")
                and roster_auto_case.applies(org_id, c.get("parish_id")) is not None]
        if not adds or _why_not_available(org_id) is not None:
            return set()
        return {c["id"] for c in adds}
    except Exception:
        return set()


# ------------------------------------------------------------------ opening the case

def _case_fields(change: dict) -> dict:
    """What the request knows about the new hire, in the shape logic.hr_lifecycle.cases.open_case takes. Hours and pay are not on a
    request; clergy status is not either (False until a person sets it on the case's setup screen)."""
    raw = {
        "first_name": change.get("proposed_first_name"),
        "last_name": change.get("proposed_last_name"),
        "position": change.get("proposed_position"),
        "parish_id": change.get("parish_id"),
        "payroll_employee_ref": change.get("proposed_employee_number"),
        "is_clergy": False,
    }
    if change.get("as_of_date"):  # the hire date the parish gave
        raw["start_date"] = change["as_of_date"]
    return raw


def _fail(code: str) -> dict:
    return {"ok": False, "reason": code, "sentence": sentence(code)}


def _link(case_id: int, staff_id, change: dict, actor_id: int) -> bool:
    """Point the case at the roster row Roster Review just created (so completing it adds nobody twice) and record where the case came
    from. True when the link was made. The roster row must be in the same department as the case."""
    if not staff_id:
        return False
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE hr.cases SET staff_id = s.id, last_activity_at = now(), updated_at = now() "
                "FROM portal.staff_roster s WHERE hr.cases.id = %s AND s.id = %s AND s.parish_id = hr.cases.parish_id "
                "AND hr.cases.staff_id IS NULL", (case_id, staff_id))
            linked = cur.rowcount == 1
            # hr.cases.source only allows 'hr' or 'parish', so the origin is kept as a history line on the case (ids only)
            cur.execute(
                "INSERT INTO hr.case_events (case_id, event, detail, actor_user_id) VALUES (%s, 'from_roster_review', %s, %s)",
                (case_id, json.dumps({"roster_change_id": change.get("id"), "staff_id": staff_id, "linked": linked}), actor_id))
        conn.commit()
    return linked


def open_for_new_hire(org_id: int, change: dict, staff_id, actor_id: int) -> dict:
    """Open the onboarding case for an approved new hire. NEVER raises: returns {"ok": True, "case_id", "case_number", "linked"} or
    {"ok": False, "reason", "sentence"}. Call it only after the approval has committed."""
    try:
        why = why_not_offered(org_id, change)
        if why:
            return _fail(why)
        cases, _templates, HrError = _hr()
        raw = _case_fields(change)
        parish_id = change.get("parish_id")
        first = (raw["first_name"] or "").strip()
        last = (raw["last_name"] or "").strip()
        # the same two checks open_case makes, run first so the sentence can say which one stopped it
        try:
            cases._check_unit(org_id, parish_id, "onboard")
        except HrError:
            return _fail("unit")
        try:
            cases._check_not_duplicate(org_id, "onboard", first, last, parish_id)
        except HrError:
            return _fail("duplicate")
        try:
            case_id = cases.open_case(org_id, "onboard", raw, actor_id, source="hr")
        except HrError as e:
            return _fail("details" if e.code in ("invalid", "pii") else "refused")
        number = None
        row = db.query_one("SELECT case_number FROM hr.cases WHERE id = %s", (case_id,))
        if row:
            number = row["case_number"]
        try:
            linked = _link(case_id, staff_id, change, actor_id)
        except Exception as exc:  # the case exists; say so, and say the link is missing
            print(f"[roster_onboarding] case {case_id} opened but not linked ({type(exc).__name__})")
            linked = False
        return {"ok": True, "case_id": case_id, "case_number": number, "linked": linked}
    except Exception as exc:  # the approval already stands: no detail of the person is ever printed
        print(f"[roster_onboarding] could not open a case for roster change {change.get('id')} ({type(exc).__name__})")
        return _fail("error")


def redirect_query(result: dict) -> str:
    """The query string that goes after ?approved=1 for this result (ids and fixed codes only)."""
    if result.get("ok"):
        return f"&onboarding_case={int(result['case_id'])}" + ("" if result.get("linked") else "&onboarding_linked=0")
    return f"&onboarding_note={result.get('reason') or 'error'}"


# ------------------------------------------------------------------ what the page shows after the redirect

def case_summary(org_id: int, case_id: int) -> dict | None:
    """{id, case_number} of an onboarding case in THIS diocese (a case id from another diocese reads as nothing)."""
    try:
        row = db.query_one("SELECT id, case_number FROM hr.cases WHERE id = %s AND org_id = %s AND case_kind = 'onboard'",
                           (case_id, org_id))
    except Exception:
        return None
    return dict(row) if row else None


def banner_context(org_id: int, params) -> dict:
    """{"case": {...} | None, "note": sentence | None} from the redirect's query parameters."""
    out = {"case": None, "note": None}
    raw = params.get("onboarding_case")
    if raw and raw.isascii() and raw.isdigit():
        out["case"] = case_summary(org_id, int(raw))
        if out["case"] is not None and params.get("onboarding_linked") == "0":
            out["case"]["unlinked"] = True
    out["note"] = sentence(params.get("onboarding_note"))
    return out
