"""
statement_runs.py -- Beacon Donor Management: a STAFF RUN of giving statements for one parish and one year (Plan section 4c and 4e). Nothing here sends anything to anyone.

  run_options(ctx)                  what the "Run statements" page needs: the years with gifts, the templates, the last gift date, open deposits, the profile gaps
  plan_run(ctx, year, ...)          the preview list. Writes a "planned" run and one row per donor: planned (will be built) or skipped (with the plain reason).
                                    Builds no PDF. Refused until the parish profile is confirmed, and while another run for the same year is still open.
  build_chunk(ctx, run_id, ...)     builds the next few planned statements in ONE browser start, under a database lock for that run (two people or two tabs
                                    cannot build the same run at once). Safe to call again and again: it stops when nothing is left. A statement that fails is
                                    marked failed with a reason; nothing else stops.
  retry_failed(ctx, run_id)         puts the failed ones back to planned so the next chunk builds them
  cancel_run(ctx, run_id)           stops a run that is not finished. Statements already built stay (they are real); the rest are marked skipped
  history(ctx), get_run(ctx, id), run_summary(ctx, id), list_statements(ctx, id, ...)
  statement_file / print_pdf / zip_all / skipped_csv     the downloads (each writes one audit line)

What a run uses, fixed at the moment it is planned: the parish's template VERSION (so a template edited mid-run does not change the statements), the year, the
parish settings. The GIFTS are read again when each statement is built (so a statement is always built from the current books) and the fingerprint stored is the
fingerprint of what was actually printed. Joint pairs: both spouses must be set to joint and linked; one statement in both names is built for the lower person id
(or for whichever spouse has gifts) and the other is skipped as included.

Every function takes a Ctx (who and which parish), checks statements.run with giving turned on, and reads or writes only rows of THAT parish; a run id or a statement
id from the browser is only ever a key looked up inside the parish. The app role cannot delete anything.

Tests replace the PDF renderer, the file store and the logo loader with stand-ins (arguments `render`, `put`/`get`, `logo_loader`, `html_for`), so no test starts a
browser or writes to the real bucket unless it means to.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
import zipfile

from psycopg.types.json import Jsonb

import statement_data as SD
import statement_profile as SP
import statement_render as RND
import statement_rules as SR
import statement_templates as TPL
from donor_core import Conflict, Ctx, InvalidInput, NotFound, need_giving, tx

CHUNK = 15                      # statements per call (one browser start; about 20 seconds at most)
LOCK_NAMESPACE = 0x5354         # "ST": advisory lock key, with the run id
SORTS = ("name", "zip", "envelope")
DELIVERIES = ("all", "print", "email")
OPEN_RUN = ("planned", "building")


def _need(ctx: Ctx) -> None:
    need_giving(ctx, "statements.run", "You do not have permission to run statements for this parish.")


def _event(c, ctx: Ctx, kind: str, *, person_id=None, statement_id=None, detail=None) -> None:
    c.execute("INSERT INTO donor.stmt_event (parish_id, person_id, statement_id, kind, user_id, detail) VALUES (%s, %s, %s, %s, %s, %s)",
              (ctx.parish_id, person_id, statement_id, kind, ctx.user_id, Jsonb(detail) if detail is not None else None))


def _run(c, ctx: Ctx, run_id) -> dict:
    try:
        rid = int(run_id)
    except (TypeError, ValueError):
        raise NotFound("That run was not found at this parish.")
    c.execute("SELECT * FROM donor.stmt_run WHERE id = %s AND parish_id = %s", (rid, ctx.parish_id))
    r = c.fetchone()
    if not r:
        raise NotFound("That run was not found at this parish.")
    return r


def _settings(ctx: Ctx, minimum=None) -> dict:
    s = dict(ctx.settings)
    if minimum is not None:
        s["statement_min_total"] = minimum
    return s


def _version_parts(c, parish_id: int, version_id) -> dict:
    """The parts of the template version a run was planned with (a version of a global template or of this parish's own)."""
    if version_id is None:
        return TPL.safe_parts(None)
    c.execute("SELECT v.parts FROM donor.stmt_template_version v JOIN donor.stmt_template t ON t.id = v.template_id "
              " WHERE v.id = %s AND (t.scope = 'global' OR t.parish_id = %s)", (version_id, parish_id))
    r = c.fetchone()
    return TPL.safe_parts(r["parts"]) if r else TPL.safe_parts(None)


# ── Reading everyone's gifts for the period in two queries ──────────────────────────────────────
def _bulk_gifts(c, parish_id: int, p_from: dt.date, p_to: dt.date) -> dict[int, list[dict]]:
    c.execute(f"SELECT g.person_id, {SD.GIFT_COLUMNS} FROM donor.gift g "
              "  JOIN donor.batch b ON b.id = g.batch_id AND b.parish_id = g.parish_id AND b.status IN ('closed', 'reconciled') "
              " WHERE g.parish_id = %s AND g.person_id IS NOT NULL AND g.status <> 'voided' AND g.gift_type <> 'non_gift_receipt' "
              f"   AND {SD._EFFECTIVE_DATE} BETWEEN %s AND %s ORDER BY {SD._EFFECTIVE_DATE}, g.id", (parish_id, p_from, p_to))
    gifts = c.fetchall()
    if not gifts:
        return {}
    ids = [g["id"] for g in gifts]
    c.execute("SELECT gs.gift_id, gs.fund_id, COALESCE(NULLIF(f.statement_name, ''), f.name) AS fund_name, gs.amount "
              "  FROM donor.gift_split gs JOIN donor.fund f ON f.id = gs.fund_id AND f.parish_id = %s "
              " WHERE gs.gift_id = ANY(%s) ORDER BY gs.id", (parish_id, ids))
    by_gift: dict = {}
    for s in c.fetchall():
        by_gift.setdefault(s["gift_id"], []).append({"fund_id": s["fund_id"], "fund_name": s["fund_name"], "amount": s["amount"]})
    out: dict[int, list[dict]] = {}
    for g in gifts:
        g["splits"] = by_gift.get(g["id"], [])
        if g["splits"]:
            out.setdefault(g["person_id"], []).append(g)
    return out


def _zip_of(household: dict | None) -> str:
    h = household or {}
    return (h.get("mail_postal_code") or h.get("postal_code") or "").strip()


def _compose(c, parish_id: int, person_id: int, partner_id: int | None, p_from: dt.date, p_to: dt.date, settings: dict, *, bulk: dict | None = None) -> dict:
    """The statement for one subject: one person, or a joint pair (person_id is the lead). Reads the donor records (and, unless `bulk` supplies them, the gifts).
    Returns {donor (the name/address block), statement, facts (for decide), record, partner_record}."""
    record = SD.donor_record(c, parish_id, person_id)
    if not record:
        raise NotFound("That person was not found at this parish.")

    def gifts_of(pid):
        if bulk is not None:
            return list(bulk.get(pid, []))
        return SD.person_gifts(c, parish_id, pid, p_from, p_to)

    own = gifts_of(person_id)
    partner_record = None
    name = SR.donor_name(record["person"])
    household = record["household"]
    if partner_id:
        partner_record = SD.donor_record(c, parish_id, partner_id)
        if partner_record:
            lead_label, partner_label = SR.donor_name(record["person"]), SR.donor_name(partner_record["person"])
            own = [dict(g, donor_label=lead_label) for g in own] + [dict(g, donor_label=partner_label) for g in gifts_of(partner_id)]
            name = f"{lead_label} and {partner_label}"
            if not SR.address_lines(household) and SR.address_lines(partner_record["household"]):
                household = partner_record["household"]
    stmt = SR.build_statement(own, p_from, p_to, include_nondeductible=bool(settings.get("statement_include_nondeductible")),
                              show_check_numbers=settings.get("statement_show_check_numbers") is not False)
    donor_out = {"person_id": person_id, "name": name, "address": SR.address_lines(household), "envelope_number": record["envelope_number"],
                 "statement_option": record["statement_option"], "delivery": record["delivery"]}
    facts = {"person_id": person_id, "statement_option": record["statement_option"], "delivery": record["delivery"], "do_not_mail": record["do_not_mail"],
             "is_placeholder": record["is_placeholder"], "has_address": bool(donor_out["address"]), "spouse": record["spouse"]}
    return {"donor": donor_out, "statement": stmt, "facts": facts, "record": record, "partner_record": partner_record, "zip": _zip_of(household)}


# ── What the page needs before a run ────────────────────────────────────────────────────────────
def run_options(ctx: Ctx, *, today: dt.date | None = None) -> dict:
    _need(ctx)
    today = today or dt.date.today()
    with tx() as c:
        c.execute(f"SELECT DISTINCT EXTRACT(YEAR FROM {SD._EFFECTIVE_DATE})::int AS y FROM donor.gift g "
                  "  JOIN donor.batch b ON b.id = g.batch_id AND b.parish_id = g.parish_id AND b.status IN ('closed', 'reconciled') "
                  " WHERE g.parish_id = %s AND g.status <> 'voided' AND g.gift_type <> 'non_gift_receipt' AND g.person_id IS NOT NULL ORDER BY y DESC", (ctx.parish_id,))
        years = [r["y"] for r in c.fetchall()]
        if today.year not in years:
            years.insert(0, today.year)
        ready, gaps = SP.profile_ready(c, ctx.parish_id)
        c.execute("SELECT MAX(entered_at) AS last_entered FROM donor.gift WHERE parish_id = %s", (ctx.parish_id,))
        last_entered = (c.fetchone() or {}).get("last_entered")
        c.execute("SELECT COUNT(*) AS n FROM donor.batch WHERE parish_id = %s AND status IN ('open', 'pending')", (ctx.parish_id,))
        open_batches = c.fetchone()["n"]
        c.execute("SELECT id, status, period_from FROM donor.stmt_run WHERE parish_id = %s AND status IN ('planned', 'building') ORDER BY id DESC", (ctx.parish_id,))
        open_runs = c.fetchall()
    templates = TPL.templates_list(ctx)
    return {"years": years, "profile_ready": ready, "profile_gaps": gaps, "last_entered": last_entered, "open_batches": open_batches, "open_runs": open_runs,
            "templates": templates, "sorts": SORTS, "deliveries": DELIVERIES, "minimum": ctx.settings.get("statement_min_total") or 0}


# ── The preview list ────────────────────────────────────────────────────────────────────────────
def _sort_key(item: dict, sort: str):
    d = item["donor"]
    rec = item["record"]["person"]
    name_key = ((rec.get("last_name") or rec.get("org_name") or "").lower(), (rec.get("first_name") or "").lower(), item["person_id"])
    if sort == "zip":
        return (item["zip"] or "~", name_key)
    if sort == "envelope":
        env = d.get("envelope_number")
        return (0 if env else 1, int(env) if str(env or "").isdigit() else 10**9, str(env or ""), name_key)
    return name_key


def plan_run(ctx: Ctx, year, *, template_id: int | None = None, delivery: str = "all", sort: str = "name", name_prefix: str = "",
             minimum=None, today: dt.date | None = None) -> dict:
    """The preview list for a year. Returns {run_id, planned, skipped, excluded, total_deductible, reasons: [(reason, n)], warnings}."""
    _need(ctx)
    try:
        yr = int(year)
    except (TypeError, ValueError):
        raise InvalidInput("Pick a year.", "year")
    if not (1990 <= yr <= 2100):
        raise InvalidInput("That year is not one statements can be built for.", "year")
    if delivery not in DELIVERIES:
        raise InvalidInput("Pick all, print only or email only.", "delivery")
    if sort not in SORTS:
        raise InvalidInput("Pick a sort order.", "sort")
    prefix = (name_prefix or "").strip().lower()
    try:
        min_override = None if minimum in (None, "") else SR.money(minimum)
    except Exception:
        raise InvalidInput("The minimum has to be an amount.", "minimum")
    if min_override is not None and min_override < SR.ZERO:
        raise InvalidInput("The minimum cannot be negative.", "minimum")
    today = today or dt.date.today()
    p_from, p_to = SR.year_period(yr, today)
    settings = _settings(ctx, min_override)
    with tx() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE, ctx.parish_id))     # two people planning at once take turns
        ready, gaps = SP.profile_ready(c, ctx.parish_id)
        if not ready:
            raise InvalidInput("Confirm the parish profile before running statements: " + "; ".join(gaps) + ".", "profile")
        c.execute("SELECT id FROM donor.stmt_run WHERE parish_id = %s AND period_from = %s AND period_to = %s AND status IN ('planned', 'building') LIMIT 1",
                  (ctx.parish_id, p_from, p_to))
        open_run = c.fetchone()
        if open_run:
            raise Conflict(f"Run #{open_run['id']} for that year is not finished. Finish it or cancel it first.")
        tpl = TPL.resolve(c, ctx.parish_id, template_id)
        if template_id is not None and (tpl["template"] or {}).get("id") != int(template_id):
            raise NotFound("That template was not found at this parish.")
        gifts_by_person = _bulk_gifts(c, ctx.parish_id, p_from, p_to)
        c.execute("SELECT person_id, spouse_person_id FROM donor.stmt_statement WHERE parish_id = %s AND period_from = %s AND period_to = %s "
                  " AND kind = 'original' AND status IN ('planned', 'built', 'failed')", (ctx.parish_id, p_from, p_to))
        have, in_joint = set(), set()
        for r in c.fetchall():
            have.add(r["person_id"])
            if r["spouse_person_id"]:
                in_joint.add(r["spouse_person_id"])
        items: list[dict] = []
        skipped_rows: list[dict] = []
        excluded = 0
        handled: set = set()
        for pid in sorted(gifts_by_person):
            if pid in handled:
                continue
            first = SD.donor_record(c, ctx.parish_id, pid)
            if not first:
                continue
            role = SR.joint_role(first)
            partner_id = None
            lead_id = pid
            if role["role"] == "joint_follower":
                if role["partner_id"] in gifts_by_person:
                    # the lead has gifts too: the lead's own pass builds the pair and records this person as included
                    continue
                role = {"role": "joint_lead", "partner_id": role["partner_id"], "warnings": role["warnings"]}
            if role["role"] == "joint_lead":
                partner_id = role["partner_id"]
                handled.add(partner_id)
            if pid in have or pid in in_joint or (partner_id and (partner_id in have or partner_id in in_joint)):
                skipped_rows.append({"person_id": pid, "reason": "already has a statement for this period", "spouse": None})
                continue
            comp = _compose(c, ctx.parish_id, lead_id, partner_id, p_from, p_to, settings, bulk=gifts_by_person)
            decision = SR.decide(comp["facts"], comp["statement"], settings, bulk=True)
            if role["warnings"]:
                decision["warnings"] = list(decision["warnings"]) + role["warnings"]
            surname = (comp["record"]["person"].get("last_name") or comp["record"]["person"].get("org_name") or "").lower()
            if prefix and not surname.startswith(prefix):
                excluded += 1
                continue
            if delivery != "all" and comp["facts"]["delivery"] != delivery:
                excluded += 1
                continue
            item = {"person_id": lead_id, "spouse_id": partner_id if comp["partner_record"] else None, "comp": comp, "decision": decision, "record": comp["record"],
                    "donor": comp["donor"], "zip": comp["zip"]}
            items.append(item)
            if partner_id and comp["partner_record"] and partner_id in gifts_by_person:
                handled.add(partner_id)
        items.sort(key=lambda it: _sort_key(it, sort))
        c.execute("INSERT INTO donor.stmt_run (parish_id, period_from, period_to, template_id, template_version_id, params, status, created_by_user_id) "
                  "VALUES (%s, %s, %s, %s, %s, %s, 'planned', %s) RETURNING id",
                  (ctx.parish_id, p_from, p_to, (tpl["template"] or {}).get("id"), tpl.get("version_id"),
                   Jsonb({"year": yr, "delivery": delivery, "sort": sort, "name_prefix": prefix, "minimum": str(settings.get("statement_min_total") or 0),
                          "excluded": excluded, "template_source": tpl["source"]}), ctx.user_id))
        run_id = c.fetchone()["id"]
        planned = skipped = 0
        total = SR.ZERO
        warnings: set = set()
        reasons: dict = {}
        for it in items:
            comp, decision = it["comp"], it["decision"]
            st = comp["statement"]
            warnings.update(decision["warnings"])
            if decision["build"]:
                c.execute("INSERT INTO donor.stmt_statement (run_id, parish_id, person_id, spouse_person_id, period_from, period_to, status, deductible_total, gift_count, "
                          " gifts_fingerprint, delivery, template_version_id) VALUES (%s, %s, %s, %s, %s, %s, 'planned', %s, %s, %s, %s, %s)",
                          (run_id, ctx.parish_id, it["person_id"], it["spouse_id"], p_from, p_to, st["deductible_total"], st["gift_count"], st["fingerprint"],
                           comp["facts"]["delivery"], tpl.get("version_id")))
                planned += 1
                total += st["deductible_total"]
            else:
                c.execute("INSERT INTO donor.stmt_statement (run_id, parish_id, person_id, spouse_person_id, period_from, period_to, status, skip_reason, deductible_total, gift_count, "
                          " gifts_fingerprint, delivery, template_version_id) VALUES (%s, %s, %s, %s, %s, %s, 'skipped', %s, %s, %s, %s, %s, %s)",
                          (run_id, ctx.parish_id, it["person_id"], it["spouse_id"], p_from, p_to, decision["reason"], st["deductible_total"], st["gift_count"], st["fingerprint"],
                           comp["facts"]["delivery"], tpl.get("version_id")))
                skipped += 1
                reasons[decision["reason"]] = reasons.get(decision["reason"], 0) + 1
            if it["spouse_id"]:
                lead_name = comp["donor"]["name"]
                c.execute("INSERT INTO donor.stmt_statement (run_id, parish_id, person_id, period_from, period_to, status, skip_reason, delivery, template_version_id) "
                          "VALUES (%s, %s, %s, %s, %s, 'skipped', %s, %s, %s)",
                          (run_id, ctx.parish_id, it["spouse_id"], p_from, p_to, f"included in {lead_name}'s joint statement", comp["facts"]["delivery"], tpl.get("version_id")))
                skipped += 1
                reasons["included in a joint statement"] = reasons.get("included in a joint statement", 0) + 1
        for row in skipped_rows:
            c.execute("INSERT INTO donor.stmt_statement (run_id, parish_id, person_id, period_from, period_to, status, skip_reason, template_version_id) "
                      "VALUES (%s, %s, %s, %s, %s, 'skipped', %s, %s)", (run_id, ctx.parish_id, row["person_id"], p_from, p_to, row["reason"], tpl.get("version_id")))
            skipped += 1
            reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
        c.execute("UPDATE donor.stmt_run SET planned_count = %s, skipped_count = %s, total_deductible = %s WHERE id = %s", (planned, skipped, total, run_id))
        _event(c, ctx, "run_planned", detail={"run_id": run_id, "year": yr, "planned": planned, "skipped": skipped})
    return {"run_id": run_id, "year": yr, "planned": planned, "skipped": skipped, "excluded": excluded, "total_deductible": total,
            "reasons": sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])), "warnings": sorted(warnings),
            "template_name": (tpl["template"] or {}).get("name") or "Built-in Standard", "template_version": tpl.get("version_no")}


