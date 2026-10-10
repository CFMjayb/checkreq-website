"""
donor_portal_admin.py -- Beacon Donor Management: the STAFF side of the parishioner login (Parishioner Self-Service).

A parishioner login is an explicit record (donor.parishioner_login) that staff turn on for ONE person at ONE parish from that person's
System tab, like TouchPoint's System > User Account. It is NOT a Beacon login: it is never an app_users row, it appears nowhere in
Setup > Users, it holds no donor role and no capability, and a parishioner session never satisfies a staff route (see
donor_portal_login). Nobody signs in by email match alone: the code goes only to the sign-in email staff pick here, and that email must
still be an active email on the person's profile each time they sign in.

  login_panel(ctx, person_id)              what the System tab's "Parishioner login (self-service)" panel shows (None without roles.manage)
  login_enable(ctx, person_id, email)      turn the login on (or change its sign-in email): a connected, eligible person and one of their
                                           active emails are required (a blank email uses the person's one active email)
  login_disable(ctx, person_id, reason)    turn it off (a reason is required, the row is kept) and end every live session at once
  login_sign_out_everywhere(ctx, person_id)  end every live session of the person; the login stays enabled

Who: capability roles.manage (a Parish Admin, or the diocese's Beacon Admin or Setup Admin). Every action goes to donor.change_log.
Never for an organization, a placeholder, an archived or deceased person, or anyone under 18, and never for a person this parish is not
connected to (require_connection). Pinned by Tools/test_donor_portal.py.
"""
from __future__ import annotations

import donor_portal_login as L
import donor_roles
from donor_core import Conflict, Ctx, InvalidInput, NotFound, clean_email, clean_text, is_minor, log_change, tx
from donor_people import require_connection

MANAGE_MESSAGE = "Only a Parish Admin, or the diocese, can manage parishioner logins."
NO_EMAIL_MESSAGE = "Add an email address on the Personal tab first."
EVENT_WORDS = {"signed_in": "Signed in", "signed_out": "Signed out", "session_ended": "Session ended"}
END_WORDS = {"expired_idle": "timed out after 30 idle minutes", "expired_absolute": "reached its two-hour limit",
             "ineligible": "no longer allowed to sign in", "login_disabled": "login turned off by staff",
             "signed_out_by_staff": "signed out by staff", "replaced": "replaced by a newer sign-in", "signed_out": "signed out"}


def ineligible_reason(row: dict) -> str | None:
    """Why a parishioner login can never be set up for this person, in words that finish 'cannot be set up for ...'."""
    if row["record_type"] != "person":
        return "an organization"
    if row["is_placeholder"]:
        return "a placeholder record"
    if row["archived_at"] is not None:
        return "an archived record"
    if row["deceased_date"] is not None:
        return "a person who has died"
    if is_minor(row["birth_date"], household_position=row.get("position"), deceased_date=row["deceased_date"]):
        return "a person under 18"
    return None


def _state(c, person_id: int) -> dict:
    c.execute("SELECT p.record_type, p.is_placeholder, p.archived_at, p.deceased_date, p.birth_date, hm.position FROM donor.person p "
              "LEFT JOIN donor.household_member hm ON hm.person_id = p.id AND hm.left_at IS NULL WHERE p.id = %s", (person_id,))
    row = c.fetchone()
    if not row:
        raise NotFound("That person was not found at this parish.")
    return row


def _active_emails(c, person_id: int) -> list[dict]:
    c.execute("SELECT id, value, is_preferred FROM donor.person_contact WHERE person_id = %s AND kind = 'email' AND archived_at IS NULL "
              "ORDER BY is_preferred DESC, id", (person_id,))
    return c.fetchall()


def login_panel(ctx: Ctx, person_id: int) -> dict | None:
    """The System tab panel's data, or None when this role may not manage parishioner logins (the panel is then not shown at all)."""
    if not ctx.can("roles.manage"):
        return None
    with tx() as c:
        require_connection(c, ctx, person_id)
        state = _state(c, person_id)
        emails = _active_emails(c, person_id)
        c.execute("SELECT * FROM donor.parishioner_login WHERE person_id = %s AND parish_id = %s", (person_id, ctx.parish_id))
        login = c.fetchone()
        c.execute("SELECT COUNT(*) AS n FROM donor.parishioner_session WHERE person_id = %s AND parish_id = %s AND revoked_at IS NULL AND expires_at > NOW()",
                  (person_id, ctx.parish_id))
        live = c.fetchone()["n"]
        c.execute("SELECT created_at, kind, ip, detail FROM donor.parishioner_signin_event WHERE person_id = %s AND parish_id = %s "
                  "AND kind IN ('signed_in', 'signed_out', 'session_ended') ORDER BY id DESC LIMIT 10", (person_id, ctx.parish_id))
        events = c.fetchall()
    for e in events:
        e["words"] = EVENT_WORDS.get(e["kind"], e["kind"]) + (f": {END_WORDS.get(e['detail'], e['detail'])}" if e["kind"] == "session_ended" and e["detail"] else "")
    names = donor_roles.user_labels([x for x in ((login or {}).get("enabled_by_user_id"), (login or {}).get("disabled_by_user_id")) if x]) if login else {}
    return {
        "reason": ineligible_reason(state), "emails": emails, "login": login, "live_sessions": live, "events": events,
        "login_email_active": bool(login and any(e["value"].lower() == login["login_email"] for e in emails)),
        "enabled_by": names.get(login["enabled_by_user_id"]) if login else None,
        "disabled_by": names.get(login["disabled_by_user_id"]) if login and login["disabled_by_user_id"] else None,
        "portal_on": bool(ctx.settings.get("portal_enabled")),
        "no_email_message": NO_EMAIL_MESSAGE,
    }


