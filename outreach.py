"""
outreach.py -- 26-156: the shared Email Response Engine.

One engine for "email people a private link, track what happens, remind the ones
who have not answered": campaigns have recipients, each recipient has a token, the
engine sends, records open signals / link visits / responses, sends reminders and
keeps an append-only event log. A *kind* (outreach_kinds.py) plugs in what the
email says and what the response page does -- 'poll' now, the SMA letters
(26-129 plan rev 11) later. Design: "26-156 Beacon Role Polling\\Plan.md".

New file per the standing rule (nothing new in main.py). No HTTP in here: the
public routes are outreach_public.py, the admin screens outreach_admin.py.

Honest tracking semantics (also stated in the admin UI):
  * A response is the only hard fact.
  * An "open" is a best-effort signal: Outlook / Apple Mail block or proxy the
    pixel, and M365 link scanning fetches links. Link visits are recorded
    separately from opens.
  * Non-response = no response by the deadline.
  * Delivery / bounce is NOT visible: Graph's sendMail returns nothing, and the
    26-122 email server returns only {"status": "sent"}.

Authorization rule (2026-10-05 cross-diocese lesson): audience selectors are
always (role, ONE entity). The creator must hold Beacon Admin at every entity
selected. Nothing here ever passes org_id=None to a role check.
"""
from __future__ import annotations

import html as _html
import re
import secrets

from psycopg.types.json import Jsonb

import db
import email_client
import parish_roles
import rbac

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ADMIN_ROLES = ("beacon_admin",)  # who may create / send / read polls (Plan.md section 8, Q1)

# The 26-122 Cloud Email Server only accepts these two From addresses
# (26-122 Cloud Email Server\sender_registry.py). Same list and same default
# rule as report_template_editor.default_sender -- duplicated here, not
# imported, so this module never pulls in the whole report-template editor.
SENDERS = ("businessoffice@episcopalmaryland.org", "notifications@cfmins.org")
_EDOM_FAMILY = {"EDOM", "CLAGGETT"}

SEND_CHUNK = 40                 # emails per send_pending() call (a 300 s Cloud Run request must never hold a whole send)
DEFAULT_TOKEN_DAYS = 90         # token lifetime when the campaign has no deadline
TOKEN_GRACE_DAYS = 14           # token lifetime after the deadline
MAX_LIST_ENTRIES = 2000
_LOCK_NS = 7156                 # advisory-lock namespace for "this campaign is being sent right now"
_LOCK_NS_REMIND = 7157          # ... and for "this campaign's reminders are being sent right now"
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")

SOURCES = ("entity_role", "parish_role", "list")
COMPLETION_RULES = ("each", "any_in_group")


class OutreachError(Exception):
    """A refusal the caller can show: .errors is a list of plain-English messages."""

    def __init__(self, errors):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


# ---------------------------------------------------------------------------
# Kind registry. A kind decides what a campaign's email says and what its
# response page does. The engine never knows about polls or letters.
# ---------------------------------------------------------------------------
class Kind:
    key = ""
    label = ""
    template = ""                 # included by respond.html for the active/response state
    allow_list_audience = False   # may a campaign of this kind take an explicit recipient list?

    def validate_ready(self, campaign: dict) -> list[str]:
        """Plain-English reasons the campaign cannot start sending yet."""
        return []

    def email_parts(self, campaign: dict, recipient: dict, urls: dict) -> dict:
        """{"headline": str, "body_html": str (already escaped), "body_text": str,
            "buttons": [{"label": str, "url": str, "primary": bool}]}"""
        raise NotImplementedError

    def page_context(self, campaign: dict, recipient: dict, preselect: str | None,
                     submitted: dict | None = None) -> dict:
        """Context for the response form. preselect = the ?a= choice from an email button;
        submitted = the form values from a POST that failed validation (shown back to the person)."""
        return {}

    def parse_response(self, campaign: dict, recipient: dict, form: dict):
        """-> (parsed, errors). form is {field: [values]}. Pure: no writes."""
        raise NotImplementedError

    def save_response(self, cur, campaign: dict, recipient: dict, parsed) -> dict:
        """Write the parsed response inside the engine's transaction (cur).
        Return the event detail. {"unchanged": True} suppresses the event on a re-submit."""
        raise NotImplementedError

    def summary(self, campaign: dict) -> dict:
        return {}

    def quick_answer(self, campaign: dict, recipient: dict, choice: str) -> dict | None:
        """The form fields ({name: [values]}) that record a ONE-TAP answer for an email-button
        choice, or None when this campaign has no such thing (e.g. several questions). The public
        page then records it from its own script, so a link scanner that merely fetches the page
        records nothing."""
        return None

    def received_answers(self, campaign: dict, recipient: dict) -> list[tuple[str, str]]:
        """[(label, text), ...]: what this person answered, shown back to them after they answer."""
        return []

    def answers_by_recipient(self, campaign: dict) -> dict:
        """{recipient_id: [(label, text), ...]} for the admin results table."""
        return {}

    def export_rows(self, campaign: dict) -> tuple[list[str], list[list[str]]]:
        """(headers, rows) for the results CSV."""
        return [], []


KINDS: dict[str, Kind] = {}


def register_kind(kind: Kind) -> None:
    KINDS[kind.key] = kind


def get_kind(key: str) -> Kind:
    if key not in KINDS:
        raise OutreachError(f"Unknown campaign kind '{key}'.")
    return KINDS[key]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def esc(value) -> str:
    return _html.escape("" if value is None else str(value), quote=True)


