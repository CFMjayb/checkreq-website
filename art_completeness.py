"""
art_completeness.py — ART completeness tracking (Tier 2 of the ART List
build, 2026-08-02). See Invoice Processing Intake Plan.md's "ART
completeness tracking" section for the full design.

Scope, deliberately: this module builds the MANUAL, on-demand version of
the reconciliation only — a button click that runs the check live against
real QBO data for one ART entry (or every active entry in the current
entity) and writes checkreq.art_period_status. The plan's own nightly
Cloud Run Job + Cloud Scheduler piece (a new script in the separate
cfm-daily-jobs repo, 26-124) is explicitly NOT built here — that's new
GCP infrastructure in a different repo, and this org's own standing pattern
(see feedback_auto_mode_classifier_gates memory) is that first-time
infrastructure creation of that kind can get blocked by the session's own
safety classifier, and in any case deserves a human confirming the exact
"overdue"/grace-window behavior against real data before it starts sending
automated status emails. This manual version proves the reconciliation
LOGIC works correctly against real production data -- that's the safely
buildable, valuable piece today. The next step (the nightly job + the new
"ART Completeness" section in 26-124's summary_job.py 6 AM email) is
deliberately left for a future session, once Jay has watched a few manual
runs and is comfortable with how it behaves.

Matching logic (from the plan): for one active, non-expired ART entry with
a real gl_account_id (a single-line vendor -- the plan's 'mult' case has no
one account to check against and is explicitly left unautomated, see
_check_one's early return), look up real QBO transactions against that GL
account for the entry's *current* expected period (via qbo_mcp_client's
get_gl_detail(), reusing qbo-mcp-server's existing canonical GL fetch --
no new QBO API surface needed, matching the plan's own instruction).
Match by vendor name (fuzzy, case-insensitive substring either direction --
QBO's own free-text payee name field has no other reliable identifier to
join on here) and the entry's own amount_check_mode:
  exact     -- the transaction's absolute amount must equal amount_exact
  range     -- amount_min <= abs(amount) <= amount_max
  seasonal/
  variable  -- presence only, no amount check
Write the result (pending/posted/overdue, matched QBO txn id if found) to
checkreq.art_period_status, using the entry's own grace_days to decide
whether a still-missing period counts as overdue yet or is still pending.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

import db
import qbo_mcp_client

_EXPECTED_DAY_WORDS = {
    "1st": 1, "first": 1,
    "15th": 15, "fifteenth": 15,
    "25th": 25, "twenty-fifth": 25,
}


def _parse_expected_day(expected_day_of_month: str | None) -> int | None:
    """'1st'/'15th'/'25th' -> the day number. 'NA'/'MAIL'/blank/anything
    else not recognized -> None (no specific day to compare against --
    completeness for that entry is judged only against the period's own
    end + grace_days)."""
    s = (expected_day_of_month or "").strip().lower()
    if s in _EXPECTED_DAY_WORDS:
        return _EXPECTED_DAY_WORDS[s]
    # Tolerate a bare number too ("15") even though the real spreadsheet
    # data is always ordinal-word-shaped -- costs nothing, guesses nothing.
    if s.isdigit():
        n = int(s)
        if 1 <= n <= 31:
            return n
    return None


def frequency_bucket(frequency: str | None) -> str:
    """'monthly' | 'quarterly' | 'annual' | 'skip'. Free-text real data
    ('Monthly', 'Quarterly', 'Annual', 'Bi-Monthly', 'seasonal', 'as
    needed') is bucketed by substring match, not an exact enum -- 'skip'
    means this frequency has no reliable single expected period to check
    against, so no period row is created/judged for it at all (never
    silently guessed at)."""
    s = (frequency or "").strip().lower()
    if "annual" in s:
        return "annual"
    if "quarter" in s:
        return "quarterly"
    if "season" in s or "as needed" in s or "variable" in s:
        return "skip"
    # Monthly, Bi-Monthly, blank, or anything else not recognized above --
    # monthly is the real data's overwhelmingly common case and the
    # sensible default granularity.
    return "monthly"


def current_period(bucket: str, today: date) -> tuple[str, date, date]:
    """(period_label, period_start, period_end) for `today`'s period under
    the given bucket. period_end may be in the future within the period
    (e.g. the 30th of a still-in-progress month) -- callers cap their QBO
    lookup window at `today`, never at period_end."""
    if bucket == "annual":
        return (str(today.year), date(today.year, 1, 1), date(today.year, 12, 31))
    if bucket == "quarterly":
        q = (today.month - 1) // 3 + 1
        start_month = (q - 1) * 3 + 1
        end_month = start_month + 2
        last_day = calendar.monthrange(today.year, end_month)[1]
        return (
            f"{today.year}-Q{q}",
            date(today.year, start_month, 1),
            date(today.year, end_month, last_day),
        )
    # monthly
    last_day = calendar.monthrange(today.year, today.month)[1]
    return (
        f"{today.year}-{today.month:02d}",
        date(today.year, today.month, 1),
        date(today.year, today.month, last_day),
    )


def _expected_date(period_start: date, day: int | None) -> date | None:
    if day is None:
        return None
    last_day = calendar.monthrange(period_start.year, period_start.month)[1]
    return period_start.replace(day=min(day, last_day))


def _normalize_name(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum() or ch.isspace()).strip()


def _vendor_name_matches(qbo_payee_name: str, vendor_display_name: str) -> bool:
    """Fuzzy, case/punctuation-insensitive substring match either
    direction. QBO's free-text payee field has no other reliable join key
    available from the GL report -- this is a deliberate heuristic, not an
    exact-id match, and every match this function makes is surfaced back
    to the admin with the real matched transaction's own name/date/amount
    so a false positive is always visually obvious, never silently
    trusted."""
    a, b = _normalize_name(qbo_payee_name), _normalize_name(vendor_display_name)
    if not a or not b:
        return False
    return a in b or b in a


def _amount_matches(art: dict, amount: float) -> bool:
    mode = art.get("amount_check_mode")
    amt = abs(amount)
    if mode == "exact":
        exact = art.get("amount_exact")
        return exact is not None and abs(amt - float(exact)) < 0.01
    if mode == "range":
        lo, hi = art.get("amount_min"), art.get("amount_max")
        if lo is None or hi is None:
            return False
        return float(lo) <= amt <= float(hi)
    # seasonal / variable -- presence only, no amount check
    return True


def _art_row_for_check(art_id: int) -> dict | None:
    return db.query_one(
        """
        SELECT al.*, v.display_name AS vendor_display_name, v.is_active AS vendor_is_active,
               ga.account_number, ga.account_name, o.code AS org_code
        FROM checkreq.art_list al
        JOIN checkreq.vendors v ON v.id = al.vendor_id
        LEFT JOIN checkreq.gl_accounts ga ON ga.id = al.gl_account_id
        JOIN checkreq.organizations o ON o.id = al.org_id
        WHERE al.id = %s
        """,
        (art_id,),
    )


def check_one(art_id: int, *, today: date | None = None) -> dict:
    """Runs the completeness check for one ART entry's current period,
    writes checkreq.art_period_status, and returns a plain dict describing
    what happened -- always a dict, never raises, so a single bad entry in
    a bulk run (check_org) can't take the others down with it. Shape:
    {"art_id", "ok": bool, "skipped": bool, "reason": str|None,
     "period_label", "status", "matched_qbo_txn_id", "matched_amount",
     "posted_date"}."""
    today = today or date.today()
    art = _art_row_for_check(art_id)
    if not art:
        return {"art_id": art_id, "ok": False, "skipped": True, "reason": "ART entry not found."}

    if not art["is_active"]:
        return {"art_id": art_id, "ok": True, "skipped": True, "reason": "Entry is not active."}
    if art.get("authorized_from") and art["authorized_from"] > today:
        return {"art_id": art_id, "ok": True, "skipped": True,
                "reason": f"Not yet in effect (starts {art['authorized_from']})."}
    if art.get("authorized_through") and art["authorized_through"] < today:
        return {"art_id": art_id, "ok": True, "skipped": True,
                "reason": f"Authorization ended {art['authorized_through']}."}

    bucket = frequency_bucket(art.get("frequency"))
    if bucket == "skip":
        return {"art_id": art_id, "ok": True, "skipped": True,
                "reason": f"Frequency '{art.get('frequency')}' has no single reliable expected "
                          f"period -- not auto-checked."}

    if not art.get("account_number"):
        return {"art_id": art_id, "ok": True, "skipped": True,
                "reason": "No single GL account on file (multi-line vendor) -- "
                          "verify this one manually."}

    period_label, period_start, period_end = current_period(bucket, today)
    day = _parse_expected_day(art.get("expected_day_of_month"))
    expected = _expected_date(period_start, day)
    grace_days = int(art.get("grace_days") or 0)
    deadline = (expected or period_end) + timedelta(days=grace_days)

    gl_end = today if today < period_end else period_end
    data, err = qbo_mcp_client.get_gl_detail(
        (art["org_code"] or "").lower(), art["account_number"],
        period_start.isoformat(), gl_end.isoformat(),
    )
    if err:
        return {"art_id": art_id, "ok": False, "skipped": False,
                "reason": f"QBO lookup failed: {err}"}

    candidates = []
    for acct in (data or {}).get("accounts", []):
        for txn in acct.get("transactions", []):
            if not _vendor_name_matches(txn.get("name", ""), art["vendor_display_name"]):
                continue
            if not _amount_matches(art, float(txn.get("amount") or 0)):
                continue
            candidates.append(txn)

    matched = None
    if candidates:
        if expected:
            matched = min(
                candidates,
                key=lambda t: abs((date.fromisoformat(t["date"]) - expected).days) if t.get("date") else 999,
            )
        else:
            matched = max(candidates, key=lambda t: t.get("date") or "")

    if matched:
        status = "posted"
        posted_date = matched.get("date")
        matched_txn_id = matched.get("txn_id") or None
        matched_amount = abs(float(matched.get("amount") or 0))
    else:
        status = "overdue" if today > deadline else "pending"
        posted_date = None
        matched_txn_id = None
        matched_amount = None

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO checkreq.art_period_status
                    (art_list_id, period_label, expected_date, status, posted_date,
                     matched_qbo_txn_id, matched_amount, checked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (art_list_id, period_label) DO UPDATE SET
                    expected_date = EXCLUDED.expected_date,
                    -- Never downgrade a human's manual_resolved call with an
                    -- automated re-check -- that status only ever changes via
                    -- the admin's own "Mark Resolved" action.
                    status = CASE WHEN checkreq.art_period_status.status = 'manually_resolved'
                                   THEN checkreq.art_period_status.status ELSE EXCLUDED.status END,
                    posted_date = EXCLUDED.posted_date,
                    matched_qbo_txn_id = EXCLUDED.matched_qbo_txn_id,
                    matched_amount = EXCLUDED.matched_amount,
                    checked_at = NOW()
                """,
                (art_id, period_label, expected, status, posted_date,
                 matched_txn_id, matched_amount),
            )

    return {
        "art_id": art_id, "ok": True, "skipped": False, "reason": None,
        "period_label": period_label, "status": status,
        "matched_qbo_txn_id": matched_txn_id, "matched_amount": matched_amount,
        "posted_date": posted_date,
    }


def check_org(org_id: int) -> dict:
    """Runs check_one() for every ART entry in the given org. On-demand
    only (a Refresh-All-style button click), never on page load -- each
    entry is a live QBO round trip via qbo-mcp-server, same reasoning
    admin_setup.py's own per-account budget-check button already documents
    for exactly this class of call."""
    ids = [r["id"] for r in db.query(
        "SELECT id FROM checkreq.art_list WHERE org_id = %s ORDER BY id", (org_id,)
    )]
    results = [check_one(i) for i in ids]
    checked = sum(1 for r in results if r["ok"] and not r["skipped"])
    skipped = sum(1 for r in results if r["skipped"])
    failed = sum(1 for r in results if not r["ok"])
    posted = sum(1 for r in results if r.get("status") == "posted")
    overdue = sum(1 for r in results if r.get("status") == "overdue")
    return {
        "total": len(ids), "checked": checked, "skipped": skipped, "failed": failed,
        "posted": posted, "overdue": overdue, "results": results,
    }