# ── Reading a run ───────────────────────────────────────────────────────────────────────────────
def get_run(ctx: Ctx, run_id) -> dict:
    _need(ctx)
    with tx() as c:
        return _run(c, ctx, run_id)


def _counts(c, run_id: int) -> dict:
    c.execute("SELECT status, COUNT(*) AS n, COALESCE(SUM(deductible_total) FILTER (WHERE status IN ('planned', 'built')), 0) AS total "
              "  FROM donor.stmt_statement WHERE run_id = %s GROUP BY status", (run_id,))
    out = {"planned": 0, "built": 0, "failed": 0, "skipped": 0, "replaced": 0, "total": SR.ZERO}
    for r in c.fetchall():
        out[r["status"]] = r["n"]
        out["total"] += r["total"]
    return out


def run_summary(ctx: Ctx, run_id) -> dict:
    _need(ctx)
    with tx() as c:
        r = _run(c, ctx, run_id)
        counts = _counts(c, r["id"])
        c.execute("SELECT CASE WHEN skip_reason LIKE 'included in %%' THEN 'included in a joint statement' ELSE skip_reason END AS reason, COUNT(*) AS n "
                  "  FROM donor.stmt_statement WHERE run_id = %s AND status = 'skipped' GROUP BY 1 ORDER BY n DESC, 1", (r["id"],))
        reasons = [(x["reason"], x["n"]) for x in c.fetchall()]
        c.execute("SELECT t.name AS template_name, v.version_no FROM donor.stmt_template_version v JOIN donor.stmt_template t ON t.id = v.template_id WHERE v.id = %s",
                  (r["template_version_id"],))
        tv = c.fetchone() or {}
    return {"run": r, "counts": counts, "reasons": reasons, "template_name": tv.get("template_name") or "Built-in Standard", "template_version": tv.get("version_no"),
            "remaining": counts["planned"], "done": r["status"] == "done"}