def text_to_html(text: str) -> str:
    """Plain admin-typed text -> escaped HTML paragraphs."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    return "".join(f'<p style="margin:0 0 12px 0;">{esc(p).replace(chr(10), "<br>")}</p>' for p in paras)


def valid_email(addr: str) -> bool:
    return bool(addr) and bool(_EMAIL_RE.match(addr.strip()))


def default_sender(org_code: str) -> str:
    """EDOM and Claggett send as the diocesan business office, everyone else as
    notifications@cfmins.org -- the same rule as report templates."""
    return SENDERS[0] if str(org_code or "").upper() in _EDOM_FAMILY else SENDERS[1]


def tables_ready() -> bool:
    """False until migration 073 is applied, so screens can say 'not set up yet'
    instead of raising."""
    try:
        row = db.query_one("SELECT to_regclass('portal.outreach_campaigns') IS NOT NULL AS ok")
        return bool(row and row["ok"])
    except Exception:
        return False


EVENT_CAP = 50   # per recipient: how many open-signal / link-visit EVENT rows are kept (the counters keep counting)


def strip_nul(value):
    """Postgres text and jsonb refuse the NUL character: one in a typed answer or a query value would
    be an unhandled 500 and roll the whole write back. Removed at every boundary that takes outside text."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [strip_nul(v) for v in value]
    return value


def _event(cur, campaign_id: int, recipient_id, event_type: str, *, ip=None, ua=None, detail=None) -> None:
    cur.execute(
        "INSERT INTO portal.outreach_events (campaign_id, recipient_id, event_type, ip, user_agent, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (campaign_id, recipient_id, event_type, strip_nul(ip or None), strip_nul((ua or "")[:300] or None),
         Jsonb(strip_nul(detail or {}))),
    )


