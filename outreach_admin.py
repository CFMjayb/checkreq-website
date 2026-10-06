"""
outreach_admin.py -- 26-156: the admin screens for polls (the first kind of the shared
Email Response Engine) plus the scheduler route that sends reminders.

    /admin/polls                    list this entity's polls
    /admin/polls/new                create (audience picker with a live recipient count, question builder)
    /admin/polls/{id}               status, counts, results, who has / has not answered, event log
    /admin/polls/{id}/edit          change a DRAFT
    /admin/polls/{id}/send          freeze the recipients and mint their links (then the page sends in chunks)
    /admin/polls/{id}/remind-chunk  the manual "remind non-responders" button
    /admin/polls/{id}/export.csv    results as CSV
    POST /internal/send-outreach-reminders   Cloud Scheduler -> reminders + sweep of expired polls

AUTHORIZATION (the 2026-10-05 cross-diocese lesson): every route requires Beacon Admin at the
poll's OWN entity (the session's current entity, which must equal the poll's org_id -- a poll
of another entity is a 404). The audience picker may reach other entities only where the
person is ALSO a Beacon Admin, re-checked on preview, save AND send; the preview never lists
people from an entity the person does not administer. No role check here ever uses org_id=None.

New file per the standing rule: main.py only imports this and calls register().
"""
from __future__ import annotations

import csv
import hmac
import io
import json
import os
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import beacon_address
import db
import org_time
import outreach
import outreach_kinds
import parish_roles
import rbac

router = APIRouter()

_current_user = None
_current_org = None
_render = None
_get_internal_key = None

BEACON_ENV = os.environ.get("BEACON_ENV", "dev")
POLL = outreach_kinds.POLL
MAX_TITLE, MAX_SUBJECT, MAX_INTRO = 150, 200, 4000


def register(app, *, current_user, current_org, render, get_internal_key=None) -> None:
    global _current_user, _current_org, _render, _get_internal_key
    _current_user, _current_org, _render = current_user, current_org, render
    _get_internal_key = get_internal_key
    app.include_router(router)


# ---------------------------------------------------------------------------
# Guards and small helpers
# ---------------------------------------------------------------------------
def _guard(request: Request, *, as_json: bool = False):
    """-> (user, org, None) or (None, None, error response). Beacon Admin at the CURRENT entity."""
    def refuse(status, text):
        return JSONResponse({"error": text}, status_code=status) if as_json else HTMLResponse(text, status_code=status)

    user = _current_user(request)
    if not user:
        return None, None, (JSONResponse({"error": "Sign in required"}, status_code=401) if as_json
                            else RedirectResponse("/login"))
    org = _current_org(request)
    if not org or not rbac.user_has_any_role(user["id"], list(outreach.ADMIN_ROLES), org_id=org["id"]):
        return None, None, refuse(403, "Beacon Admin access at this entity is required.")
    if not outreach.tables_ready():
        return None, None, refuse(503, "Polls are not set up yet (migration 073 has not been applied).")
    return user, org, None


def _campaign(org: dict, cid: int) -> dict | None:
    c = outreach.get_campaign(cid)
    if not c or c["kind"] != "poll" or c["org_id"] != org["id"]:
        return None
    return c


def _not_found():
    return HTMLResponse("Poll not found.", status_code=404)


_AUDIENCE_REFUSAL = ("This draft's audience includes entities you are not a Beacon Admin at, so only an admin "
                     "of those entities can see who it reaches, edit it or send it.")


def _audience_errors(user, org, cid: int) -> list[str]:
    """Does this viewer administer EVERY entity the saved audience reaches? A poll belongs to one
    entity, but its audience can reach others (where its creator was also Beacon Admin). Someone
    who administers only the owning entity must not learn who holds roles elsewhere, so the draft's
    recipient list, edit form and Send are withheld from them (2026-10-06 security review)."""
    return outreach.authorize_selectors(user["id"], org["id"], outreach.get_audience(cid), POLL)