def history(ctx: Ctx, *, limit: int = 50) -> list[dict]:
    _need(ctx)
    with tx() as c:
        c.execute("SELECT r.*, t.name AS template_name, v.version_no FROM donor.stmt_run r LEFT JOIN donor.stmt_template_version v ON v.id = r.template_version_id "
                  "  LEFT JOIN donor.stmt_template t ON t.id = v.template_id WHERE r.parish_id = %s ORDER BY r.id DESC LIMIT %s", (ctx.parish_id, int(limit)))
        return c.fetchall()


def _person_label(p: dict) -> str:
    return SR.donor_name(p)


def list_statements(ctx: Ctx, run_id, *, status: str | None = None, limit: int = 200, offset: int = 0) -> list[dict]:
    """The rows of a run, in print order, each with the donor's name."""
    _need(ctx)
    with tx() as c:
        r = _run(c, ctx, run_id)
        params: list = [r["id"]]
        where = ""
        if status:
            where = " AND s.status = %s"
            params.append(status)
        params += [int(limit), int(offset)]
        c.execute("SELECT s.id, s.person_id, s.spouse_person_id, s.status, s.skip_reason, s.error, s.deductible_total, s.gift_count, s.delivery, s.file_path IS NOT NULL AS has_file, "
                  "       p.record_type, p.org_name, p.first_name, p.middle_name, p.last_name, p.suffix "
                  "  FROM donor.stmt_statement s JOIN donor.person p ON p.id = s.person_id WHERE s.run_id = %s" + where + " ORDER BY s.id LIMIT %s OFFSET %s", params)
        rows = c.fetchall()
    for x in rows:
        x["name"] = _person_label(x)
    return rows