def add_event(campaign_id: int, event_type: str, *, recipient_id: int | None = None, detail: dict | None = None) -> None:
    """Public writer for events the admin screens record (edited, reminder batch, ...)."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            _event(cur, campaign_id, recipient_id, event_type, detail=detail)


def respond_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/respond/{token}"


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------
def get_campaign(campaign_id: int) -> dict | None:
    return db.query_one("SELECT * FROM portal.outreach_campaigns WHERE id = %s", (campaign_id,))


def create_campaign(*, kind: str, title: str, org_id: int, created_by: int | None,
                    sender_email: str | None = None, subject: str | None = None,
                    intro: str = "", closes_at=None, completion_rule: str = "each",
                    allow_change: bool = True, reminder_every_days: int | None = None,
                    test_mode: bool = False, test_address: str | None = None,
                    config: dict | None = None) -> int:
    get_kind(kind)
    errors = []
    title = (title or "").strip()
    if not title:
        errors.append("A title is required.")
    org = db.query_one("SELECT id, code FROM checkreq.organizations WHERE id = %s", (org_id,))
    if not org:
        errors.append("Unknown entity.")
    sender = (sender_email or "").strip() or (default_sender(org["code"]) if org else SENDERS[1])
    if sender not in SENDERS:
        errors.append("The From address must be one of: " + ", ".join(SENDERS) + ".")
    if completion_rule not in COMPLETION_RULES:
        errors.append("Unknown completion rule.")
    if reminder_every_days is not None and not (1 <= int(reminder_every_days) <= 90):
        errors.append("Reminders must be every 1 to 90 days.")
    if test_mode and not valid_email(test_address or ""):
        errors.append("A test run needs a valid test address.")
    if errors:
        raise OutreachError(errors)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.outreach_campaigns (kind, title, org_id, sender_email, subject, intro, "
                "closes_at, completion_rule, allow_change, reminder_every_days, test_mode, test_address, "
                "config, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (kind, title, org_id, sender, (subject or title).strip(), intro or "", closes_at,
                 completion_rule, allow_change, reminder_every_days, test_mode,
                 (test_address or "").strip() or None, Jsonb(config or {}), created_by),
            )
            cid = cur.fetchone()["id"]
            _event(cur, cid, None, "campaign_created", detail={"by": created_by})
    return cid


def update_campaign(campaign_id: int, **fields) -> None:
    """Edit a DRAFT campaign. Only the listed columns are accepted."""
    allowed = {"title", "subject", "intro", "closes_at", "completion_rule", "allow_change",
               "reminder_every_days", "reminders_paused", "test_mode", "test_address", "sender_email"}
    c = get_campaign(campaign_id)
    if not c:
        raise OutreachError("Campaign not found.")
    if c["status"] != "draft" and set(fields) - {"reminders_paused", "reminder_every_days", "closes_at"}:
        raise OutreachError("Only a draft can be edited (reminders and the deadline can still change).")
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            raise OutreachError(f"'{k}' cannot be edited.")
        if k == "sender_email" and v not in SENDERS:
            raise OutreachError("The From address must be one of: " + ", ".join(SENDERS) + ".")
        sets.append(f"{k} = %s")
        params.append(v)
    if not sets:
        return
    sets.append("updated_at = NOW()")
    params.append(campaign_id)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE portal.outreach_campaigns SET {', '.join(sets)} WHERE id = %s", tuple(params))


def list_campaigns(org_ids: list[int], kind: str | None = None) -> list[dict]:
    return db.query(
        "SELECT c.*, "
        "  (SELECT count(*) FROM portal.outreach_recipients r WHERE r.campaign_id = c.id) AS n_recipients, "
        "  (SELECT count(*) FROM portal.outreach_recipients r WHERE r.campaign_id = c.id AND r.status = 'responded') AS n_responded "
        "FROM portal.outreach_campaigns c "
        "WHERE c.org_id = ANY(%s) AND (%s::text IS NULL OR c.kind = %s) "
        "ORDER BY c.created_at DESC",
        (list(org_ids), kind, kind),
    )


# ---------------------------------------------------------------------------
# Audience
# ---------------------------------------------------------------------------
def validate_selectors(selectors: list[dict]) -> list[str]:
    errors = []
    if not selectors:
        return ["Choose at least one audience."]
    for i, s in enumerate(selectors, start=1):
        tag = f"Audience {i}: "
        src = s.get("source")
        if src not in SOURCES:
            errors.append(tag + "unknown source.")
            continue
        if src == "list":
            entries = s.get("entries")
            if not isinstance(entries, list) or not entries:
                errors.append(tag + "the list is empty.")
                continue
            if len(entries) > MAX_LIST_ENTRIES:
                errors.append(tag + f"the list is longer than {MAX_LIST_ENTRIES}.")
            for e in entries:
                if not isinstance(e, dict) or not valid_email(e.get("email", "")):
                    errors.append(tag + f"invalid email '{(e or {}).get('email', '') if isinstance(e, dict) else e}'.")
                    break
            continue
        if not isinstance(s.get("org_id"), int):
            errors.append(tag + "choose an entity.")
            continue
        if not db.query_one("SELECT 1 FROM checkreq.organizations WHERE id = %s", (s["org_id"],)):
            errors.append(tag + "unknown entity.")
            continue
        rk = s.get("role_key")
        if not rk:
            errors.append(tag + "choose a role.")
            continue
        if src == "entity_role":
            if not db.query_one("SELECT 1 FROM checkreq.roles WHERE key = %s AND is_active", (rk,)):
                errors.append(tag + f"unknown role '{rk}'.")
        else:
            if not db.query_one("SELECT 1 FROM portal.parish_roles WHERE key = %s AND is_active", (rk,)):
                errors.append(tag + f"unknown parish role '{rk}'.")
            if s.get("scope", "org") == "parish":
                p = db.query_one("SELECT org_id FROM portal.parishes WHERE id = %s", (s.get("parish_id"),))
                if not p or p["org_id"] != s["org_id"]:
                    errors.append(tag + "that parish does not belong to the chosen entity.")
    return errors


def authorize_selectors(user_id: int, campaign_org_id: int, selectors: list[dict], kind: Kind) -> list[str]:
    """The creator must be a Beacon Admin at the campaign's entity AND at every
    entity a selector reaches. org_id is never None here."""
    errors = []
    if not rbac.user_has_any_role(user_id, list(ADMIN_ROLES), org_id=campaign_org_id):
        errors.append("You must be a Beacon Admin at this entity to send a poll from it.")
    for s in selectors:
        if s.get("source") == "list":
            if not kind.allow_list_audience:
                errors.append(f"A '{kind.label or kind.key}' campaign cannot use a typed-in list.")
            continue
        org_id = s.get("org_id")
        if not isinstance(org_id, int):
            errors.append("An audience has no entity.")
            continue
        if not rbac.user_has_any_role(user_id, list(ADMIN_ROLES), org_id=org_id):
            o = db.query_one("SELECT code FROM checkreq.organizations WHERE id = %s", (org_id,))
            errors.append(f"You are not a Beacon Admin at {o['code'] if o else org_id}, so you cannot poll its role-holders.")
    return errors


def set_audience(campaign_id: int, selectors: list[dict]) -> None:
    """Replace a DRAFT campaign's audience selectors."""
    c = get_campaign(campaign_id)
    if not c:
        raise OutreachError("Campaign not found.")
    if c["status"] != "draft":
        raise OutreachError("The audience can only change while the campaign is a draft.")
    errors = validate_selectors(selectors)
    if errors:
        raise OutreachError(errors)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM portal.outreach_audiences WHERE campaign_id = %s", (campaign_id,))
            for s in selectors:
                cur.execute(
                    "INSERT INTO portal.outreach_audiences (campaign_id, source, role_key, org_id, parish_id, scope, entries) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (campaign_id, s["source"], s.get("role_key"), s.get("org_id"), s.get("parish_id"),
                     s.get("scope", "org"), Jsonb(s["entries"]) if s.get("entries") is not None else None),
                )


def get_audience(campaign_id: int) -> list[dict]:
    return db.query("SELECT * FROM portal.outreach_audiences WHERE campaign_id = %s ORDER BY id", (campaign_id,))


