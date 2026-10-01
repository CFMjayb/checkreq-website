"""
digest_extras.py -- two additions to the 7 AM daily digest run
(POST /internal/send-daily-digest in main.py), Jay, 2026-09-30.

1. AP Review email. Until now nothing told an AP Reviewer there was work
   waiting -- only a count badge on the AP Review tile. CR26-010 sat in
   Needs GL Coding for 13 days. Each AP Reviewer now gets ONE email, only on
   mornings when something is waiting at an entity they review, listing:
     - requests needing GL coding (Ask My Accountant), with days waiting;
     - approved requests that are ready to post to QuickBooks;
     - approved requests held for a W-9, flagging the ones where the vendor
       has already uploaded it and AP just needs to confirm it.
   Each item links to the AP edit screen.

2. Communications summary. Jay: "a summary of all communications email
   ... who got what that morning", then (same evening) "I only want the
   summary of communications to go to me right now for each entity. We'll
   do more later." So: ONE email, to the addresses in app_settings
   'digest_summary_recipients' (comma-separated; default jay@cfmins.org),
   with a section per entity listing every email this run sent (or failed
   to send) for that entity: who, which kind, which requests. NOT sent when
   the run emailed nobody (Jay: "do not send emails to people who have zero
   notices to email about"). Widening it later is a settings change, not a
   deploy.

   Nobody gets an empty email: approver reminders, coding notices and the AP
   email each go out only when that person has something in them.

Kept out of main.py (already ~9,000 lines). main.py passes in the few
helpers this needs (no import of main from here -- one-way dependency, the
same convention as admin_setup.register()).

Each communication is a dict:
  {"to", "name", "kind", "subject", "orgs": set of org codes,
   "items": [str, ...], "ok": bool, "error": str|None}
"""
from __future__ import annotations

from datetime import datetime, timezone

import app_settings
import db
import email_client
import rbac

SUMMARY_RECIPIENTS_SETTING = "digest_summary_recipients"
DEFAULT_SUMMARY_RECIPIENTS = "jay@cfmins.org"


def _vendor_name(r: dict, vr_name) -> str:
    if r.get("vendor_display_name"):
        return r["vendor_display_name"]
    if r.get("vr_entity_type"):
        return vr_name(r["vr_entity_type"], r["vr_company_name"], r["vr_dba_name"],
                       r["vr_first_name"], r["vr_last_name"])
    return "—"


def _ap_work(org_ids: list[int], vr_name) -> dict:
    """Everything waiting on AP at these entities, bucketed. Same hold rules
    as main.py's ap_review_list / _w9_hold_cleared."""
    rows = db.query(
        """
        SELECT pr.id, pr.request_number, pr.status, pr.amount, pr.created_at, pr.org_id,
               pr.vendor_request_id, pr.existing_vendor_w9_flagged, pr.w9_override,
               o.code AS org_code,
               v.display_name AS vendor_display_name, v.w9_on_file, v.w9_not_required, v.w9_uploaded_at,
               vr.entity_type AS vr_entity_type, vr.first_name AS vr_first_name,
               vr.last_name AS vr_last_name, vr.company_name AS vr_company_name,
               vr.dba_name AS vr_dba_name, vr.status AS vr_status,
               vr.requires_w9 AS vr_requires_w9, vr.w9_received AS vr_w9_received,
               vr.w9_uploaded_at AS vr_w9_uploaded_at
          FROM checkreq.payment_requests pr
          JOIN checkreq.organizations o ON o.id = pr.org_id
          LEFT JOIN checkreq.vendors v ON v.id = pr.vendor_id
          LEFT JOIN checkreq.vendor_requests vr ON vr.id = pr.vendor_request_id
         WHERE pr.org_id = ANY(%s) AND pr.status IN ('AwaitingCoding', 'Approved')
         ORDER BY pr.created_at
        """,
        (org_ids,),
    )
    now = datetime.now(timezone.utc)
    work = {"coding": [], "ready": [], "confirm_w9": [], "held": []}
    for r in rows:
        r["vendor_name"] = _vendor_name(r, vr_name)
        r["days"] = max(0, (now - r["created_at"]).days) if r.get("created_at") else 0
        if r["status"] == "AwaitingCoding":
            work["coding"].append(r)
            continue
        if r["vendor_request_id"]:
            if r["vr_status"] != "approved":
                r["hold"] = "new vendor not yet approved"
            elif r["vr_requires_w9"] and not r["vr_w9_received"] and not r["w9_override"]:
                r["hold"] = "W-9 uploaded -- confirm it" if r.get("vr_w9_uploaded_at") else "waiting on W-9"
            else:
                r["hold"] = None
        elif r["existing_vendor_w9_flagged"] and not (
                r.get("w9_on_file") or r.get("w9_not_required") or r.get("w9_override")):
            r["hold"] = "W-9 uploaded -- confirm it" if r.get("w9_uploaded_at") else "waiting on W-9"
        else:
            r["hold"] = None
        if r["hold"] is None:
            work["ready"].append(r)
        elif r["hold"].startswith("W-9 uploaded"):
            r["anchor"] = "#w9"   # straight to the W-9 review panel
            work["confirm_w9"].append(r)
        else:
            work["held"].append(r)
    return work