# ── Building, a few at a time ───────────────────────────────────────────────────────────────────
def _default_html_for(sheet_ctx: dict) -> str:
    import statement_pdf as SPDF
    return SPDF.document_html(SPDF.sheet_html(sheet_ctx))


def _default_render(docs: list[str]) -> list[bytes]:
    import statement_pdf as SPDF
    return SPDF.render_many(docs)


def _default_put(path: str, data: bytes) -> str:
    import statement_store as SST
    return SST.put(path, data)


def _default_logo(parish_id: int):
    import statement_pdf as SPDF
    return SPDF.logo_data_uri(parish_id)


def _short_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return re.sub(r"\s+", " ", text)[:200]


def build_chunk(ctx: Ctx, run_id, *, limit: int = CHUNK, today: dt.date | None = None, render=None, put=None, logo_loader=None, html_for=None) -> dict:
    """Build the next planned statements of a run. Returns the progress: {run_id, status, planned, built, failed, skipped, remaining, done, busy, built_now, failed_now}.
    `busy` is True when another chunk of the same run is running (nothing was built); `renderer_busy` when the PDF renderer could not be had (nothing was marked failed)."""
    _need(ctx)
    render = render or _default_render
    put = put or _default_put
    logo_loader = logo_loader or _default_logo
    html_for = html_for or _default_html_for
    today = today or dt.date.today()
    limit = max(1, min(int(limit), 40))
    with tx() as lock:
        lock.execute("SELECT pg_try_advisory_xact_lock(%s, %s) AS ok", (LOCK_NAMESPACE + 1, int(run_id) % 2147483647))
        if not lock.fetchone()["ok"]:
            return {"run_id": int(run_id), "busy": True}
        # the lock is held until this block ends; the work below uses its own short transactions so progress is saved as it goes
        with tx() as c:
            r = _run(c, ctx, run_id)
            if r["status"] not in OPEN_RUN:
                return _progress(c, r, 0, 0, busy=False)
            if r["status"] == "planned":
                c.execute("UPDATE donor.stmt_run SET status = 'building' WHERE id = %s", (r["id"],))
            parts = _version_parts(c, ctx.parish_id, r["template_version_id"])
            c.execute("SELECT * FROM donor.parish_settings WHERE parish_id = %s", (ctx.parish_id,))
            ps = c.fetchone() or {}
            settings = _settings(ctx, ps.get("statement_min_total"))
            settings["statement_include_nondeductible"] = bool(ps.get("statement_include_nondeductible"))
            settings["statement_show_check_numbers"] = ps.get("statement_show_check_numbers") is not False
            profile = SP._row(c, ctx.parish_id)
            c.execute("SELECT id, person_id, spouse_person_id FROM donor.stmt_statement WHERE run_id = %s AND status = 'planned' ORDER BY id LIMIT %s", (r["id"], limit))
            todo = c.fetchall()
        logo = None
        try:
            logo = logo_loader(ctx.parish_id)
        except Exception:
            logo = None
        prepared: list[dict] = []
        failed_now = 0
        with tx() as c:
            for row in todo:
                try:
                    comp = _compose(c, ctx.parish_id, row["person_id"], row["spouse_person_id"], r["period_from"], r["period_to"], settings)
                    st = comp["statement"]
                    if not st["has_reportable"]:
                        prepared.append({"row": row, "skip": "no gifts to report at the time of the build", "comp": comp})
                        continue
                    sheet = RND.sheet_context(parts, parish_name=ctx.parish_name, profile=profile, donor=comp["donor"], statement=st, period_from=r["period_from"],
                                              period_to=r["period_to"], recorded_as_of=today, today=today, settings=settings, logo_src=logo)
                    prepared.append({"row": row, "comp": comp, "html": html_for(sheet)})
                except Exception as exc:        # one bad statement never stops the rest
                    prepared.append({"row": row, "error": _short_error(exc)})
        to_render = [p for p in prepared if "html" in p]
        pdfs: list = []
        if to_render:
            try:
                pdfs = render([p["html"] for p in to_render])
            except RuntimeError as exc:
                if "busy" in str(exc).lower():
                    with tx() as c:
                        return dict(_progress(c, _run(c, ctx, run_id), 0, 0, busy=False), renderer_busy=True)
                pdfs = _render_one_by_one(to_render, render)
            except Exception:
                pdfs = _render_one_by_one(to_render, render)
        built_now = 0
        by_row = {id(p): pdf for p, pdf in zip(to_render, pdfs)}
        for p in prepared:
            row = p["row"]
            if "skip" in p:
                with tx() as c:
                    c.execute("UPDATE donor.stmt_statement SET status = 'skipped', skip_reason = %s WHERE id = %s AND status = 'planned'", (p["skip"], row["id"]))
                continue
            if "error" in p or isinstance(by_row.get(id(p)), Exception) or by_row.get(id(p)) is None:
                message = p.get("error") or (_short_error(by_row[id(p)]) if isinstance(by_row.get(id(p)), Exception) else "The statement could not be rendered.")
                with tx() as c:
                    c.execute("UPDATE donor.stmt_statement SET status = 'failed', error = %s WHERE id = %s AND status = 'planned'", (message, row["id"]))
                failed_now += 1
                continue
            st = p["comp"]["statement"]
            try:
                import statement_store as SST
                path = SST.path_for(ctx.parish_id, r["id"], row["id"])
                sha = put(path, by_row[id(p)])
            except Exception as exc:
                with tx() as c:
                    c.execute("UPDATE donor.stmt_statement SET status = 'failed', error = %s WHERE id = %s AND status = 'planned'", (_short_error(exc), row["id"]))
                failed_now += 1
                continue
            with tx() as c:
                c.execute("UPDATE donor.stmt_statement SET status = 'built', error = NULL, deductible_total = %s, gift_count = %s, gifts_fingerprint = %s, "
                          " file_path = %s, file_sha256 = %s, built_at = NOW() WHERE id = %s AND status = 'planned'",
                          (st["deductible_total"], st["gift_count"], st["fingerprint"], path, sha, row["id"]))
            built_now += 1
        with tx() as c:
            r2 = _run(c, ctx, run_id)
            prog = _progress(c, r2, built_now, failed_now, busy=False, finish=True)
            if built_now or failed_now:
                _event(c, ctx, "built", detail={"run_id": r2["id"], "built": built_now, "failed": failed_now})
        return prog