def resolve_selectors(selectors: list[dict]) -> list[dict]:
    """Selectors -> de-duplicated people: [{email, name, role_label, user_id, group_key, via: [..]}].
    Live: used for the preview and, at send time, for the frozen snapshot."""
    people: dict[tuple, dict] = {}

    def add(email, name, user_id, group_key, role_label, via):
        email = (email or "").strip()
        if not valid_email(email):
            return
        key = (email.lower(), group_key or "")
        p = people.get(key)
        if p is None:
            people[key] = {"email": email, "name": name or email, "user_id": user_id,
                           "group_key": group_key, "role_label": role_label or "", "via": [via]}
        elif via not in p["via"]:
            p["via"].append(via)

    for s in selectors:
        src = s["source"]
        if src == "list":
            for e in s["entries"]:
                add(e["email"], e.get("name"), None, e.get("group_key"), e.get("role_label", ""),
                    "typed-in list")
            continue
        org_id = s["org_id"]
        if org_id is None:
            raise OutreachError("An audience has no entity.")  # never "any entity"
        org = db.query_one("SELECT code FROM checkreq.organizations WHERE id = %s", (org_id,)) or {}
        if src == "entity_role":
            role = db.query_one("SELECT label FROM checkreq.roles WHERE key = %s", (s["role_key"],)) or {}
            label = f"{role.get('label', s['role_key'])} at {org.get('code', org_id)}"
            for u in rbac.get_users_with_role(s["role_key"], org_id):
                add(u["email"], u["display_name"], u["id"], None, role.get("label", ""), label)
        else:
            role = db.query_one("SELECT label FROM portal.parish_roles WHERE key = %s", (s["role_key"],)) or {}
            if s.get("scope", "org") == "parish":
                parish = db.query_one("SELECT name FROM portal.parishes WHERE id = %s", (s["parish_id"],)) or {}
                for u in parish_roles.get_users_with_parish_role(s["role_key"], s["parish_id"]):
                    add(u["email"], u["display_name"], u["id"], None, role.get("label", ""),
                        f"{role.get('label', s['role_key'])} at {parish.get('name', s['parish_id'])}")
            else:
                rows = db.query(
                    "SELECT DISTINCT u.id, u.email, u.display_name, p.name AS parish_name "
                    "FROM portal.parish_user_roles pur "
                    "JOIN checkreq.app_users u ON u.id = pur.user_id AND u.is_active "
                    "JOIN portal.parish_roles pr ON pr.key = pur.role_key AND pr.is_active "
                    "JOIN portal.parishes p ON p.id = pur.parish_id AND p.is_active "
                    "WHERE pur.role_key = %s AND pur.revoked_at IS NULL AND p.org_id = %s "
                    "ORDER BY u.display_name, u.email, p.name",
                    (s["role_key"], org_id),
                )
                for r in rows:
                    add(r["email"], r["display_name"], r["id"], None, role.get("label", ""),
                        f"{role.get('label', s['role_key'])} at {r['parish_name']}")
    return sorted(people.values(), key=lambda p: (p["name"].lower(), p["email"].lower()))


def resolve_audience(campaign_id: int) -> list[dict]:
    return resolve_selectors(get_audience(campaign_id))


