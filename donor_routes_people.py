"""
donor_routes_people.py -- Beacon Donor Management: the People screens (search, new person, the person record).

Routes only: resolve the parish (donor_web.gate), call ONE service function from the donor_* modules, render or
redirect. No business rules live here. A parish id never comes from the browser. Static paths (/people/new,
/people/api/search) are declared before /people/{person_id} so they are not read as an id.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db
import donor_changelog as CL
import donor_households as H
import donor_membership as MS
import donor_merge as MG
import donor_notes as N
import donor_people as P
import donor_personal as PS
import donor_portal as PP
import donor_portal_admin as PA
import donor_roles
import donor_web as W
from donor_core import DonorError, NotFound, initials

router = APIRouter(dependencies=[Depends(W.check_path_ids)])
PAGE_SIZE = 50
TABS = (("personal", "Personal"), ("membership", "Membership"), ("sacraments", "Sacraments"),
        ("giving", "Giving"), ("notes", "Notes & tasks"), ("system", "System"))


def register(app) -> None:
    app.include_router(router)


def _opt_int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ── Search and list ─────────────────────────────────────────────────────────────────────────────
@router.get("/people", response_class=HTMLResponse)
def people_list(request: Request, q: str = "", status: str = "", gender: str = "", marital: str = "", rtype: str = "",
                archived: str = "", page: int = 1):
    user, parish, ctx, resp = W.gate(request)
    if resp:
        return resp
    page = max(1, page)
    rows, total, error = [], 0, None
    try:
        res = P.person_search(ctx, q, member_status=status or None, gender=gender or None, marital_status=marital or None,
                              record_type=rtype or None, include_archived=bool(archived), limit=PAGE_SIZE,
                              offset=(page - 1) * PAGE_SIZE)
        rows, total = res["rows"], res["total"]
    except DonorError as e:
        error = e.message
    codes = MS.status_code_list(ctx) if ctx.can("membership.view") else []
    # Get Help messages from parishioners (Parishioner Self-Service): the staff-visible record, People editors only. Fail soft.
    portal_messages = W.safe(lambda: PP.messages_waiting(ctx), []) if (ctx.settings.get("portal_enabled") and ctx.can("people.edit")) else []
    return W.page(request, "donor_people.html", user, parish, ctx, "people", {
        "portal_messages": portal_messages,
        "rows": rows, "total": total, "q": q, "status": status, "gender": gender, "marital": marital, "rtype": rtype,
        "archived": archived, "page_no": page, "page_size": PAGE_SIZE, "codes": codes, "search_error": error,
        "pages": (total + PAGE_SIZE - 1) // PAGE_SIZE,
    })


@router.get("/people/api/search")
def people_api_search(request: Request, q: str = ""):
    """JSON for the person pickers (spouse, household). Same gate, same parish scoping as the list."""
    user, parish, ctx, resp = W.gate(request)
    if resp:
        return JSONResponse({"error": "not available"}, status_code=403)
    if len((q or "").strip()) < 2:
        return JSONResponse({"results": []})
    try:
        rows = P.person_search(ctx, q, limit=10)["rows"]
    except DonorError:
        return JSONResponse({"results": []})
    return JSONResponse({"results": [{"id": r["id"], "name": r["name"], "detail": ", ".join(
        x for x in (r.get("city"), r.get("email")) if x)} for r in rows]})


# ── New person ──────────────────────────────────────────────────────────────────────────────────
def _new_form_data(form) -> dict:
    return {k: (form.get(k) or "") for k in (
        "first_name", "middle_name", "last_name", "goes_by", "org_name", "birth_date", "gender", "email", "phone_cell",
        "phone_home", "record_type", "connection")}


@router.get("/people/new", response_class=HTMLResponse)
def person_new_form(request: Request):
    user, parish, ctx, resp = W.gate(request, need="people.create")
    if resp:
        return resp
    return W.page(request, "donor_person_new.html", user, parish, ctx, "people", {"v": {"record_type": "person"}, "matches": [], "dups": []})


@router.post("/people/new")
async def person_new_submit(request: Request):
    user, parish, ctx, resp = W.gate(request, need="people.create")
    if resp:
        return resp
    form = await request.form()
    v = _new_form_data(form)
    is_org = v["record_type"] == "organization"
    data = {k: v[k] for k in ("first_name", "middle_name", "last_name", "goes_by", "birth_date", "gender") if v[k]} if not is_org else {"org_name": v["org_name"]}
    contacts = []
    if v["email"]:
        contacts.append({"kind": "email", "value": v["email"], "is_preferred": True})
    if v["phone_cell"]:
        contacts.append({"kind": "phone", "value": v["phone_cell"], "subtype": "cell", "is_preferred": True})
    if v["phone_home"]:
        contacts.append({"kind": "phone", "value": v["phone_home"], "subtype": "home"})
    try:
        # One shared profile: if the email, or name + birth date, matches a person another parish already has,
        # offer to connect to that profile instead of creating a second one.
        matches = []
        if not form.get("skip_match") and not is_org:
            matches = P.find_profile_matches(ctx, email=v["email"] or None, first_name=v["first_name"] or None,
                                             last_name=v["last_name"] or None, birth_date=v["birth_date"] or None)
        if matches:
            return W.page(request, "donor_person_new.html", user, parish, ctx, "people", {"v": v, "matches": matches, "dups": []})
        res = P.person_create(ctx, data, record_type="organization" if is_org else "person",
                              connection_kind=v["connection"] or None, contacts=contacts,
                              allow_duplicate=bool(form.get("allow_duplicate")))
        return W.back(request, f"/people/{res['id']}", ok="Person added.")
    except DonorError as e:
        dups = e.details if isinstance(e.details, list) else []
        return W.page(request, "donor_person_new.html", user, parish, ctx, "people", {
            "v": v, "matches": [], "dups": dups, "error": e.message}, status_code=409 if dups else 400)


@router.post("/people/link")
async def person_link(request: Request):
    """Connect this parish to a profile another parish already has, using the proof (email, or name and birth
    date) that found it. A bare person id is never enough: person_link_existing re-checks the proof."""
    user, parish, ctx, resp = W.gate(request, need="people.create")
    if resp:
        return resp
    form = await request.form()
    pid = _opt_int(form.get("person_id"))
    try:
        if pid is None:
            raise NotFound("That profile could not be matched with what you entered.")
        P.person_link_existing(ctx, pid, {"email": form.get("email") or None, "first_name": form.get("first_name") or None,
                                          "last_name": form.get("last_name") or None, "birth_date": form.get("birth_date") or None},
                               form.get("connection") or "giver")
        return W.back(request, f"/people/{pid}", ok="Connected. This person's shared profile is now part of this parish.")
    except DonorError as e:
        return W.back(request, "/people/new", err=e.message)


# ── The person record ───────────────────────────────────────────────────────────────────────────
def _allowed_tabs(ctx) -> list[tuple[str, str]]:
    out = []
    for key, lbl in TABS:
        if key == "membership" and not ctx.can("membership.view"):
            continue
        if key == "sacraments" and not ctx.can("sacrament.view"):
            continue
        if key == "giving" and not (ctx.settings.get("giving_enabled") and ctx.can("giving.read")):
            continue
        if key == "notes" and not ctx.can("notes.staff"):
            continue
        out.append((key, lbl))
    return out


@router.get("/people/{person_id}", response_class=HTMLResponse)
def person_record(person_id: int, request: Request, tab: str = "personal", voided: str = ""):
    user, parish, ctx, resp = W.gate(request)
    if resp:
        return resp
    try:
        g = P.person_get(ctx, person_id)
    except NotFound:
        return W.page(request, "donor_off.html", user, parish, ctx, "people", {"reason": "notfound", "feature": "people"}, status_code=404)
    tabs = _allowed_tabs(ctx)
    if tab not in {k for k, _ in tabs}:
        tab = "personal"
    other_ids = [o["parish_id"] for o in g["other_connections"]]
    other_names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM portal.parishes WHERE id = ANY(%s)", (other_ids,))} if other_ids else {}
    extra: dict = {"g": g, "p": g["person"], "tab": tab, "tabs": tabs, "pid": person_id,
                   "title_name": g["person"]["full_name"] or "Person", "initials": initials(g["person"]),
                   "other_names": other_names, "access": [], "show_access": False}
    if tab == "system" and ctx.can("roles.manage") and not g["redacted"]:
        # The System tab's "User account" panel (like TouchPoint's): which Beacon login is this person, and what they may do here.
        extra["access"] = donor_roles.roles_for_person(ctx, person_id)
        extra["show_access"] = True
    if tab == "membership":
        extra.update(codes=MS.status_code_list(ctx), adult=MS.adult_member_status(ctx, person_id),
                     letters=MS.transfer_letter_list(ctx, person_id))
    elif tab == "sacraments":
        extra.update(events=MS.sacrament_list(ctx, person_id, include_voided=bool(voided)), show_voided=bool(voided))
    elif tab == "notes":
        extra.update(notes=N.notes_for_person(ctx, person_id), tasks=N.tasks_for_person(ctx, person_id),
                     assignees=donor_roles.assignable_users(ctx))
    elif tab == "giving":
        try:
            import donor_gifts
            extra.update(giving=donor_gifts.person_giving_summary(ctx, person_id))
        except ImportError:
            extra.update(giving=None)
    elif tab == "system":
        extra.update(changes=CL.changes_for_person(ctx, person_id), merges=MG.merge_history_for(ctx, person_id))
        if ctx.can("roles.manage") and not g["redacted"]:
            # "Parishioner login (self-service)": the member's own login, created ONLY here and separate from the Beacon staff login above.
            extra["portal_login"] = W.safe(lambda: PA.login_panel(ctx, person_id), None)
    return W.page(request, "donor_person.html", user, parish, ctx, "people", extra)


async def _act(request: Request, person_id: int, tab: str, action):
    """Run one service call for a POST and redirect back to the record with the result as a flash message."""
    user, parish, ctx, resp = W.gate(request)
    if resp:
        return resp
    form = await request.form()
    url = f"/people/{person_id}?tab={tab}"
    try:
        msg = action(ctx, form)
        return W.back(request, url, ok=msg)
    except DonorError as e:
        return W.back(request, url, err=e.message)


def _g(form, key):
    v = form.get(key)
    return v if v not in (None, "") else None


@router.post("/people/{person_id}/personal")
async def post_personal(person_id: int, request: Request):
    """The Personal tab's one Save (see donor_personal): profile, contacts, household, this parish's connection."""
    def act(ctx, form):
        r = PS.personal_save(ctx, person_id, form)
        return "Saved." if r["changed"] else "Nothing to change."
    return await _act(request, person_id, "personal", act)


@router.post("/people/{person_id}/profile")
async def post_profile(person_id: int, request: Request):
    def act(ctx, form):
        changes = {}
        for f in P.EDITABLE_FIELDS:
            if f in P.BOOL_FIELDS:
                changes[f] = form.get(f) is not None            # an unchecked box is simply absent
            elif f in form:
                changes[f] = form.get(f)
        r = P.person_update(ctx, person_id, changes, reason=_g(form, "reason"))
        return "Saved." if r["changed"] else "Nothing to change."
    return await _act(request, person_id, "personal", act)


@router.post("/people/{person_id}/contact/add")
async def post_contact_add(person_id: int, request: Request):
    def act(ctx, form):
        P.contact_add(ctx, person_id, form.get("kind"), form.get("value"), form.get("subtype") or "other",
                      form.get("is_preferred") is not None)
        return "Contact added."
    return await _act(request, person_id, "personal", act)


@router.post("/people/{person_id}/contact/{contact_id}/update")
async def post_contact_update(person_id: int, contact_id: int, request: Request):
    def act(ctx, form):
        P.contact_update(ctx, contact_id, value=_g(form, "value"), subtype=_g(form, "subtype"),
                         is_preferred=True if form.get("make_preferred") else None)
        return "Contact saved."
    return await _act(request, person_id, "personal", act)


@router.post("/people/{person_id}/contact/{contact_id}/archive")
async def post_contact_archive(person_id: int, contact_id: int, request: Request):
    def act(ctx, form):
        P.contact_archive(ctx, contact_id)
        return "Contact removed."
    return await _act(request, person_id, "personal", act)


@router.post("/people/{person_id}/household")
async def post_household(person_id: int, request: Request):
    def act(ctx, form):
        action = form.get("action")
        g = P.person_get(ctx, person_id)
        hid = g["household"]["household_id"] if g["household"] else None
        if action == "address":
            if hid is None:
                H.household_create(ctx, {k: form.get(k) for k in H.HOUSEHOLD_FIELDS if k in form},
                                   [{"person_id": person_id, "position": form.get("position") or "primary_adult",
                                     "is_primary_contact": True}])
                return "Household created."
            H.household_update(ctx, hid, {k: form.get(k) for k in H.HOUSEHOLD_FIELDS if k in form})
            return "Household saved."
        if action == "new":
            H.household_move_member(ctx, person_id, None, form.get("position") or "primary_adult")
            return "Moved to a new household."
        if action == "join":
            other = _opt_int(form.get("other_person_id"))
            if other is None:
                raise NotFound("Pick the person whose household to join.")
            og = P.person_get(ctx, other)
            if not og["household"]:
                raise NotFound("That person is not in a household yet.")
            H.household_move_member(ctx, person_id, og["household"]["household_id"], form.get("position") or "secondary_adult")
            return "Household joined."
        if action == "primary":
            if hid is None:
                raise NotFound("This person is not in a household.")
            H.household_set_primary_contact(ctx, hid, _opt_int(form.get("member_id")) or person_id)
            return "Primary contact set."
        if action == "position":
            H.household_set_position(ctx, _opt_int(form.get("member_id")) or person_id, form.get("position"))
            return "Position saved."
        if action == "relation":
            other = _opt_int(form.get("other_person_id"))
            og = P.person_get(ctx, other) if other is not None else None
            if hid is None or not og or not og["household"]:
                raise NotFound("Both people need to be in a household.")
            H.household_relation_add(ctx, hid, og["household"]["household_id"], form.get("description"))
            return "Related household added."
        if action == "relation_archive":
            H.household_relation_archive(ctx, _opt_int(form.get("relation_id")) or 0)
            return "Relationship removed."
        raise NotFound("Unknown household action.")
    return await _act(request, person_id, "personal", act)


@router.post("/people/{person_id}/spouse")
async def post_spouse(person_id: int, request: Request):
    def act(ctx, form):
        if form.get("action") == "end":
            H.spouse_link_end(ctx, person_id, _g(form, "reason"))
            return "Spouse link ended."
        other = _opt_int(form.get("spouse_person_id"))
        if other is None:
            raise NotFound("Pick the spouse.")
        H.spouse_link_set(ctx, person_id, other, _g(form, "married_on"))
        return "Spouse linked."
    return await _act(request, person_id, "personal", act)


@router.post("/people/{person_id}/connection")
async def post_connection(person_id: int, request: Request):
    def act(ctx, form):
        changes = {k: form.get(k) for k in ("kind", "envelope_number", "statement_option", "statement_delivery") if k in form}
        if "has_canonical" in form:
            changes["is_canonical"] = form.get("is_canonical") is not None
        r = H.parish_connection_set(ctx, person_id, changes)
        return "Saved." if r["changed"] else "Nothing to change."
    return await _act(request, person_id, "personal", act)


@router.post("/people/{person_id}/membership")
async def post_membership(person_id: int, request: Request):
    def act(ctx, form):
        data = {k: form.get(k) for k in ("status_code_id", "how_joined", "join_date", "removal_date", "removal_reason") if k in form}
        r = MS.membership_update(ctx, person_id, data)
        return "Saved." if r["changed"] else "Nothing to change."
    return await _act(request, person_id, "membership", act)


@router.post("/people/{person_id}/standing")
async def post_standing(person_id: int, request: Request):
    def act(ctx, form):
        MS.standing_review(ctx, person_id, form.get("standing"), _g(form, "reviewed_on"))
        return "Canonical standing recorded."
    return await _act(request, person_id, "membership", act)


@router.post("/people/{person_id}/transfer")
async def post_transfer(person_id: int, request: Request):
    def act(ctx, form):
        data = {k: form.get(k) for k in ("direction", "other_parish", "requested_on", "issued_on", "received_on", "status", "notes") if k in form}
        MS.transfer_letter_update(ctx, person_id, data, _opt_int(form.get("letter_id")))
        return "Transfer letter saved."
    return await _act(request, person_id, "membership", act)


@router.post("/people/{person_id}/sacrament/add")
async def post_sacrament_add(person_id: int, request: Request):
    def act(ctx, form):
        MS.sacrament_add(ctx, person_id, form.get("kind"), _g(form, "event_date"), _g(form, "place"), _g(form, "officiant_name"),
                         _g(form, "register_ref"), _g(form, "notes"), _opt_int(form.get("related_person_id")),
                         form.get("date_approximate") is not None)
        return "Recorded."
    return await _act(request, person_id, "sacraments", act)


@router.post("/people/{person_id}/sacrament/{event_id}/void")
async def post_sacrament_void(person_id: int, event_id: int, request: Request):
    def act(ctx, form):
        MS.sacrament_void(ctx, event_id, form.get("reason") or "")
        return "Record voided (kept in the register)."
    return await _act(request, person_id, "sacraments", act)


@router.post("/people/{person_id}/note/add")
async def post_note_add(person_id: int, request: Request):
    def act(ctx, form):
        N.note_add(ctx, person_id, form.get("body") or "", form.get("visibility") or "staff", _g(form, "keywords"))
        return "Note added."
    return await _act(request, person_id, "notes", act)


@router.post("/people/{person_id}/note/{note_id}/archive")
async def post_note_archive(person_id: int, note_id: int, request: Request):
    def act(ctx, form):
        N.note_archive(ctx, note_id)
        return "Note archived."
    return await _act(request, person_id, "notes", act)


@router.post("/people/{person_id}/task/add")
async def post_task_add(person_id: int, request: Request):
    def act(ctx, form):
        N.task_add(ctx, person_id, form.get("title") or "", _g(form, "details"), _g(form, "due_date"), _opt_int(form.get("assigned_to_user_id")))
        return "Task added."
    return await _act(request, person_id, "notes", act)


@router.post("/people/{person_id}/task/{task_id}/update")
async def post_task_update(person_id: int, task_id: int, request: Request):
    def act(ctx, form):
        N.task_update(ctx, task_id, status=_g(form, "status"))
        return "Task updated."
    return await _act(request, person_id, "notes", act)


@router.post("/people/{person_id}/archive")
async def post_archive(person_id: int, request: Request):
    def act(ctx, form):
        r = P.person_archive(ctx, person_id, _g(form, "reason"))
        return "Archived at this parish." if r["changed"] else "Already archived."
    return await _act(request, person_id, "personal", act)


@router.post("/people/{person_id}/restore")
async def post_restore(person_id: int, request: Request):
    def act(ctx, form):
        P.person_restore(ctx, person_id)
        return "Restored."
    return await _act(request, person_id, "personal", act)


@router.post("/people/{person_id}/undo/{change_id}")
async def post_undo(person_id: int, change_id: int, request: Request):
    def act(ctx, form):
        CL.change_undo(ctx, change_id)
        return "Change undone."
    return await _act(request, person_id, "system", act)