def _render_one_by_one(items: list[dict], render) -> list:
    """A whole batch failed in the browser: try each alone so one bad statement does not fail its neighbours. A statement that still fails comes back as its exception."""
    out: list = []
    for it in items:
        try:
            out.append(render([it["html"]])[0])
        except Exception as exc:
            out.append(exc)
    return out


def _progress(c, r: dict, built_now: int, failed_now: int, *, busy: bool, finish: bool = False) -> dict:
    counts = _counts(c, r["id"])
    status = r["status"]
    if finish and status in OPEN_RUN and counts["planned"] == 0:
        status = "done"
        c.execute("UPDATE donor.stmt_run SET status = 'done', finished_at = NOW() WHERE id = %s AND status IN ('planned', 'building')", (r["id"],))
    if finish or status == "done":
        c.execute("UPDATE donor.stmt_run SET planned_count = %s, built_count = %s, skipped_count = %s, failed_count = %s, total_deductible = %s WHERE id = %s",
                  (counts["planned"] + counts["built"] + counts["failed"], counts["built"], counts["skipped"], counts["failed"],
                   counts["total"], r["id"]))
    return {"run_id": r["id"], "status": status, "busy": busy, "built": counts["built"], "failed": counts["failed"], "skipped": counts["skipped"],
            "planned": counts["planned"], "remaining": counts["planned"], "done": status == "done", "built_now": built_now, "failed_now": failed_now}