# ---------------------------------------------------------------------------
# Start sending: freeze the recipients and mint their tokens
# ---------------------------------------------------------------------------
def start_sending(campaign_id: int, *, by_user_id: int | None) -> dict:
    """draft -> sending. Snapshots the recipients (a later grant or revoke does not
    change who was asked) and mints one private token each. No email goes out here:
    send_pending() does that in resumable chunks."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM portal.outreach_campaigns WHERE id = %s FOR UPDATE", (campaign_id,))
            c = cur.fetchone()
            if not c:
                raise OutreachError("Campaign not found.")
            if c["status"] != "draft":
                raise OutreachError(f"This campaign is already {c['status']}.")
            kind = get_kind(c["kind"])
            errors = list(kind.validate_ready(c))
            if c["test_mode"] and not valid_email(c["test_address"] or ""):
                errors.append("A test run needs a valid test address.")
            people = resolve_audience(campaign_id)
            if not people:
                errors.append("The chosen audience has no one in it.")
            if c["closes_at"] is not None:
                # The deadline was only checked when the draft was saved: a draft can sit for days.
                cur.execute("SELECT NOW() > %s::timestamptz AS past", (c["closes_at"],))
                if cur.fetchone()["past"]:
                    errors.append("The deadline has already passed. Edit the poll to set a new deadline "
                                  "(or none) before sending.")
            if errors:
                raise OutreachError(errors)
            cur.execute(
                "SELECT (%s::timestamptz + make_interval(days => %s)) AS exp, "
                "(NOW() + make_interval(days => %s)) AS exp_default",
                (c["closes_at"], TOKEN_GRACE_DAYS, DEFAULT_TOKEN_DAYS),
            )
            exp_row = cur.fetchone()
            token_expires = exp_row["exp"] if c["closes_at"] else exp_row["exp_default"]
            n = 0
            for p in people:
                cur.execute(
                    "INSERT INTO portal.outreach_recipients (campaign_id, group_key, user_id, email, name, "
                    "role_label, token, token_expires_at, meta) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING RETURNING id",
                    (campaign_id, p["group_key"], p["user_id"], p["email"], p["name"], p["role_label"],
                     secrets.token_urlsafe(32), token_expires, Jsonb({"via": p["via"]})),
                )
                row = cur.fetchone()
                if row:
                    n += 1
                    _event(cur, campaign_id, row["id"], "queued")
            cur.execute(
                "UPDATE portal.outreach_campaigns SET status = 'sending', sent_at = NOW(), updated_at = NOW() "
                "WHERE id = %s", (campaign_id,))
            _event(cur, campaign_id, None, "campaign_started", detail={"by": by_user_id, "recipients": n})
    return {"recipients": n}


# ---------------------------------------------------------------------------
# Email building and sending
# ---------------------------------------------------------------------------
def build_email(campaign: dict, rec: dict, parts: dict, urls: dict, *, reminder: bool = False) -> tuple[str, str]:
    """Shared wrapper: header band, body, STACKED full-width buttons (inline buttons
    collided on an iPhone -- see main._email_action_buttons_html), why-you-got-this
    footer, open pixel. Returns (html, text)."""
    via = (rec.get("meta") or {}).get("via") or []
    why = ("You are receiving this because you are: " + "; ".join(via) + ".") if via else ""
    rows = []
    for b in parts.get("buttons", []):
        bg = "#1F4E79" if b.get("primary", True) else "#FFFFFF"
        fg = "#FFFFFF" if b.get("primary", True) else "#1F4E79"
        rows.append(
            '<tr><td style="padding:6px 0;">'
            f'<a href="{esc(b["url"])}" style="display:block;text-align:center;padding:12px 16px;'
            f'background:{bg};color:{fg};border:1px solid #1F4E79;border-radius:6px;'
            f'font-weight:bold;text-decoration:none;font-family:Arial,sans-serif;">{esc(b["label"])}</a>'
            "</td></tr>")
    reminder_html = ('<p style="margin:0 0 12px 0;color:#8a5a00;"><strong>Reminder:</strong> '
                     "we have not received your response yet.</p>") if reminder else ""
    html_body = (
        '<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#222;">'
        f'<div style="background:#1F4E79;color:#fff;padding:14px 18px;font-size:18px;font-weight:bold;">'
        f'{esc(parts.get("headline") or campaign["title"])}</div>'
        '<div style="padding:18px;border:1px solid #dcdcdc;border-top:0;">'
        f'{reminder_html}{parts.get("body_html", "")}'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:16px 0;">{"".join(rows)}</table>'
        '<p style="font-size:12px;color:#666;margin:16px 0 0 0;">'
        f"{esc(why)} This link is private to {esc(rec['email'])}. Please do not forward it: "
        "anyone who has it can answer as you.</p></div>"
        f'<img src="{esc(urls["pixel"])}" width="1" height="1" alt="" style="display:none;">'
        "</div>"
    )
    text_lines = []
    if reminder:
        text_lines.append("REMINDER: we have not received your response yet.\n")
    text_lines.append(parts.get("body_text", ""))
    for b in parts.get("buttons", []):
        text_lines.append(f"{b['label']}: {b['url']}")
    if why:
        text_lines.append("\n" + why)
    text_lines.append(f"This link is private to {rec['email']}. Please do not forward it.")
    return html_body, "\n".join(text_lines)


def _send_one(campaign: dict, rec: dict, kind: Kind, base_url: str, *, reminder: bool = False) -> tuple[str, str | None]:
    base = respond_url(base_url, rec["token"])
    urls = {"respond": base, "pixel": f"{base}/o.gif",
            "choice": lambda key: f"{base}?a={key}"}
    parts = kind.email_parts(campaign, rec, urls)
    html_body, text_body = build_email(campaign, rec, parts, urls, reminder=reminder)
    subject = ("Reminder: " if reminder else "") + campaign["subject"]
    to = rec["email"]
    if campaign["test_mode"] and campaign.get("test_address"):
        subject = f"[TEST — would have gone to: {to}] {subject}"
        to = campaign["test_address"]
    res = email_client.send_email(to=to, subject=subject, body_html=html_body,
                                  body_text=text_body, sender=campaign["sender_email"])
    status = res.get("status")
    if status == "sent":
        return "sent", None
    if status == "suppressed":
        return "suppressed", res.get("error")
    return "failed", str(res.get("error") or res)[:500]


def send_pending(campaign_id: int, *, base_url: str, limit: int = SEND_CHUNK,
                 retry_failed: bool = False) -> dict:
    """Send up to `limit` unsent emails. Resumable: call again until remaining == 0.
    Only 'pending' rows are tried unless retry_failed (so a failure never loops
    forever). Commits after every email, so a crash never re-sends a delivered one.
    A session advisory lock makes two simultaneous clicks safe."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s, %s) AS got", (_LOCK_NS, campaign_id))
            if not cur.fetchone()["got"]:
                return {"busy": True, "sent": 0, "failed": 0, "suppressed": 0, "remaining": None}
            try:
                cur.execute("SELECT * FROM portal.outreach_campaigns WHERE id = %s", (campaign_id,))
                c = cur.fetchone()
                if not c:
                    raise OutreachError("Campaign not found.")
                if c["status"] not in ("sending", "open"):
                    raise OutreachError(f"A {c['status']} campaign cannot send.")
                kind = get_kind(c["kind"])
                wanted = ["pending", "failed"] if retry_failed else ["pending"]
                cur.execute(
                    "SELECT * FROM portal.outreach_recipients WHERE campaign_id = %s AND send_status = ANY(%s) "
                    "AND status <> 'excluded' ORDER BY id LIMIT %s", (campaign_id, wanted, limit))
                batch = cur.fetchall()
                tally = {"sent": 0, "failed": 0, "suppressed": 0}
                for rec in batch:
                    status, err = _send_one(c, rec, kind, base_url)
                    cur.execute(
                        "UPDATE portal.outreach_recipients SET send_status = %s, send_error = %s, "
                        "sent_at = CASE WHEN %s = 'sent' THEN NOW() ELSE sent_at END WHERE id = %s",
                        (status, err, status, rec["id"]))
                    _event(cur, campaign_id, rec["id"],
                           {"sent": "sent", "suppressed": "suppressed"}.get(status, "send_failed"),
                           detail={"error": err} if err else {})
                    conn.commit()
                    tally[status] += 1
                cur.execute(
                    "SELECT count(*) AS n FROM portal.outreach_recipients WHERE campaign_id = %s "
                    "AND send_status = 'pending' AND status <> 'excluded'", (campaign_id,))
                remaining = cur.fetchone()["n"]
                if remaining == 0 and c["status"] == "sending":
                    cur.execute("UPDATE portal.outreach_campaigns SET status = 'open', updated_at = NOW() "
                                "WHERE id = %s AND status = 'sending'", (campaign_id,))
                    _event(cur, campaign_id, None, "campaign_open")
                    conn.commit()
                return {"busy": False, **tally, "remaining": remaining}
            finally:
                # An error mid-chunk leaves the transaction aborted, so roll back before
                # unlocking (closing the connection would release the lock anyway).
                try:
                    conn.rollback()
                    cur.execute("SELECT pg_advisory_unlock(%s, %s)", (_LOCK_NS, campaign_id))
                    conn.commit()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# The recipient's side: token lookup, state, tracking, response