def _section_html(title: str, rows: list[dict], base_for, esc, extra) -> str:
    if not rows:
        return ""
    trs = "".join(
        f'<tr><td style="padding:6px 8px;border-bottom:1px solid #eee;">'
        f'<a href="{base_for(r["org_id"])}/requests/{esc(r["request_number"])}/ap-edit{r.get("anchor", "")}">'
        f'<strong>{esc(r["request_number"])}</strong></a>'
        f'<br><span style="color:#888;font-size:0.85em;">{esc(r["org_code"])}</span></td>'
        f'<td style="padding:6px 8px;border-bottom:1px solid #eee;">{esc(r["vendor_name"])}</td>'
        f'<td style="padding:6px 8px;border-bottom:1px solid #eee;text-align:right;">${float(r["amount"] or 0):,.2f}</td>'
        f'<td style="padding:6px 8px;border-bottom:1px solid #eee;color:#555;">{esc(extra(r))}</td></tr>'
        for r in rows
    )
    return (f'<h3 style="font-size:15px;margin:20px 0 6px;color:#1F4E79;">{esc(title)} ({len(rows)})</h3>'
            f'<table style="width:100%;border-collapse:collapse;font-size:0.92rem;">{trs}</table>')


def _section_text(title: str, rows: list[dict], base_for, extra) -> str:
    if not rows:
        return ""
    lines = "\n".join(
        f"- {r['request_number']} ({r['org_code']}) {r['vendor_name']} ${float(r['amount'] or 0):,.2f}"
        f" -- {extra(r)}\n  {base_for(r['org_id'])}/requests/{r['request_number']}/ap-edit{r.get('anchor', '')}"
        for r in rows
    )
    return f"{title} ({len(rows)}):\n{lines}\n\n"


def send_ap_digests(*, base_for, esc, wrap_html, vr_name, sender) -> list[dict]:
    """One email per AP Reviewer with work waiting. Returns the comms log."""
    log = []
    for u in rbac.get_users_with_role("ap_reviewer"):
        org_ids = rbac.get_granted_org_ids(u["id"], "ap_reviewer")
        if not org_ids:
            continue
        w = _ap_work(org_ids, vr_name)
        total = sum(len(v) for v in w.values())
        if not total:
            continue
        sections = [
            ("Needs GL coding", w["coding"], lambda r: f"waiting {r['days']} day{'s' if r['days'] != 1 else ''}"),
            ("W-9 received -- review it to release", w["confirm_w9"], lambda r: "vendor uploaded its W-9"),
            ("Ready to post to QuickBooks", w["ready"], lambda r: "approved"),
            ("Held", w["held"], lambda r: r["hold"]),
        ]
        body = (f"<p>Hello {esc(u.get('display_name'))},</p>"
                f"<p>Here is the AP work waiting in Beacon this morning.</p>"
                + "".join(_section_html(t, rows, base_for, esc, ex) for t, rows, ex in sections))
        text = (f"Beacon AP Review -- work waiting this morning:\n\n"
                + "".join(_section_text(t, rows, base_for, ex) for t, rows, ex in sections))
        parts = []
        if w["coding"]:
            parts.append(f"{len(w['coding'])} to code")
        if w["confirm_w9"]:
            parts.append(f"{len(w['confirm_w9'])} W-9 to confirm")
        if w["ready"]:
            parts.append(f"{len(w['ready'])} ready to post")
        if w["held"]:
            parts.append(f"{len(w['held'])} held")
        subject = f"Beacon AP Review: {', '.join(parts)}"
        first_org = (w["coding"] or w["confirm_w9"] or w["ready"] or w["held"])[0]["org_id"]
        entry = {"to": u["email"], "name": u.get("display_name"), "kind": "AP Review work waiting",
                 "subject": subject, "ok": False, "error": None,
                 "orgs": {r["org_code"] for v in w.values() for r in v},
                 "items": [f"{r['request_number']} ({r['org_code']})" for v in w.values() for r in v]}
        try:
            res = email_client.send_email(
                to=u["email"], subject=subject,
                body_html=wrap_html(body, f"{base_for(first_org)}/admin/ap-review", "Beacon — AP Review"),
                body_text=text, sender=sender)
            entry["ok"] = res.get("status") == "sent"
            entry["error"] = None if entry["ok"] else res.get("error")
        except Exception as exc:  # never let one reviewer's failure stop the run
            entry["error"] = str(exc)
        log.append(entry)
    return log