def retry_failed(ctx: Ctx, run_id) -> int:
    """Put the failed statements of a run back to planned (the next chunk builds them). Returns how many."""
    _need(ctx)
    with tx() as c:
        r = _run(c, ctx, run_id)
        if r["status"] == "cancelled":
            raise Conflict("That run was cancelled.")
        c.execute("UPDATE donor.stmt_statement SET status = 'planned', error = NULL WHERE run_id = %s AND status = 'failed'", (r["id"],))
        n = c.rowcount
        if n and r["status"] == "done":
            c.execute("UPDATE donor.stmt_run SET status = 'building', finished_at = NULL WHERE id = %s", (r["id"],))
        return n


def cancel_run(ctx: Ctx, run_id) -> dict:
    """Stop a run that is not finished. Statements already built stay (they are real and downloadable); the ones still waiting are marked skipped."""
    _need(ctx)
    with tx() as c:
        r = _run(c, ctx, run_id)
        if r["status"] not in OPEN_RUN:
            raise Conflict("Only a run that is not finished can be cancelled.")
        c.execute("UPDATE donor.stmt_statement SET status = 'skipped', skip_reason = 'the run was cancelled' WHERE run_id = %s AND status IN ('planned', 'failed')", (r["id"],))
        c.execute("UPDATE donor.stmt_run SET status = 'cancelled', finished_at = NOW() WHERE id = %s", (r["id"],))
        counts = _counts(c, r["id"])
        c.execute("UPDATE donor.stmt_run SET built_count = %s, skipped_count = %s, failed_count = 0 WHERE id = %s", (counts["built"], counts["skipped"], r["id"]))
        _event(c, ctx, "run_cancelled", detail={"run_id": r["id"], "built": counts["built"]})
        return {"run_id": r["id"], "built": counts["built"], "skipped": counts["skipped"]}


# ── Downloads (each writes one audit line; nothing is sent) ─────────────────────────────────────
def _file_name(row: dict, year: int) -> str:
    base = re.sub(r"[^A-Za-z0-9._ -]+", "", _person_label(row)).strip().replace("  ", " ") or f"Donor {row['person_id']}"
    return f"{base} - {year} - {row['person_id']}.pdf"