# ---------------------------------------------------------------------------
def lookup(token: str) -> dict | None:
    """{'recipient', 'campaign', 'state'} or None for an unknown token."""
    if not token or len(token) > 200:
        return None
    rec = db.query_one("SELECT * FROM portal.outreach_recipients WHERE token = %s", (token,))
    if not rec:
        return None
    campaign = get_campaign(rec["campaign_id"])
    if not campaign:
        return None
    flags = db.query_one(
        "SELECT (c.closes_at IS NOT NULL AND NOW() > c.closes_at) AS past_close, "
        "(r.token_expires_at IS NOT NULL AND NOW() > r.token_expires_at) AS token_expired "
        "FROM portal.outreach_recipients r JOIN portal.outreach_campaigns c ON c.id = r.campaign_id "
        "WHERE r.id = %s", (rec["id"],))
    return {"recipient": rec, "campaign": campaign, "state": _state(campaign, rec, flags)}


def _state(campaign: dict, rec: dict, flags: dict) -> str:
    """The one place that decides what a token may still do ('active' = may respond)."""
    if campaign["status"] == "cancelled":
        return "cancelled"
    if campaign["status"] == "draft":
        return "not_open"
    if campaign["status"] == "closed" or flags["past_close"]:
        return "closed"
    if flags["token_expired"]:
        return "expired"
    if rec["status"] == "excluded":
        return "excluded"
    return "active"


def last_response_at(recipient_id: int):
    """When this person's CURRENT answer was recorded (their first answer, or their latest change)."""
    row = db.query_one(
        "SELECT max(occurred_at) AS at FROM portal.outreach_events "
        "WHERE recipient_id = %s AND event_type IN ('responded', 'response_changed')", (recipient_id,))
    return row["at"] if row else None


def group_responder(campaign: dict, rec: dict) -> dict | None:
    """any_in_group: another member of this recipient's group who has already responded."""
    if campaign["completion_rule"] != "any_in_group" or not rec.get("group_key"):
        return None
    return db.query_one(
        "SELECT id, name, email, responded_at FROM portal.outreach_recipients "
        "WHERE campaign_id = %s AND group_key = %s AND status = 'responded' AND id <> %s "
        "ORDER BY responded_at LIMIT 1", (campaign["id"], rec["group_key"], rec["id"]))


def record_visit(rec: dict, ip: str | None, ua: str | None, choice: str | None = None) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.outreach_recipients SET click_count = click_count + 1, "
                "first_click_at = COALESCE(first_click_at, NOW()) WHERE id = %s RETURNING click_count", (rec["id"],))
            row = cur.fetchone()
            # The counter always counts; the event rows stop at EVENT_CAP so a looping client cannot flood the log.
            if row and row["click_count"] <= EVENT_CAP:
                _event(cur, rec["campaign_id"], rec["id"], "link_visit", ip=ip, ua=ua,
                       detail={"choice": choice} if choice else {})


def record_open(token: str, ip: str | None, ua: str | None) -> bool:
    """The pixel. A best-effort signal only: never changes a status."""
    rec = db.query_one("SELECT id, campaign_id FROM portal.outreach_recipients WHERE token = %s", (token or "",))
    if not rec:
        return False
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.outreach_recipients SET open_count = open_count + 1, "
                "first_open_at = COALESCE(first_open_at, NOW()) WHERE id = %s RETURNING open_count", (rec["id"],))
            row = cur.fetchone()
            if row and row["open_count"] <= EVENT_CAP:
                _event(cur, rec["campaign_id"], rec["id"], "open_signal", ip=ip, ua=ua)
    return True


VIAS = ("email_button", "form")   # how a response arrived: a one-tap email button, or the form