def _base_url(org_id: int, request: Request) -> str:
    """Prod links use the entity's own branded Beacon address; dev stays on the host the
    request came in on, so a dev email never links into production. (Same rule as
    main._entity_base_url -- not imported, this module must never import main.)"""
    if BEACON_ENV == "prod":
        return "https://" + beacon_address.beacon_address(org_id)
    return str(request.base_url).rstrip("/")


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _zone(org_id: int) -> str:
    return org_time.zone_name_for_org(org_id)


def _parse_local(value: str, org_id: int):
    """A <input type=datetime-local> value, read in the entity's time zone -> UTC. Blank -> None."""
    value = (value or "").strip()
    if not value:
        return None
    naive = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    return naive.replace(tzinfo=ZoneInfo(_zone(org_id))).astimezone(timezone.utc)


def _to_local_input(dt, org_id: int) -> str:
    if not dt:
        return ""
    return dt.astimezone(ZoneInfo(_zone(org_id))).strftime("%Y-%m-%dT%H:%M")


def _fmt(dt, org_id: int) -> str:
    return org_time.format_local(dt, _zone(org_id)) if dt else ""


def _redirect(cid: int, **q) -> RedirectResponse:
    qs = "&".join(f"{k}={quote(str(v))}" for k, v in q.items())
    return RedirectResponse(f"/admin/polls/{cid}" + (f"?{qs}" if qs else ""), status_code=303)


def _clean_selectors(raw) -> list[dict]:
    """The browser's audience rows -> engine selectors. Only the two role sources come from the
    UI (a typed-in list is for other kinds). Everything is re-validated by the engine."""
    out = []
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict) or s.get("source") not in ("entity_role", "parish_role"):
            continue
        scope = "parish" if s["source"] == "parish_role" and s.get("scope") == "parish" else "org"
        out.append({"source": s["source"], "role_key": str(s.get("role_key") or "").strip(),
                    "org_id": _int(s.get("org_id")), "scope": scope,
                    "parish_id": _int(s.get("parish_id")) if scope == "parish" else None})
    return out