def statement_file(ctx: Ctx, statement_id, *, get=None) -> tuple[bytes, str]:
    """One built statement's PDF and its file name."""
    _need(ctx)
    try:
        sid = int(statement_id)
    except (TypeError, ValueError):
        raise NotFound("That statement was not found.")
    with tx() as c:
        c.execute("SELECT s.id, s.person_id, s.file_path, s.period_to, s.status, p.record_type, p.org_name, p.first_name, p.middle_name, p.last_name, p.suffix "
                  "  FROM donor.stmt_statement s JOIN donor.person p ON p.id = s.person_id WHERE s.id = %s AND s.parish_id = %s", (sid, ctx.parish_id))
        row = c.fetchone()
        if not row or not row["file_path"]:
            raise NotFound("That statement was not found.")
        _event(c, ctx, "staff_download", person_id=row["person_id"], statement_id=sid, detail={"kind": "single"})
    if get is None:
        import statement_store as SST
        get = SST.get
    data = get(row["file_path"])
    if data is None:
        raise NotFound("That statement's file could not be found.")
    return data, _file_name(row, row["period_to"].year)


def _built_rows(c, ctx: Ctx, run_id) -> tuple[dict, list[dict]]:
    r = _run(c, ctx, run_id)
    c.execute("SELECT s.id, s.person_id, s.delivery, s.file_path, p.record_type, p.org_name, p.first_name, p.middle_name, p.last_name, p.suffix "
              "  FROM donor.stmt_statement s JOIN donor.person p ON p.id = s.person_id WHERE s.run_id = %s AND s.status = 'built' AND s.file_path IS NOT NULL ORDER BY s.id", (r["id"],))
    return r, c.fetchall()


def print_pdf(ctx: Ctx, run_id, *, get=None) -> tuple[bytes, str]:
    """ONE PDF of every built statement meant for print, in the run's order. Assembled when asked, not stored."""
    _need(ctx)
    from pypdf import PdfReader, PdfWriter
    with tx() as c:
        r, rows = _built_rows(c, ctx, run_id)
        rows = [x for x in rows if x["delivery"] == "print"]
        _event(c, ctx, "staff_download", detail={"run_id": r["id"], "kind": "print_pdf", "count": len(rows)})
    if not rows:
        raise NotFound("No statements for print have been built in this run.")
    if get is None:
        import statement_store as SST
        get = SST.get
    writer = PdfWriter()
    for x in rows:
        data = get(x["file_path"])
        if data is None:
            continue
        for page in PdfReader(io.BytesIO(data)).pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), f"Statements to print - {r['period_to'].year}.pdf"


def zip_all(ctx: Ctx, run_id, *, get=None) -> tuple[bytes, str]:
    """A ZIP of every built statement, one PDF per donor, in a Print folder and an Email folder. Assembled when asked, not stored."""
    _need(ctx)
    with tx() as c:
        r, rows = _built_rows(c, ctx, run_id)
        _event(c, ctx, "staff_download", detail={"run_id": r["id"], "kind": "zip", "count": len(rows)})
    if not rows:
        raise NotFound("No statements have been built in this run.")
    if get is None:
        import statement_store as SST
        get = SST.get
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for x in rows:
            data = get(x["file_path"])
            if data is None:
                continue
            z.writestr(("Email/" if x["delivery"] == "email" else "Print/") + _file_name(x, r["period_to"].year), data)
    return buf.getvalue(), f"Statements - {r['period_to'].year}.zip"


def skipped_csv(ctx: Ctx, run_id) -> tuple[bytes, str]:
    """The skipped donors and why, as a spreadsheet file."""
    _need(ctx)
    with tx() as c:
        r = _run(c, ctx, run_id)
        c.execute("SELECT s.person_id, s.skip_reason, p.record_type, p.org_name, p.first_name, p.middle_name, p.last_name, p.suffix "
                  "  FROM donor.stmt_statement s JOIN donor.person p ON p.id = s.person_id WHERE s.run_id = %s AND s.status = 'skipped' "
                  " ORDER BY LOWER(COALESCE(p.last_name, p.org_name, '')), LOWER(COALESCE(p.first_name, ''))", (r["id"],))
        rows = c.fetchall()
        _event(c, ctx, "staff_download", detail={"run_id": r["id"], "kind": "skipped_csv", "count": len(rows)})
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Person id", "Name", "Why no statement"])
    for x in rows:
        w.writerow([x["person_id"], _person_label(x), x["skip_reason"] or ""])
    return out.getvalue().encode("utf-8-sig"), f"Skipped - {r['period_to'].year}.csv"


# ── Changed since issued, and re-issue (Plan section 4c) ────────────────────────────────────────
def _live_settings(c, ctx: Ctx) -> dict:
    c.execute("SELECT * FROM donor.parish_settings WHERE parish_id = %s", (ctx.parish_id,))
    ps = c.fetchone() or {}
    s = _settings(ctx, ps.get("statement_min_total"))
    s["statement_include_nondeductible"] = bool(ps.get("statement_include_nondeductible"))
    s["statement_show_check_numbers"] = ps.get("statement_show_check_numbers") is not False
    return s