def record_response(token: str, form: dict, ip: str | None, ua: str | None, via: str | None = None) -> dict:
    """Validate and record a response (via = how it arrived, kept in the event log so an admin can
    tell a one-tap email answer from a form submit). Returns {'state': ...}:
       done | error (with errors) | already_responded | group_done | any non-active
       state from _state() | not_found. The recipient row is locked for the whole
       write, so a double POST cannot record twice."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM portal.outreach_recipients WHERE token = %s FOR UPDATE", (token or "",))
            rec = cur.fetchone()
            if not rec:
                return {"state": "not_found"}
            cur.execute("SELECT * FROM portal.outreach_campaigns WHERE id = %s", (rec["campaign_id"],))
            campaign = cur.fetchone()
            cur.execute(
                "SELECT (c.closes_at IS NOT NULL AND NOW() > c.closes_at) AS past_close, "
                "(r.token_expires_at IS NOT NULL AND NOW() > r.token_expires_at) AS token_expired "
                "FROM portal.outreach_recipients r JOIN portal.outreach_campaigns c ON c.id = r.campaign_id "
                "WHERE r.id = %s", (rec["id"],))
            state = _state(campaign, rec, cur.fetchone())
            if state != "active":
                return {"state": state, "campaign": campaign, "recipient": rec}
            if campaign["completion_rule"] == "any_in_group" and rec.get("group_key"):
                # The row lock above is only this person's own row: two members of one group answering
                # together would both pass the check below. Serialize per group until commit.
                cur.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (campaign["id"], rec["group_key"]))
            other = group_responder(campaign, rec)
            if other:
                return {"state": "group_done", "campaign": campaign, "recipient": rec, "by": other}
            already = rec["status"] == "responded"
            if already and not campaign["allow_change"]:
                return {"state": "already_responded", "campaign": campaign, "recipient": rec}
            kind = get_kind(campaign["kind"])
            parsed, errors = kind.parse_response(campaign, rec, form)
            if errors:
                return {"state": "error", "errors": errors, "campaign": campaign, "recipient": rec}
            detail = kind.save_response(cur, campaign, rec, parsed) or {}
            if via in VIAS:
                detail = {**detail, "via": via}
            if not (already and detail.get("unchanged")):
                cur.execute(
                    "UPDATE portal.outreach_recipients SET status = 'responded', "
                    "responded_at = COALESCE(responded_at, NOW()) WHERE id = %s", (rec["id"],))
                _event(cur, campaign["id"], rec["id"], "response_changed" if already else "responded",
                       ip=ip, ua=ua, detail=detail)
            return {"state": "done", "changed": already and not detail.get("unchanged"),
                    "campaign": campaign, "recipient": rec}


# ---------------------------------------------------------------------------
# Reminders, closing, results
# ---------------------------------------------------------------------------
_REMINDER_SQL = (
    "SELECT r.* FROM portal.outreach_recipients r "
    "JOIN portal.outreach_campaigns c ON c.id = r.campaign_id "
    "WHERE c.id = %s AND c.status = 'open' "
    "AND (c.closes_at IS NULL OR NOW() < c.closes_at) "
    "AND r.status = 'pending' AND r.send_status = 'sent' "
    "AND (r.token_expires_at IS NULL OR r.token_expires_at > NOW()) "   # never remind someone whose link is dead
    "{due} "
    "AND (c.completion_rule = 'each' OR r.group_key IS NULL OR NOT EXISTS ("
    "  SELECT 1 FROM portal.outreach_recipients g WHERE g.campaign_id = r.campaign_id "
    "  AND g.group_key = r.group_key AND g.status = 'responded')) "
    "ORDER BY r.id LIMIT %s"
)
_DUE_CLAUSE = ("AND NOT c.reminders_paused AND c.reminder_every_days IS NOT NULL "
               "AND COALESCE(r.last_reminder_at, r.sent_at) + make_interval(days => c.reminder_every_days) <= NOW()")
# The manual button ignores the cadence and the pause switch, but never re-reminds someone who was
# reminded in the last hour -- so a repeated click (or the page's keep-going loop) cannot spam.
_MANUAL_CLAUSE = "AND (r.last_reminder_at IS NULL OR r.last_reminder_at < NOW() - interval '1 hour')"


def send_reminders(campaign_id: int, *, base_url: str, only_due: bool = True, limit: int = SEND_CHUNK) -> dict:
    """Remind recipients who have not responded. only_due=True follows the campaign's
    cadence and pause switch (the scheduler); False is the admin's manual 'remind
    non-responders' button, which ignores both but skips anyone reminded in the last hour."""
    c = get_campaign(campaign_id)
    if not c:
        raise OutreachError("Campaign not found.")
    kind = get_kind(c["kind"])
    sent = failed = 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            # One reminder run per poll at a time: a scheduler run and the manual button overlapping
            # would otherwise both read the same "not yet reminded" rows and remind everyone twice.
            cur.execute("SELECT pg_try_advisory_lock(%s, %s) AS got", (_LOCK_NS_REMIND, campaign_id))
            if not cur.fetchone()["got"]:
                return {"sent": 0, "failed": 0, "busy": True}
            try:
                # Read AFTER taking the lock, so a run that waited sees the first run's last_reminder_at.
                cur.execute(_REMINDER_SQL.format(due=_DUE_CLAUSE if only_due else _MANUAL_CLAUSE), (campaign_id, limit))
                rows = cur.fetchall()
                for rec in rows:
                    status, err = _send_one(c, rec, kind, base_url, reminder=True)
                    if status == "sent":
                        cur.execute("UPDATE portal.outreach_recipients SET last_reminder_at = NOW(), "
                                    "reminder_count = reminder_count + 1 WHERE id = %s", (rec["id"],))
                        _event(cur, campaign_id, rec["id"], "reminder_sent")
                        sent += 1
                    else:
                        # last_reminder_at is the last ATTEMPT: a bad address is tried again after the
                        # cadence (or the manual one-hour gap), not on every scheduler run.
                        cur.execute("UPDATE portal.outreach_recipients SET last_reminder_at = NOW() WHERE id = %s",
                                    (rec["id"],))
                        _event(cur, campaign_id, rec["id"], "reminder_failed", detail={"error": err, "status": status})
                        failed += 1
                    conn.commit()
            finally:
                try:
                    conn.rollback()
                    cur.execute("SELECT pg_advisory_unlock(%s, %s)", (_LOCK_NS_REMIND, campaign_id))
                    conn.commit()
                except Exception:
                    pass
    return {"sent": sent, "failed": failed}


