"""
outreach_mine.py -- 26-156: "My Polls", the signed-in person's own side of the shared Email
Response Engine. A person who answered a poll from the emailed link can sign in and see, in one
place, the polls they were asked, what they answered, and anything still waiting.

    GET /my-polls      this person's polls: waiting for an answer / answered / no longer open

WHO SEES WHAT. Only rows addressed to the signed-in person themselves
(outreach_recipients.user_id = their own id, taken from the session, never from the request),
across every entity, each labelled with its entity. A recipient with no Beacon account (a typed-in
list, a future SMA signer) is not listed: their private email link stays their only way back.
Never shown: test runs (their emails went to the sender's test address, so the real recipients
were never asked), drafts, cancelled polls, people the admin took off the list, and polls whose
email was never actually sent to them (still queued, failed, or suppressed) unless they answered.
Polls only (kind 'poll'); other kinds will have their own screens.

LINKS. A waiting poll links to its private /respond/{token} page, and an answered one to the
change form. While an admin is IMPERSONATING the person the page is view-only (no answer or change
link, so an impersonation can never put words in someone's mouth) and shows only the entities the
real admin is a Beacon Admin at (the page promises answers are visible to the person and the
administrators who sent the poll, not to any admin who can switch entities).

LIMITS. Every poll still waiting is always shown. Answered / no-longer-open ones are capped at
MAX_ROWS (newest first) with a note.

NAV. A "My Polls" item appears in the user menu only for people who have at least one poll,
with a count of those still waiting. That one query runs on every signed-in page, so it is cached
for NAV_TTL seconds per person (the page itself drops its own cache entry, so the two agree on the
same render), and it can never break a page: any failure means "no item".

New file per the standing rule: main.py only imports this and calls register().
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

import db
import org_time
import outreach
import outreach_kinds  # noqa: F401  -- importing registers the 'poll' kind
import rbac

router = APIRouter()
_current_user = None
_render = None

MAX_ROWS = 50          # answered / no-longer-open polls shown (waiting ones are never capped)
NAV_TTL = 60           # seconds the user-menu count may lag (the page itself is always live)

_PRIVATE_HEADERS = {   # the page carries private answer links, so nothing may cache or leak it
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
}

# The ONE definition of "a poll that belongs on this person's list" (page and menu count share it).
_VISIBLE = ("r.user_id = %s AND c.kind = 'poll' AND NOT c.test_mode "
            "AND c.status IN ('sending', 'open', 'closed') "
            "AND r.status <> 'excluded' "
            "AND (r.send_status = 'sent' OR r.status = 'responded')")

_nav_cache: dict[int, tuple[float, dict]] = {}


def register(app, *, templates, current_user, render) -> None:
    global _current_user, _render
    _current_user, _render = current_user, render
    templates.env.globals["my_polls_nav"] = nav_summary     # used by base.html's user menu
    app.include_router(router)


def _when(dt, org_id, zones: dict) -> str:
    """A time as the poll's own entity writes it. `zones` memoizes the entity's zone (one lookup per
    entity per page instead of one per date)."""
    if not dt:
        return ""
    try:
        zone = zones.get(org_id)
        if zone is None:
            zone = zones[org_id] = org_time.zone_name_for_org(org_id)
        return org_time.format_local(dt, zone)
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------
def polls_for_user(user_id: int, *, org_ids=None, limit: int = MAX_ROWS) -> tuple[list[dict], bool]:
    """(items, truncated). Each item is classified:
         waiting           open, not answered yet                       (never capped)
         answered          they answered (open, closed or link expired)
         answered_by_other any_in_group poll answered by someone else in their group
         missed            no longer open and no answer was recorded (closed, or the link expired)
       org_ids: restrict to these entities (used while an admin impersonates). truncated: older
       answered / no-longer-open polls were left off (limit)."""
    params: list = [user_id]
    org_clause = ""
    if org_ids is not None:
        org_clause = " AND c.org_id = ANY(%s)"
        params.append(list(org_ids))
    rows = db.query(
        "SELECT r.id AS rid, r.campaign_id, r.group_key, r.token, r.status AS rstatus, r.responded_at, "
        "       c.title, c.org_id, c.status AS cstatus, c.closes_at, c.closed_at, c.allow_change, "
        "       c.completion_rule, c.sent_at AS c_sent_at, o.name AS org_name, "
        "       (c.closes_at IS NOT NULL AND NOW() > c.closes_at) AS past_close, "
        "       (r.token_expires_at IS NOT NULL AND NOW() > r.token_expires_at) AS token_expired, "
        "       (SELECT max(e.occurred_at) FROM portal.outreach_events e WHERE e.recipient_id = r.id "
        "         AND e.event_type IN ('responded', 'response_changed')) AS last_resp_at "
        "FROM portal.outreach_recipients r "
        "JOIN portal.outreach_campaigns c ON c.id = r.campaign_id "
        "JOIN checkreq.organizations o ON o.id = c.org_id "
        f"WHERE {_VISIBLE}{org_clause} ORDER BY c.sent_at DESC NULLS LAST, r.id DESC",
        tuple(params))

    # Phase 1: classify every row (cheap), so the cap can spare the waiting ones.
    classified = []
    for row in rows:
        # The engine's own rule for what a link may still do (closed / expired / active ...).
        state = outreach._state({"status": row["cstatus"]}, {"status": row["rstatus"]}, row)
        responded = row["rstatus"] == "responded"
        camp = {"id": row["campaign_id"], "kind": "poll", "title": row["title"], "org_id": row["org_id"],
                "completion_rule": row["completion_rule"]}
        rec = {"id": row["rid"], "group_key": row["group_key"]}
        other = None if responded else outreach.group_responder(camp, rec)
        if responded:
            group = "answered"
        elif other:
            group = "answered_by_other"
        elif state == "active":
            group = "waiting"
        else:
            group = "missed"
        classified.append((row, state, group, camp, rec, other))
    waiting = [c for c in classified if c[2] == "waiting"]
    rest = [c for c in classified if c[2] != "waiting"]
    truncated = len(rest) > limit
    kept = waiting + rest[:limit]

    # Phase 2: the per-row work (answers, formatted times) only for what is shown.
    kind = outreach.get_kind("poll")
    zones: dict = {}
    out = []
    for row, state, group, camp, rec, other in kept:
        is_open = state == "active"
        closed_when = row["closes_at"] if (row["past_close"] and row["closes_at"]) else row["closed_at"]
        org = row["org_id"]
        out.append({
            "title": row["title"], "entity": row["org_name"], "group": group, "token": row["token"],
            "is_open": is_open, "expired": state == "expired",
            "can_answer": group == "waiting",
            "can_change": group == "answered" and is_open and bool(row["allow_change"]),
            "final": group == "answered" and not row["allow_change"],
            "answers": kind.received_answers(camp, rec) if group == "answered" else [],
            "closes_at": row["closes_at"],
            "sent_text": _when(row["c_sent_at"], org, zones),
            "closes_text": _when(row["closes_at"], org, zones),
            "closed_text": _when(closed_when, org, zones),
            "answered_text": _when(row["last_resp_at"] or row["responded_at"], org, zones) if group == "answered" else "",
            "by": ({"name": other["name"], "when": _when(other["responded_at"], org, zones)} if other else None),
        })
    return out, truncated


@router.get("/my-polls")
def my_polls_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login")
    _nav_cache.pop(user["id"], None)      # so the menu on this very page agrees with the list below
    # Impersonating = the identity acting on this page is not the person who signed in. (Not the raw
    # session key: that can linger when the signed-in person is not a CFO, and _current_user then
    # simply returns their own account.)
    real_id = request.session.get("user_id")
    impersonating = real_id is not None and int(real_id) != int(user["id"])
    org_ids = rbac.get_granted_org_ids(int(real_id), "beacon_admin") if impersonating else None
    ready = outreach.tables_ready()
    items, truncated, load_failed = [], False, False
    if ready:
        try:
            items, truncated = polls_for_user(user["id"], org_ids=org_ids)
        except Exception as exc:
            print(f"[outreach_mine] could not load polls for user {user['id']}: {exc!r}")
            load_failed = True
    waiting = sorted((i for i in items if i["group"] == "waiting"),
                     key=lambda i: (i["closes_at"] is None, i["closes_at"] or 0))
    answered = [i for i in items if i["group"] in ("answered", "answered_by_other")]
    missed = [i for i in items if i["group"] == "missed"]
    resp = _render(request, "my_polls.html", user, {
        "waiting": waiting, "answered": answered, "missed": missed, "total": len(items),
        "read_only": impersonating, "limit": MAX_ROWS, "truncated": truncated,
        "ready": ready, "load_failed": load_failed})
    for k, v in _PRIVATE_HEADERS.items():
        resp.headers[k] = v
    return resp


# ---------------------------------------------------------------------------
# The user-menu entry
# ---------------------------------------------------------------------------
def nav_summary(user) -> dict:
    """{'total': polls on this person's list, 'waiting': those still waiting for an answer}. Used by
    base.html on every signed-in page: cached per person for NAV_TTL seconds and never raises."""
    empty = {"total": 0, "waiting": 0}
    try:
        uid = int(user["id"])
    except Exception:
        return empty
    hit = _nav_cache.get(uid)
    now = time.monotonic()
    if hit and now - hit[0] < NAV_TTL:
        return hit[1]
    result = empty
    try:
        if outreach.tables_ready():
            row = db.query_one(
                "SELECT count(*) AS total, count(*) FILTER (WHERE c.status IN ('sending', 'open') "
                "  AND r.status = 'pending' "
                "  AND NOT (c.closes_at IS NOT NULL AND NOW() > c.closes_at) "
                "  AND NOT (r.token_expires_at IS NOT NULL AND NOW() > r.token_expires_at) "
                "  AND NOT (c.completion_rule = 'any_in_group' AND COALESCE(r.group_key, '') <> '' AND EXISTS ("
                "        SELECT 1 FROM portal.outreach_recipients o WHERE o.campaign_id = r.campaign_id "
                "        AND o.group_key = r.group_key AND o.status = 'responded' AND o.id <> r.id))) AS waiting "
                "FROM portal.outreach_recipients r JOIN portal.outreach_campaigns c ON c.id = r.campaign_id "
                f"WHERE {_VISIBLE}", (uid,))
            result = {"total": int(row["total"]), "waiting": int(row["waiting"])}
    except Exception as exc:        # a menu entry must never take a page down
        print(f"[outreach_mine] menu count unavailable: {exc!r}")
    if len(_nav_cache) > 5000:
        _nav_cache.clear()
    _nav_cache[uid] = (now, result)
    return result