def send_admin_summaries(log: list[dict], *, esc, wrap_html, sign_in_url, sender) -> int:
    """One communications summary, a section per entity, to the configured
    recipients (see module docstring). Returns the number of emails sent."""
    raw = app_settings.get_setting(SUMMARY_RECIPIENTS_SETTING, DEFAULT_SUMMARY_RECIPIENTS) \
        or DEFAULT_SUMMARY_RECIPIENTS
    recipients = [x.strip() for x in raw.split(",") if x.strip()]

    by_org: dict[str, list[dict]] = {}
    for e in log:
        for code in sorted(e["orgs"]):
            items = [i for i in e["items"] if i.endswith(f"({code})")]
            by_org.setdefault(code, []).append({**e, "items": items})

    ok = sum(1 for e in log if e["ok"])
    failed = len(log) - ok
    if not log:
        # Jay, 2026-09-30: "do not send emails to people who have zero
        # notices to email about" -- a quiet morning sends no summary.
        return 0
    else:
        failed_html = (' (<strong style="color:#c62828">' + str(failed) + ' failed</strong>)'
                       if failed else '')
        plural = "s" if ok != 1 else ""
        body = f"<p>Beacon's 7 AM run sent <strong>{ok}</strong> email{plural}{failed_html}.</p>"
        text = f"Beacon's 7 AM run -- {ok} sent, {failed} failed.\n"
        for code in sorted(by_org):
            entries = by_org[code]
            trs = "".join(
                f'<tr><td style="padding:6px 8px;border-bottom:1px solid #eee;">{esc(e["name"] or "")}<br>'
                f'<span style="color:#888;font-size:0.85em;">{esc(e["to"])}</span></td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;">{esc(e["kind"])}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;">{esc(", ".join(e["items"]))}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;'
                f'color:{"#2e7d32" if e["ok"] else "#c62828"};">'
                f'{"Sent" if e["ok"] else "FAILED: " + esc(e["error"] or "unknown")}</td></tr>'
                for e in entries)
            body += (f'<h3 style="font-size:15px;margin:20px 0 6px;color:#1F4E79;">{esc(code)}</h3>'
                     f'<table style="width:100%;border-collapse:collapse;font-size:0.9rem;">'
                     f'<tr style="background:#f5f5f5;"><th style="padding:6px 8px;text-align:left;">To</th>'
                     f'<th style="padding:6px 8px;text-align:left;">Email</th>'
                     f'<th style="padding:6px 8px;text-align:left;">Requests</th>'
                     f'<th style="padding:6px 8px;text-align:left;">Result</th></tr>{trs}</table>')
            text += f"\n{code}:\n" + "\n".join(
                f"- {e['name'] or ''} <{e['to']}>: {e['kind']} -- {', '.join(e['items'])} -- "
                f"{'Sent' if e['ok'] else 'FAILED: ' + (e['error'] or 'unknown')}" for e in entries) + "\n"
        subject = f"Beacon morning emails: {ok} sent" + (f", {failed} FAILED" if failed else "")

    sent = 0
    for to in recipients:
        try:
            res = email_client.send_email(
                to=to, subject=subject,
                body_html=wrap_html(body, sign_in_url, "Beacon \u2014 Morning Email Summary"),
                body_text=text, sender=sender)
            if res.get("status") == "sent":
                sent += 1
        except Exception as exc:
            print(f"[digest] communications summary to {to} failed: {exc}")
    return sent