def campaigns_due_for_reminders() -> list[int]:
    rows = db.query(
        "SELECT DISTINCT c.id FROM portal.outreach_campaigns c "
        "JOIN portal.outreach_recipients r ON r.campaign_id = c.id "
        "WHERE c.status = 'open' AND NOT c.reminders_paused AND c.reminder_every_days IS NOT NULL "
        "AND (c.closes_at IS NULL OR NOW() < c.closes_at) AND r.status = 'pending' AND r.send_status = 'sent' "
        "AND (r.token_expires_at IS NULL OR r.token_expires_at > NOW()) "
        "AND COALESCE(r.last_reminder_at, r.sent_at) + make_interval(days => c.reminder_every_days) <= NOW()")
    return [r["id"] for r in rows]


def close_expired(campaign_id: int | None = None) -> int:
    """Mark campaigns whose deadline has passed as closed (the pages already treat
    them as closed on read -- this just makes the stored status honest).
    campaign_id limits it to one campaign (used by tests, never by the scheduler)."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.outreach_campaigns SET status = 'closed', closed_at = NOW(), updated_at = NOW() "
                "WHERE status IN ('sending', 'open') AND closes_at IS NOT NULL AND NOW() > closes_at "
                "AND (%s::int IS NULL OR id = %s) RETURNING id", (campaign_id, campaign_id))
            ids = [r["id"] for r in cur.fetchall()]
            for cid in ids:
                _event(cur, cid, None, "campaign_closed", detail={"reason": "deadline"})
    return len(ids)


def close_campaign(campaign_id: int, by_user_id: int | None) -> bool:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.outreach_campaigns SET status = 'closed', closed_at = NOW(), updated_at = NOW() "
                "WHERE id = %s AND status IN ('sending', 'open') RETURNING id", (campaign_id,))
            ok = cur.fetchone() is not None
            if ok:
                _event(cur, campaign_id, None, "campaign_closed", detail={"by": by_user_id})
    return ok


def cancel_campaign(campaign_id: int, by_user_id: int | None) -> bool:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.outreach_campaigns SET status = 'cancelled', closed_at = NOW(), updated_at = NOW() "
                "WHERE id = %s AND status IN ('draft', 'sending', 'open') RETURNING id", (campaign_id,))
            ok = cur.fetchone() is not None
            if ok:
                _event(cur, campaign_id, None, "campaign_cancelled", detail={"by": by_user_id})
    return ok


def exclude_recipient(campaign_id: int, recipient_id: int, by_user_id: int | None, reason: str = "") -> bool:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portal.outreach_recipients SET status = 'excluded' "
                "WHERE id = %s AND campaign_id = %s AND status = 'pending' RETURNING id", (recipient_id, campaign_id))
            ok = cur.fetchone() is not None
            if ok:
                _event(cur, campaign_id, recipient_id, "excluded", detail={"by": by_user_id, "reason": reason})
    return ok


def delete_campaign(campaign_id: int) -> bool:
    """Hard delete -- only a draft or a test campaign (everything cascades). A real
    sent campaign is closed or cancelled, never erased."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM portal.outreach_campaigns WHERE id = %s AND (status = 'draft' OR test_mode) "
                        "RETURNING id", (campaign_id,))
            return cur.fetchone() is not None


def counts(campaign_id: int) -> dict:
    row = db.query_one(
        "SELECT count(*) AS total, "
        "count(*) FILTER (WHERE send_status = 'sent') AS sent, "
        "count(*) FILTER (WHERE send_status = 'failed') AS failed, "
        "count(*) FILTER (WHERE send_status = 'suppressed') AS suppressed, "
        "count(*) FILTER (WHERE send_status = 'pending' AND status <> 'excluded') AS unsent, "
        "count(*) FILTER (WHERE first_open_at IS NOT NULL) AS opened, "
        "count(*) FILTER (WHERE first_click_at IS NOT NULL) AS clicked, "
        "count(*) FILTER (WHERE status = 'responded') AS responded, "
        "count(*) FILTER (WHERE status = 'excluded') AS excluded, "
        "count(*) FILTER (WHERE status = 'pending' AND send_status = 'sent') AS awaiting "
        "FROM portal.outreach_recipients WHERE campaign_id = %s", (campaign_id,))
    return dict(row)


def recipients_report(campaign_id: int) -> list[dict]:
    return db.query(
        "SELECT id, name, email, role_label, group_key, send_status, send_error, sent_at, first_open_at, "
        "open_count, first_click_at, click_count, responded_at, status, reminder_count, last_reminder_at, meta "
        "FROM portal.outreach_recipients WHERE campaign_id = %s "
        "ORDER BY (status = 'responded'), name, email", (campaign_id,))


def recent_events(campaign_id: int, limit: int = 200) -> list[dict]:
    return db.query(
        "SELECT e.id, e.event_type, e.occurred_at, e.ip, e.detail, r.name, r.email "
        "FROM portal.outreach_events e LEFT JOIN portal.outreach_recipients r ON r.id = e.recipient_id "
        "WHERE e.campaign_id = %s ORDER BY e.id DESC LIMIT %s", (campaign_id, limit))