def changed_since_issued(ctx: Ctx, year=None) -> list[dict]:
    """The built statements whose gifts are no longer the gifts the statement printed (a late gift, a correction, a reopened deposit). Each statement is compared on its OWN
    period, by its fingerprint. Newest first. Nothing is changed."""
    _need(ctx)
    params: list = [ctx.parish_id]
    where = ""
    if year not in (None, ""):
        try:
            params.append(int(year))
        except (TypeError, ValueError):
            raise InvalidInput("Pick a year.", "year")
        where = " AND EXTRACT(YEAR FROM s.period_to) = %s"
    out: list[dict] = []
    with tx() as c:
        settings = _live_settings(c, ctx)
        c.execute("SELECT s.id, s.run_id, s.person_id, s.spouse_person_id, s.period_from, s.period_to, s.kind, s.deductible_total, s.gift_count, s.gifts_fingerprint, s.built_at "
                  "  FROM donor.stmt_statement s WHERE s.parish_id = %s AND s.status = 'built' AND s.kind IN ('original', 'corrected')" + where +
                  " ORDER BY s.id DESC", params)
        for s in c.fetchall():
            comp = _compose(c, ctx.parish_id, s["person_id"], s["spouse_person_id"], s["period_from"], s["period_to"], settings)
            st = comp["statement"]
            if st["fingerprint"] == s["gifts_fingerprint"]:
                continue
            out.append({"statement_id": s["id"], "run_id": s["run_id"], "person_id": s["person_id"], "name": comp["donor"]["name"], "period_from": s["period_from"],
                        "period_to": s["period_to"], "kind": s["kind"], "built_at": s["built_at"], "old_total": s["deductible_total"], "old_count": s["gift_count"],
                        "new_total": st["deductible_total"], "new_count": st["gift_count"], "can_reissue": bool(st["has_reportable"])})
    return out


def reissue(ctx: Ctx, statement_id, *, today: dt.date | None = None, render=None, put=None, logo_loader=None, html_for=None) -> dict:
    """Build a CORRECTED statement for one built statement whose gifts have changed. The old statement is kept (marked replaced, still downloadable); the new one says it is a
    corrected statement and the date it replaces. One at a time per statement (a lock); the new file and the change of status commit together or not at all."""
    _need(ctx)
    render = render or _default_render
    put = put or _default_put
    logo_loader = logo_loader or _default_logo
    html_for = html_for or _default_html_for
    today = today or dt.date.today()
    try:
        sid = int(statement_id)
    except (TypeError, ValueError):
        raise NotFound("That statement was not found.")
    with tx() as lock:
        lock.execute("SELECT pg_try_advisory_xact_lock(%s, %s) AS ok", (LOCK_NAMESPACE + 2, sid % 2147483647))
        if not lock.fetchone()["ok"]:
            raise Conflict("That statement is being re-issued right now.")
        with tx() as c:
            c.execute("SELECT * FROM donor.stmt_statement WHERE id = %s AND parish_id = %s", (sid, ctx.parish_id))
            old = c.fetchone()
            if not old:
                raise NotFound("That statement was not found.")
            if old["status"] != "built":
                raise Conflict("Only a built statement that is still current can be re-issued.")
            settings = _live_settings(c, ctx)
            comp = _compose(c, ctx.parish_id, old["person_id"], old["spouse_person_id"], old["period_from"], old["period_to"], settings)
            st = comp["statement"]
            if st["fingerprint"] == old["gifts_fingerprint"]:
                raise InvalidInput("Nothing has changed since this statement was built.", "statement")
            if not st["has_reportable"]:
                raise InvalidInput("The gifts on this statement no longer count, so there is nothing to put on a corrected one. Check the donor's gifts first.", "statement")
            parts = _version_parts(c, ctx.parish_id, old["template_version_id"])
            profile = SP._row(c, ctx.parish_id)
            replaces = old["built_at"].date() if old["built_at"] else today
        try:
            logo = logo_loader(ctx.parish_id)
        except Exception:
            logo = None
        sheet = RND.sheet_context(parts, parish_name=ctx.parish_name, profile=profile, donor=comp["donor"], statement=st, period_from=old["period_from"], period_to=old["period_to"],
                                  recorded_as_of=today, today=today, settings=settings, logo_src=logo)
        sheet["correction"] = {"issued": today, "replaces": replaces}
        try:
            pdf = render([html_for(sheet)])[0]
        except RuntimeError as exc:
            if "busy" in str(exc).lower():
                raise Conflict("The PDF builder is busy. Please try again in a minute.")
            raise InvalidInput("The corrected statement could not be built. Nothing was changed.", "statement")
        except Exception:
            raise InvalidInput("The corrected statement could not be built. Nothing was changed.", "statement")
        import statement_store as SST
        with tx() as c:
            c.execute("INSERT INTO donor.stmt_statement (run_id, parish_id, person_id, spouse_person_id, period_from, period_to, kind, corrects_statement_id, status, deductible_total, gift_count, "
                      " gifts_fingerprint, delivery, template_version_id, built_at) VALUES (%s, %s, %s, %s, %s, %s, 'corrected', %s, 'built', %s, %s, %s, %s, %s, NOW()) RETURNING id",
                      (old["run_id"], ctx.parish_id, old["person_id"], old["spouse_person_id"], old["period_from"], old["period_to"], old["id"], st["deductible_total"], st["gift_count"],
                       st["fingerprint"], comp["facts"]["delivery"], old["template_version_id"]))
            new_id = c.fetchone()["id"]
            path = SST.path_for(ctx.parish_id, old["run_id"] or 0, new_id)
            sha = put(path, pdf)
            c.execute("UPDATE donor.stmt_statement SET file_path = %s, file_sha256 = %s WHERE id = %s", (path, sha, new_id))
            c.execute("UPDATE donor.stmt_statement SET status = 'replaced' WHERE id = %s AND status = 'built'", (old["id"],))
            if c.rowcount != 1:
                raise Conflict("That statement was changed by someone else. Nothing was re-issued.")
            if old["run_id"]:
                counts = _counts(c, old["run_id"])
                c.execute("UPDATE donor.stmt_run SET built_count = %s WHERE id = %s", (counts["built"], old["run_id"]))
            _event(c, ctx, "reissued", person_id=old["person_id"], statement_id=new_id, detail={"replaces": old["id"], "run_id": old["run_id"]})
    return {"statement_id": new_id, "replaces": old["id"], "deductible_total": st["deductible_total"], "gift_count": st["gift_count"]}