def _csv_safe(value) -> str:
    """Spreadsheet formula injection: a cell that starts with = + - @ (or a tab/CR) is
    treated as a formula by Excel. Answers are typed by outside people, so neutralize them."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _admin_orgs(user_id: int) -> list[dict]:
    """Entities where this person is a Beacon Admin: the only ones the audience picker offers."""
    ids = rbac.get_granted_org_ids(user_id, "beacon_admin")
    if not ids:
        return []
    return db.query("SELECT id, code, name FROM checkreq.organizations WHERE id = ANY(%s) ORDER BY code", (ids,))


def _form_context(request, user, org, *, campaign=None, values=None, selectors=None, questions=None, errors=None):
    if values is None:
        if campaign:
            values = {
                "title": campaign["title"], "subject": campaign["subject"], "intro": campaign["intro"],
                "sender_email": campaign["sender_email"],
                "closes_at": _to_local_input(campaign["closes_at"], org["id"]),
                "reminder_every_days": campaign["reminder_every_days"] or "",
                "allow_change": campaign["allow_change"], "test_mode": campaign["test_mode"],
                "test_address": campaign["test_address"] or "",
            }
        else:
            values = {"title": "", "subject": "", "intro": "", "sender_email": outreach.default_sender(org["code"]),
                      "closes_at": "", "reminder_every_days": "", "allow_change": True,
                      "test_mode": False, "test_address": ""}
    if selectors is None:
        selectors = [{"source": a["source"], "role_key": a["role_key"], "org_id": a["org_id"],
                      "scope": a["scope"], "parish_id": a["parish_id"]}
                     for a in (outreach.get_audience(campaign["id"]) if campaign else [])
                     if a["source"] in ("entity_role", "parish_role")]
    if questions is None:
        questions = [{"qtype": q["qtype"], "prompt": q["prompt"], "required": q["required"], "config": q["config"]}
                     for q in (POLL.questions(campaign["id"]) if campaign else [])]
    init = {
        "selectors": selectors or [{"source": "entity_role", "role_key": "", "org_id": org["id"],
                                    "scope": "org", "parish_id": None}],
        "questions": questions or [{"qtype": "yes_no", "prompt": "", "required": True, "config": {}}],
        "orgs": _admin_orgs(user["id"]),
        "entity_roles": [{"key": r["key"], "label": r["label"]} for r in rbac.all_roles()],
        "parish_roles": [{"key": r["key"], "label": r["label"]} for r in parish_roles.all_parish_roles()],
        "question_types": [{"key": k, "label": qt.label} for k, qt in outreach_kinds.QUESTION_TYPES.items()],
        "current_org_id": org["id"],
    }
    zone_label = org_time.format_local(datetime.now(timezone.utc), _zone(org["id"])).split()[-1]
    return {"editing": campaign, "values": values, "errors": errors or [], "init": init,
            "senders": list(outreach.SENDERS), "zone_label": zone_label}


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@router.get("/admin/polls", response_class=HTMLResponse)
def polls_list(request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    rows = []
    for c in outreach.list_campaigns([org["id"]], kind="poll"):
        n = c["n_recipients"]
        rows.append({**c, "deadline_text": _fmt(c["closes_at"], org["id"]) or "No deadline",
                     "created_text": _fmt(c["created_at"], org["id"]),
                     "rate": f"{round(100 * c['n_responded'] / n)}%" if n else ""})
    return _render(request, "admin_outreach_list.html", user, {"polls": rows})


# ---------------------------------------------------------------------------
# Create / edit
# ---------------------------------------------------------------------------
def _read_form(form) -> dict:
    return {
        "title": (form.get("title") or "").strip(),
        "subject": (form.get("subject") or "").strip(),
        "intro": (form.get("intro") or "").strip(),
        "sender_email": (form.get("sender_email") or "").strip(),
        "closes_at": (form.get("closes_at") or "").strip(),
        "reminder_every_days": (form.get("reminder_every_days") or "").strip(),
        "allow_change": form.get("allow_change") is not None,
        "test_mode": form.get("test_mode") is not None,
        "test_address": (form.get("test_address") or "").strip(),
    }


def _validate_form(user, org, v: dict, selectors_raw, questions_raw):
    """-> (errors, parsed) where parsed = {'fields', 'selectors', 'questions'}. Writes nothing."""
    errors, fields = [], {}
    if not v["title"]:
        errors.append("Give the poll a title.")
    elif len(v["title"]) > MAX_TITLE:
        errors.append(f"The title is longer than {MAX_TITLE} characters.")
    if len(v["subject"]) > MAX_SUBJECT:
        errors.append(f"The email subject is longer than {MAX_SUBJECT} characters.")
    if len(v["intro"]) > MAX_INTRO:
        errors.append(f"The message is longer than {MAX_INTRO} characters.")
    if v["sender_email"] not in outreach.SENDERS:
        errors.append("Choose one of the available From addresses.")
    closes = None
    try:
        closes = _parse_local(v["closes_at"], org["id"])
    except ValueError:
        errors.append("The deadline is not a valid date and time.")
    if closes is not None and closes <= datetime.now(timezone.utc):
        errors.append("The deadline must be in the future.")
    days = None
    if v["reminder_every_days"]:
        days = _int(v["reminder_every_days"])
        if days is None or not (1 <= days <= 90):
            errors.append("Reminders must be every 1 to 90 days (or blank for none).")
            days = None
    if v["test_mode"]:
        if not outreach.valid_email(v["test_address"]):
            errors.append("A test run needs a valid test address.")
        else:
            # A test run redirects EVERY email to one address, so that address must be internal:
            # at the admin's own email domain (a test run must never become a way to point many
            # emails from our sender addresses at an outside mailbox).
            mine = (user.get("email") or "").rsplit("@", 1)[-1].lower()
            theirs = v["test_address"].rsplit("@", 1)[-1].lower()
            if not mine or mine != theirs:
                errors.append(f"The test address must be an address at your own email domain (@{mine or '?'}).")

    selectors = _clean_selectors(selectors_raw)
    sel_errors = outreach.validate_selectors(selectors)
    errors += sel_errors
    if not sel_errors:
        errors += outreach.authorize_selectors(user["id"], org["id"], selectors, POLL)

    clean_questions = []
    try:
        clean_questions = POLL.normalize_questions(questions_raw if isinstance(questions_raw, list) else [])
    except outreach.OutreachError as e:
        errors += e.errors

    fields = {"title": v["title"], "subject": v["subject"] or v["title"], "intro": v["intro"],
              "sender_email": v["sender_email"], "closes_at": closes, "reminder_every_days": days,
              "allow_change": v["allow_change"], "test_mode": v["test_mode"],
              "test_address": v["test_address"] or None}
    return errors, {"fields": fields, "selectors": selectors, "questions": clean_questions}


async def _save_poll(request: Request, user, org, campaign=None):
    form = await request.form()
    v = _read_form(form)
    try:
        selectors_raw = json.loads(form.get("selectors_json") or "[]")
        questions_raw = json.loads(form.get("questions_json") or "[]")
    except ValueError:
        selectors_raw, questions_raw = [], []
    errors, parsed = _validate_form(user, org, v, selectors_raw, questions_raw)
    if errors:
        ctx = _form_context(request, user, org, campaign=campaign, values={**v, "reminder_every_days": v["reminder_every_days"]},
                            selectors=_clean_selectors(selectors_raw) or None,
                            questions=questions_raw if isinstance(questions_raw, list) and questions_raw else None,
                            errors=errors)
        return _render(request, "admin_outreach_form.html", user, ctx), None
    f = parsed["fields"]
    created_here = False
    try:
        if campaign is None:
            cid = outreach.create_campaign(
                kind="poll", title=f["title"], org_id=org["id"], created_by=user["id"],
                sender_email=f["sender_email"], subject=f["subject"], intro=f["intro"], closes_at=f["closes_at"],
                allow_change=f["allow_change"], reminder_every_days=f["reminder_every_days"],
                test_mode=f["test_mode"], test_address=f["test_address"])
            created_here = True
        else:
            cid = campaign["id"]
            outreach.update_campaign(cid, **f)
        outreach.set_audience(cid, parsed["selectors"])
        POLL.set_questions(cid, [{"qtype": qt, "prompt": p, "required": r, "config": cfg}
                                 for (qt, p, r, cfg) in parsed["questions"]])
        outreach.add_event(cid, "campaign_saved", detail={"by": user["id"], "new": created_here})
    except outreach.OutreachError as e:
        if created_here:
            outreach.delete_campaign(cid)  # never leave a half-saved draft behind
        ctx = _form_context(request, user, org, campaign=campaign, values=v,
                            selectors=parsed["selectors"], questions=questions_raw, errors=e.errors)
        return _render(request, "admin_outreach_form.html", user, ctx), None
    return None, cid


@router.get("/admin/polls/new", response_class=HTMLResponse)
def poll_new(request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    return _render(request, "admin_outreach_form.html", user, _form_context(request, user, org))


@router.post("/admin/polls/new", response_class=HTMLResponse)
async def poll_create(request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    page, cid = await _save_poll(request, user, org)
    return page if page is not None else _redirect(cid, saved=1)


@router.get("/admin/polls/{cid}/edit", response_class=HTMLResponse)
def poll_edit(cid: int, request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    c = _campaign(org, cid)
    if not c:
        return _not_found()
    if c["status"] != "draft":
        return _redirect(cid, error="Only a draft can be edited.")
    if _audience_errors(user, org, cid):
        # Editing would silently re-point selectors for entities this person cannot pick.
        return _redirect(cid, error=_AUDIENCE_REFUSAL)
    return _render(request, "admin_outreach_form.html", user, _form_context(request, user, org, campaign=c))


@router.post("/admin/polls/{cid}/edit", response_class=HTMLResponse)
async def poll_update(cid: int, request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    c = _campaign(org, cid)
    if not c:
        return _not_found()
    if c["status"] != "draft":
        return _redirect(cid, error="Only a draft can be edited.")
    if _audience_errors(user, org, cid):
        return _redirect(cid, error=_AUDIENCE_REFUSAL)
    page, _ = await _save_poll(request, user, org, campaign=c)
    return page if page is not None else _redirect(cid, saved=1)


# ---------------------------------------------------------------------------
# Live audience preview + parish picker (JSON)
# ---------------------------------------------------------------------------
@router.post("/admin/polls/audience-preview")
async def audience_preview(request: Request):
    user, org, err = _guard(request, as_json=True)
    if err:
        return err
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Bad request"}, status_code=400)
    selectors = _clean_selectors(body.get("selectors") if isinstance(body, dict) else None)
    errors = outreach.validate_selectors(selectors)
    if errors:
        return {"errors": errors, "count": None, "people": []}
    # Authorize BEFORE resolving: the preview must never reveal who holds roles at an entity
    # this person does not administer.
    errors = outreach.authorize_selectors(user["id"], org["id"], selectors, POLL)
    if errors:
        return {"errors": errors, "count": None, "people": []}
    people = outreach.resolve_selectors(selectors)
    return {"errors": [], "count": len(people),
            "people": [{"name": p["name"], "email": p["email"], "via": p["via"]} for p in people[:100]]}


@router.get("/admin/polls/parishes")
def parishes_for_org(request: Request, org_id: int):
    user, org, err = _guard(request, as_json=True)
    if err:
        return err
    if not rbac.user_has_any_role(user["id"], list(outreach.ADMIN_ROLES), org_id=org_id):
        return JSONResponse({"error": "Not a Beacon Admin at that entity"}, status_code=403)
    rows = db.query("SELECT id, name FROM portal.parishes WHERE org_id = %s AND is_active ORDER BY name", (org_id,))
    return {"parishes": [{"id": r["id"], "name": r["name"]} for r in rows]}


# ---------------------------------------------------------------------------
# Detail / results
# ---------------------------------------------------------------------------
_SEND_LABELS = {"sent": "Sent", "failed": "Email failed", "suppressed": "Not sent (suppressed)", "pending": "Not sent yet"}


def _row_status(r: dict) -> tuple[str, str]:
    if r["status"] == "excluded":
        return "excluded", "Excluded"
    if r["status"] == "responded":
        return "responded", "Responded"
    if r["send_status"] == "failed":
        return "failed", "Email failed"
    if r["send_status"] == "suppressed":
        return "suppressed", "Not sent (suppressed)"
    if r["send_status"] == "pending":
        return "unsent", "Not sent yet"
    return "awaiting", "Awaiting response"


@router.get("/admin/polls/{cid}", response_class=HTMLResponse)
def poll_detail(cid: int, request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    c = _campaign(org, cid)
    if not c:
        return _not_found()
    oid = org["id"]
    answers = POLL.answers_by_recipient(c)
    rows = []
    for r in outreach.recipients_report(cid):
        key, label = _row_status(r)
        via = (r.get("meta") or {}).get("via") or []
        rows.append({
            "id": r["id"], "name": r["name"], "email": r["email"], "via": "; ".join(via) or r["role_label"],
            "state": key, "state_label": label, "send_error": r["send_error"],
            "sent_text": _fmt(r["sent_at"], oid), "opened": r["first_open_at"] is not None,
            "opened_text": _fmt(r["first_open_at"], oid), "visited": r["first_click_at"] is not None,
            "visited_text": _fmt(r["first_click_at"], oid), "responded_text": _fmt(r["responded_at"], oid),
            "reminders": r["reminder_count"], "answers": answers.get(r["id"], []),
            "can_exclude": r["status"] == "pending",
        })
    events = [{"type": e["event_type"], "when": _fmt(e["occurred_at"], oid), "who": e["name"] or "",
               "email": e["email"] or "", "detail": e["detail"]} for e in outreach.recent_events(cid, 100)]
    counts = outreach.counts(cid)
    sent = counts["sent"]
    draft_errors = _audience_errors(user, org, cid) if c["status"] == "draft" else []
    questions = []
    for i, q in enumerate(POLL.questions(cid), start=1):
        qt = outreach_kinds.QUESTION_TYPES[q["qtype"]]
        cfg = q.get("config") or {}
        labels = [o["label"] for o in cfg.get("options", [])]
        if q["qtype"] == "yes_no":
            labels = [cfg.get("yes_label", "Yes"), cfg.get("no_label", "No")]
        questions.append({"n": i, "prompt": q["prompt"], "type_label": qt.label, "required": q["required"],
                          "options": labels, "max_select": cfg.get("max_select"), "max_length": cfg.get("max_length")})
    ctx = {
        "c": c, "counts": counts, "rows": rows, "events": events, "questions": questions,
        "summary": POLL.summary(c)["questions"],
        "deadline_text": _fmt(c["closes_at"], oid) or "No deadline",
        "created_text": _fmt(c["created_at"], oid), "sent_text": _fmt(c["sent_at"], oid),
        "rate": f"{round(100 * counts['responded'] / sent)}%" if sent else "—",
        "can_edit": c["status"] == "draft",
        # A draft shows who WOULD be asked today (the list is only frozen at Send) -- but only to
        # someone who administers every entity the audience reaches.
        "audience_refused": bool(c["status"] == "draft" and draft_errors),
        "preview": outreach.resolve_audience(cid) if c["status"] == "draft" and not draft_errors else [],
        "recipients_total": counts["total"],
        "flash": request.query_params.get("saved") and "Saved." or request.query_params.get("msg") or "",
        "error": request.query_params.get("error") or "",
    }
    return _render(request, "admin_outreach_detail.html", user, ctx)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
@router.post("/admin/polls/{cid}/send")
def poll_send(cid: int, request: Request):
    """draft -> sending: freezes the recipients and mints their links. The page then calls
    send-chunk until everything is out (resumable: reloading the page continues)."""
    user, org, err = _guard(request)
    if err:
        return err
    c = _campaign(org, cid)
    if not c:
        return _not_found()
    # Re-authorize at SEND time: the audience was checked when saved, but roles can change.
    errors = outreach.authorize_selectors(user["id"], org["id"], outreach.get_audience(cid), POLL)
    if errors:
        return _redirect(cid, error=" ".join(errors))
    try:
        info = outreach.start_sending(cid, by_user_id=user["id"])
    except outreach.OutreachError as e:
        return _redirect(cid, error=" ".join(e.errors))
    return _redirect(cid, msg=f"Frozen the list of {info['recipients']} recipients. Sending now.")


@router.post("/admin/polls/{cid}/send-chunk")
def poll_send_chunk(cid: int, request: Request, retry: int = 0):
    user, org, err = _guard(request, as_json=True)
    if err:
        return err
    if not _campaign(org, cid):
        return JSONResponse({"error": "Poll not found"}, status_code=404)
    try:
        return outreach.send_pending(cid, base_url=_base_url(org["id"], request), retry_failed=bool(retry))
    except outreach.OutreachError as e:
        return JSONResponse({"error": " ".join(e.errors)}, status_code=409)


@router.post("/admin/polls/{cid}/remind-chunk")
def poll_remind_chunk(cid: int, request: Request):
    """The manual 'remind non-responders' button: ignores the cadence and the pause switch,
    never re-reminds anyone reminded in the last hour. Returns {sent, failed}: the page keeps
    calling while sent > 0."""
    user, org, err = _guard(request, as_json=True)
    if err:
        return err
    c = _campaign(org, cid)
    if not c:
        return JSONResponse({"error": "Poll not found"}, status_code=404)
    if c["status"] != "open":
        return JSONResponse({"error": "Only an open poll can send reminders."}, status_code=409)
    res = outreach.send_reminders(cid, base_url=_base_url(org["id"], request), only_due=False)
    if res["sent"] or res["failed"]:
        outreach.add_event(cid, "reminder_batch", detail={"by": user["id"], **res})
    return res


@router.post("/admin/polls/{cid}/reminders")
async def poll_reminder_settings(cid: int, request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    c = _campaign(org, cid)
    if not c:
        return _not_found()
    form = await request.form()
    days = _int(form.get("reminder_every_days")) if (form.get("reminder_every_days") or "").strip() else None
    if days is not None and not (1 <= days <= 90):
        return _redirect(cid, error="Reminders must be every 1 to 90 days (or blank for none).")
    try:
        outreach.update_campaign(cid, reminder_every_days=days, reminders_paused=form.get("reminders_paused") is not None)
    except outreach.OutreachError as e:
        return _redirect(cid, error=" ".join(e.errors))
    outreach.add_event(cid, "reminder_settings", detail={"by": user["id"], "days": days,
                                                         "paused": form.get("reminders_paused") is not None})
    return _redirect(cid, msg="Reminder settings saved.")


@router.post("/admin/polls/{cid}/close")
def poll_close(cid: int, request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    if not _campaign(org, cid):
        return _not_found()
    ok = outreach.close_campaign(cid, user["id"])
    return _redirect(cid, **({"msg": "Closed. No more responses will be accepted."} if ok else {"error": "It is not open."}))


@router.post("/admin/polls/{cid}/cancel")
def poll_cancel(cid: int, request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    if not _campaign(org, cid):
        return _not_found()
    ok = outreach.cancel_campaign(cid, user["id"])
    return _redirect(cid, **({"msg": "Cancelled. Anyone who opens their link will be told it is no longer needed."}
                             if ok else {"error": "It cannot be cancelled now."}))


@router.post("/admin/polls/{cid}/delete")
def poll_delete(cid: int, request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    if not _campaign(org, cid):
        return _not_found()
    if outreach.delete_campaign(cid):
        return RedirectResponse("/admin/polls?deleted=1", status_code=303)
    return _redirect(cid, error="Only a draft or a test poll can be deleted. A real poll is closed or cancelled, never erased.")


@router.post("/admin/polls/{cid}/exclude/{rid}")
def poll_exclude(cid: int, rid: int, request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    if not _campaign(org, cid):
        return _not_found()
    ok = outreach.exclude_recipient(cid, rid, user["id"], "removed by admin")
    return _redirect(cid, **({"msg": "Removed from this poll."} if ok else {"error": "That person has already responded or was removed."}))


@router.get("/admin/polls/{cid}/export.csv")
def poll_export(cid: int, request: Request):
    user, org, err = _guard(request)
    if err:
        return err
    c = _campaign(org, cid)
    if not c:
        return _not_found()
    headers, rows = POLL.export_rows(c)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([_csv_safe(h) for h in headers])
    for r in rows:
        w.writerow([_csv_safe(x) for x in r])
    outreach.add_event(cid, "results_exported", detail={"by": user["id"]})
    return Response(content="﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="poll-{cid}-results.csv"',
                             "Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Cloud Scheduler: reminders + sweep of expired polls
# ---------------------------------------------------------------------------
@router.post("/internal/send-outreach-reminders")
def send_outreach_reminders(request: Request):
    """Machine-to-machine, gated by the shared X-Internal-Key header exactly like
    /internal/send-daily-digest and /internal/sync-vendors-hourly. NOT scheduled yet: creating
    the Cloud Scheduler job needs Jay's gcloud identity (see 26-156 CLAUDE.md for the command)."""
    supplied = request.headers.get("x-internal-key", "")
    expected = _get_internal_key() if _get_internal_key else ""
    if not supplied or not expected or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    if not outreach.tables_ready():
        return JSONResponse({"error": "not set up"}, status_code=503)
    closed = outreach.close_expired()
    summary = []
    for cid in outreach.campaigns_due_for_reminders():
        c = outreach.get_campaign(cid)
        base = _base_url(c["org_id"], request)
        sent = failed = 0
        for _ in range(10):  # bounded: a failed email is retried on the next run, never in a tight loop
            r = outreach.send_reminders(cid, base_url=base, only_due=True)
            sent += r["sent"]
            failed += r["failed"]
            if r["sent"] == 0:
                break
        if sent or failed:
            outreach.add_event(cid, "reminder_batch", detail={"by": "scheduler", "sent": sent, "failed": failed})
        summary.append({"campaign_id": cid, "sent": sent, "failed": failed})
    return {"closed_expired": closed, "campaigns": summary}