def login_enable(ctx: Ctx, person_id: int, login_email, *, cur=None) -> dict:
    """Turn the parishioner login on for this person at this parish, with the sign-in email they pick (one of the person's ACTIVE emails).
    Left blank, the person's one active email is used (nearly everyone has exactly one); with none there is nothing to send a code to, so
    it is refused until an email is on the Personal tab, and with several a blank is refused (the caller must pick).
    Calling it again with another email changes the sign-in email. Returns {"id", "changed"}."""
    ctx.require("roles.manage", MANAGE_MESSAGE)
    email = clean_email(login_email, field="sign-in email")
    with tx(cur) as c:
        require_connection(c, ctx, person_id, include_archived=False)
        reason = ineligible_reason(_state(c, person_id))
        if reason:
            raise InvalidInput(f"A parishioner login cannot be set up for {reason}.", "person_id")
        active = _active_emails(c, person_id)
        if not email:
            if not active:
                raise InvalidInput(NO_EMAIL_MESSAGE, "login_email")
            if len(active) > 1:
                raise InvalidInput("Pick the email address the sign-in code goes to.", "login_email")
            email = active[0]["value"].lower()
        if not any(e["value"].lower() == email for e in active):
            raise InvalidInput("The sign-in email has to be one of this person's current email addresses. Add or fix it on the Personal tab first.", "login_email")
        c.execute("SELECT id, is_enabled, login_email FROM donor.parishioner_login WHERE person_id = %s AND parish_id = %s FOR UPDATE", (person_id, ctx.parish_id))
        old = c.fetchone()
        if old and old["is_enabled"] and old["login_email"] == email:
            return {"id": old["id"], "changed": False}
        if old:
            c.execute("UPDATE donor.parishioner_login SET login_email = %s, is_enabled = TRUE, enabled_by_user_id = %s, enabled_at = NOW(), "
                      "disabled_by_user_id = NULL, disabled_at = NULL, disabled_reason = NULL WHERE id = %s", (email, ctx.user_id, old["id"]))
            lid = old["id"]
            what = ("Sign-in email changed" if old["is_enabled"] else "Turned on again")
        else:
            c.execute("INSERT INTO donor.parishioner_login (person_id, parish_id, login_email, enabled_by_user_id) VALUES (%s,%s,%s,%s) RETURNING id",
                      (person_id, ctx.parish_id, email, ctx.user_id))
            lid = c.fetchone()["id"]
            what = "Turned on"
        log_change(c, ctx, "parishioner_login", lid, "Parishioner login", "off" if not (old and old["is_enabled"]) else "on", "on",
                   person_id=person_id, kind="grant", scope="parish", reason=what)
        return {"id": lid, "changed": True}


def login_disable(ctx: Ctx, person_id: int, reason, *, cur=None) -> dict:
    """Turn the login off. A short reason is required and the row is kept. Every live session ends at once."""
    ctx.require("roles.manage", MANAGE_MESSAGE)
    why = clean_text(reason, field="reason", max_len=200)
    if not why:
        raise InvalidInput("Say why the parishioner login is being turned off.", "reason")
    with tx(cur) as c:
        require_connection(c, ctx, person_id)
        c.execute("SELECT id, is_enabled FROM donor.parishioner_login WHERE person_id = %s AND parish_id = %s FOR UPDATE", (person_id, ctx.parish_id))
        old = c.fetchone()
        if not old:
            raise NotFound("This person has no parishioner login.")
        if not old["is_enabled"]:
            raise Conflict("This parishioner login is already off.")
        c.execute("UPDATE donor.parishioner_login SET is_enabled = FALSE, disabled_by_user_id = %s, disabled_at = NOW(), disabled_reason = %s WHERE id = %s",
                  (ctx.user_id, why, old["id"]))
        log_change(c, ctx, "parishioner_login", old["id"], "Parishioner login", "on", "off", person_id=person_id, kind="revoke", scope="parish", reason=why)
    ended = L.revoke_sessions(person_id, ctx.parish_id, "login_disabled")
    return {"id": old["id"], "sessions_ended": ended}


def login_sign_out_everywhere(ctx: Ctx, person_id: int, *, cur=None) -> dict:
    """End every live session of this person (a lost phone, a shared computer). The login itself stays on, so they can sign in again."""
    ctx.require("roles.manage", MANAGE_MESSAGE)
    with tx(cur) as c:
        require_connection(c, ctx, person_id)
        c.execute("SELECT id FROM donor.parishioner_login WHERE person_id = %s AND parish_id = %s", (person_id, ctx.parish_id))
        row = c.fetchone()
        if not row:
            raise NotFound("This person has no parishioner login.")
        log_change(c, ctx, "parishioner_login", row["id"], "Parishioner sessions", None, "signed out everywhere", person_id=person_id, kind="revoke",
                   scope="parish", reason="Signed out everywhere by staff")
    ended = L.revoke_sessions(person_id, ctx.parish_id, "signed_out_by_staff")
    return {"id": row["id"], "sessions_ended": ended}
