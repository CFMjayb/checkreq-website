"""
sma_store.py -- 26-129 SMA letters (plan revision 12, step 1): the database side of the SMA letters.

Everything that reads or writes portal.sma_letter_runs / sma_letters / sma_letter_versions lives here, so the
routes (sma_letters.py) stay thin and the rules are in one place:

  * create_run()        reads the uploaded Task Force model, matches its parishes to Beacon, computes every
                        letter's figures with Beacon's own formula, ties them to the model, pre-fills signers,
                        derives the flags
  * refresh_letter()    after ANY change re-derives a letter's figures, tie-out and flags from its current data
  * edit functions      figures, name, parish match, adjustment, signers, notes, exclude (each re-checks that the
                        letter belongs to this entity's run, and each writes a note saying who did what)
  * build_chunk()       builds letters in Formstack Documents a few at a time, under a per-run lock, never
                        spending more merges than the account has left
  * sync_templates()    pushes the two Word templates (sma_templates/) to Formstack Documents

Money is Decimal. The model file, the cover letter and every letter PDF live in Beacon's per-environment bucket
under sma-letters/{year}/run-{id}/. Nothing here sends an email (that is step 2).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from psycopg.types.json import Jsonb

import app_settings
import db
import formstack_documents_client as fs
import gcs_client
import sma_calc as C
import sma_flags
import sma_match
import sma_model
import sma_pdf
import sma_signers

log = logging.getLogger("beacon.sma_store")

BEACON_ENV = os.environ.get("BEACON_ENV", "dev")
BUCKET = "cfm-beacon-files-prod" if BEACON_ENV == "prod" else "cfm-beacon-files-dev"
ADMIN_ROLES = ("beacon_admin", "setup_admin")
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sma_templates")
SETTING_LETTER_DOC = "sma_letter_doc_id"
SETTING_SIGNING_DOC = "sma_signing_doc_id"
LETTER_FIELDS = {"Year", "PriorYear", "ParishName", "Y1", "Y2", "Y3", "NOIRatePct", "NOERatePct", "FlatDeduction", "NOI1",
                 "NOI2", "NOI3", "NOI2Note", "NOIAvg", "NOE", "A_Income", "A_Expense", "FormulaA", "B_Income", "B_Expense",
                 "FormulaB", "TotalAllocation", "MonthlyPayment", "AllocationQualifier", "AdjustmentNote",
                 "PriorAllocation", "Footnote"}
# Flags that must be settled before a letter is BUILT (the PDF prints these figures). Signer problems only block POSTING.
BUILD_BLOCKING = {"unmatched", "inactive_parish", "input_differs", "adjustment_needed", "tie_mismatch", "name_missing",
                  "build_failed"}
# A letter whose last build FAILED is skipped by the run's Build button (it would be merged again, and a merge that
# fails after Formstack answered has already used allowance). The per-letter "Build this letter now" retries it.
MAX_NAME_CHARS = 120
MAX_NOTE_CHARS = 1000
MAX_MONEY = Decimal("1000000000")
CALC_INPUTS = ("noi_y1", "noi_y2", "noi_y3", "noe")        # the figures the allocation is calculated from
RUN_OPEN = ("draft", "created")


# SMA letters are the Episcopal Diocese of Maryland's. The Administrative Tasks card is already limited to this entity;
# the routes enforce it too. None allows any entity (the tests use that; production never sets it).
ENTITY_CODES: tuple[str, ...] | None = ("EDOM",)


def entity_allowed(org: dict | None) -> bool:
    return bool(org) and (ENTITY_CODES is None or str(org.get("code") or "").upper() in ENTITY_CODES)


class SmaError(ValueError):
    """Something an admin has to fix. The message is shown to them."""


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------
def tables_ready() -> bool:
    try:
        row = db.query_one("SELECT to_regclass('portal.sma_letter_runs') IS NOT NULL AS ok")
        return bool(row and row["ok"])
    except Exception:
        return False


def clean(value, maxlen: int = 200) -> str:
    """Typed text -> a safe single line: NUL and control characters out, whitespace collapsed, capped."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", s).strip()[:maxlen]


def _money(value, what: str, *, allow_blank_zero: bool = True) -> Decimal:
    text = str(value if value is not None else "").replace(",", "").replace("$", "").strip()
    if text == "":
        if allow_blank_zero:
            return Decimal(0)
        raise SmaError(f"{what} is required.")
    try:
        d = Decimal(text)
    except InvalidOperation:
        raise SmaError(f"{what} must be a number.")
    if not d.is_finite():
        raise SmaError(f"{what} must be a number.")
    if d < 0 or d > MAX_MONEY:
        raise SmaError(f"{what} must be between 0 and {MAX_MONEY:,}.")
    return d.quantize(Decimal("0.01"))


def _rates(run: dict) -> C.Rates:
    return C.Rates(noi_rate=C.D(run["noi_rate"]), noe_rate=C.D(run["noe_rate"]), flat_deduction=C.D(run["flat_deduction"]),
                   lesser_of=bool(run["lesser_of"]), min_allocation=C.D((run.get("config") or {}).get("min_allocation", 0)))


def _path(run: dict, *parts: str) -> str:
    return "/".join(["sma-letters", str(run["year"]), f"run-{run['id']}", *parts])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _note(by: str, text: str, auto: bool = False) -> dict:
    return {"at": _now(), "by": clean(by, 120), "auto": bool(auto), "text": clean(text, MAX_NOTE_CHARS)}


def _default_letter_name(parish: dict | None, row_label: str) -> str:
    """Beacon's name for the parish, with its city when the name does not already carry it (several parishes are
    called 'Christ Episcopal Church'). Editable on the check sheet."""
    if not parish:
        return C.safe_text(row_label, MAX_NAME_CHARS)
    name, city = C.safe_text(parish.get("name"), 100), C.safe_text(parish.get("city"), 60)
    if city and city.lower() not in name.lower():
        return f"{name}, {city}"
    return name


def fields_for(run: dict, L: dict) -> dict[str, str]:
    """The merge data for this letter, from its stored figures."""
    return C.merge_fields(
        year=run["year"], prior_year=run["prior_year"], year1=run["year1"], year2=run["year2"], year3=run["year3"],
        letter_name=L["letter_name"], rates=_rates(run), noi_y1=L["noi_y1"], noi_y2=L["noi_y2"], noi_y3=L["noi_y3"],
        noe=L["noe"], prior_allocation=L["prior_allocation"],
        year2_estimated=bool((run.get("config") or {}).get("year2_estimated", True)),
        adjustment_kind=L.get("adjustment_kind") or "none", adjustment_text=L.get("adjustment_text") or "",
        custom_total=L.get("adjustment_amount"))


def current_hash(run: dict, L: dict) -> str:
    cover = (run.get("config") or {}).get("cover_sha256") or ""
    blob = json.dumps({"f": fields_for(run, L), "cover": cover}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _input_differs(c: C.Calc, row: dict) -> list[str]:
    out = []
    pairs = (("three-year average income", c.noi_avg, row.get("model_noi_avg")),
             ("latest-year income", C.D(row.get("noi_y3")), row.get("model_noi_latest")),
             ("operating expense", C.D(row.get("noe")), row.get("model_noe")))
    for label, ours, theirs in pairs:
        if theirs is not None and abs(ours - C.D(theirs)) > C.TIE_TOLERANCE:
            out.append(label)
    return out


# ---------------------------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------------------------
def get_run(org_id: int, run_id: int) -> dict | None:
    return db.query_one("SELECT * FROM portal.sma_letter_runs WHERE id = %s AND org_id = %s", (run_id, org_id))


def list_runs(org_id: int) -> list[dict]:
    return db.query(
        "SELECT r.*, "
        "  (SELECT count(*) FROM portal.sma_letters l WHERE l.run_id = r.id AND l.status <> 'excluded') AS n_letters, "
        "  (SELECT count(*) FROM portal.sma_letters l WHERE l.run_id = r.id AND l.status = 'created') AS n_built, "
        "  (SELECT count(*) FROM portal.sma_letters l WHERE l.run_id = r.id AND l.status = 'excluded') AS n_excluded, "
        "  (SELECT count(*) FROM portal.sma_letters l WHERE l.run_id = r.id AND l.status = 'created' AND NOT EXISTS "
        "     (SELECT 1 FROM jsonb_array_elements(l.flags) f WHERE f->>'severity' = 'block')) AS n_ready "
        "FROM portal.sma_letter_runs r WHERE r.org_id = %s ORDER BY r.created_at DESC, r.id DESC", (org_id,))


def list_letters(run_id: int) -> list[dict]:
    return db.query(
        "SELECT l.*, p.code AS parish_code, p.name AS parish_name, p.is_active AS parish_active "
        "FROM portal.sma_letters l LEFT JOIN portal.parishes p ON p.id = l.parish_id "
        "WHERE l.run_id = %s ORDER BY COALESCE(p.code, 'zzz'), l.model_name", (run_id,))


def get_letter(run_id: int, letter_id: int) -> dict | None:
    return db.query_one(
        "SELECT l.*, p.code AS parish_code, p.name AS parish_name, p.is_active AS parish_active "
        "FROM portal.sma_letters l LEFT JOIN portal.parishes p ON p.id = l.parish_id "
        "WHERE l.run_id = %s AND l.id = %s", (run_id, letter_id))


def list_versions(letter_id: int) -> list[dict]:
    return db.query("SELECT id, version, reason, pages, bytes, merge_mode, created_at FROM portal.sma_letter_versions "
                    "WHERE letter_id = %s ORDER BY version DESC", (letter_id,))


def candidate_parishes(org_id: int, run_id: int) -> list[dict]:
    """Beacon parishes a person can match a model row to: this entity's congregations not already used in this run."""
    return db.query(
        "SELECT p.id, p.code, p.name, p.city, p.is_active FROM portal.parishes p "
        "WHERE p.org_id = %s AND COALESCE(p.is_congregation, TRUE) AND NOT EXISTS "
        "  (SELECT 1 FROM portal.sma_letters l WHERE l.run_id = %s AND l.parish_id = p.id) "
        "ORDER BY p.is_active DESC, p.code, p.name", (org_id, run_id))


def summary(letters: list[dict]) -> dict:
    """Counts for the top of the check sheet."""
    out = {"total": 0, "excluded": 0, "built": 0, "ready": 0, "needs_attention": 0, "blocked": 0, "warn": 0}
    for L in letters:
        if L["status"] == "excluded":
            out["excluded"] += 1
            continue
        out["total"] += 1
        sev = {f["severity"] for f in (L.get("flags") or [])}
        out["built"] += 1 if L["status"] == "created" else 0
        out["blocked"] += 1 if sma_flags.BLOCK in sev else 0
        out["warn"] += 1 if sma_flags.WARN in sev and sma_flags.BLOCK not in sev else 0
        if L["status"] == "created" and sma_flags.BLOCK not in sev:
            out["ready"] += 1
    out["needs_attention"] = out["blocked"]
    return out


# ---------------------------------------------------------------------------------------------
# refreshing a letter's derived data (always inside the caller's transaction)
# ---------------------------------------------------------------------------------------------
def _refresh(cur, letter_id: int) -> None:
    cur.execute("SELECT l.*, p.is_active AS parish_active FROM portal.sma_letters l "
                "LEFT JOIN portal.parishes p ON p.id = l.parish_id WHERE l.id = %s FOR UPDATE OF l", (letter_id,))
    L = cur.fetchone()
    cur.execute("SELECT * FROM portal.sma_letter_runs WHERE id = %s", (L["run_id"],))
    run = cur.fetchone()
    rates = _rates(run)
    c = C.compute(L["noi_y1"], L["noi_y2"], L["noi_y3"], L["noe"], rates)
    kind = L["adjustment_kind"] or "none"
    final = C.apply_adjustment(c, kind, L["adjustment_amount"]) if (kind != "custom" or L["adjustment_amount"] is not None) else c.total
    tie = C.ties_to_model(c, L["model_formula_a"], L["model_formula_b"], L["model_total"], final_total=final)
    q = dict(L["quality"] or {})
    q["suggested_adjustment"] = C.suggest_adjustment(c, L["model_total"]) if kind == "none" else q.get("suggested_adjustment")
    q["current_hash"] = current_hash(run, dict(L, adjustment_kind=kind))
    values = dict(L, formula_total=c.total, total_allocation=final, tie_out=tie, quality=q, adjustment_kind=kind)
    flags = sma_flags.compute_flags(values)
    cur.execute(
        "UPDATE portal.sma_letters SET formula_a = %s, formula_b = %s, formula_total = %s, total_allocation = %s, "
        "monthly_payment = %s, tie_out = %s, quality = %s, flags = %s, updated_at = NOW() WHERE id = %s",
        (c.formula_a.quantize(C.CENT), c.formula_b.quantize(C.CENT), c.total, final, C.round_whole(Decimal(final) / 12), tie,
         Jsonb(q), Jsonb(flags), letter_id))


def _refresh_run(cur, run_id: int) -> None:
    cur.execute("SELECT id FROM portal.sma_letters WHERE run_id = %s", (run_id,))
    for r in cur.fetchall():
        _refresh(cur, r["id"])


# ---------------------------------------------------------------------------------------------
# creating a run from the uploaded model
# ---------------------------------------------------------------------------------------------
def create_run(org: dict, user_id: int, *, content: bytes, filename: str, run_type: str, title: str,
               test_address: str | None = None) -> int:
    if run_type not in ("test", "real"):
        raise SmaError("Choose whether this is a test run or a real run.")
    model = sma_model.read_model(content)          # raises ModelError, a ValueError with an admin-ready message
    years, rates = model["years"], model["rates"]
    parishes = db.query("SELECT id, code, name, pr_name, legal_name, city, parochial_report_id, is_active "
                        "FROM portal.parishes WHERE org_id = %s AND COALESCE(is_congregation, TRUE)", (org["id"],))
    by_id = {p["id"]: p for p in parishes}
    matches = sma_match.match_rows(model["rows"], parishes)
    cands = sma_signers.load_candidates([m["parish_id"] for m in matches if m["parish_id"]])
    not_in_model = sma_match.unmatched_active_parishes(matches, parishes)
    summary_json = {
        "model_file": clean(filename, 150), "assumptions": model["assumptions"], "model_warnings": model["warnings"][:50],
        "model_parishes": len(model["rows"]),
        "beacon_parishes_not_in_model": [{"code": p["code"], "name": p["name"]} for p in not_in_model][:60],
    }
    config = {"year2_estimated": True, "min_allocation": str(rates.min_allocation)}
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portal.sma_letter_runs (org_id, year, title, run_type, status, noi_rate, noe_rate, flat_deduction, "
                "lesser_of, year1, year2, year3, prior_year, figures_filename, figures_sha256, model_summary, test_address, "
                "config, created_by, letter_template_doc_id, signing_template_doc_id) "
                "VALUES (%s,%s,%s,%s,'draft',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (org["id"], years["target_year"], clean(title, 150) or f"{years['target_year']} Shared Ministry Allocation letters",
                 run_type, rates.noi_rate, rates.noe_rate, rates.flat_deduction, rates.lesser_of, years["year1"], years["year2"],
                 years["year3"], years["prior_year"], clean(filename, 150), hashlib.sha256(content).hexdigest(),
                 Jsonb(summary_json), clean(test_address, 200) or None, Jsonb(config), user_id,
                 app_settings.get_setting(SETTING_LETTER_DOC), app_settings.get_setting(SETTING_SIGNING_DOC)))
            run = cur.fetchone()
            path = _path(run, "model.xlsx")
            gcs_client.upload_bytes(BUCKET, path, content,
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            cur.execute("UPDATE portal.sma_letter_runs SET figures_path = %s WHERE id = %s", (path, run["id"]))
            for m in matches:
                row, pid = m["row"], m["parish_id"]
                parish = by_id.get(pid)
                c = C.compute(row["noi_y1"], row["noi_y2"], row["noi_y3"], row["noe"], rates)
                quality = {"noi": row["noi_quality"], "noe": row["noe_quality"], "filed": row["filed"], "edited": False,
                           "input_differs": _input_differs(c, row), "match_note": m["note"], "year3": years["year3"],
                           "blank": row.get("blank") or []}
                signers = cands.get(pid, []) if pid else []
                cur.execute(
                    "INSERT INTO portal.sma_letters (run_id, parish_id, ueid, model_name, model_city, letter_name, match_status, "
                    "noi_y1, noi_y2, noi_y3, noi_avg, noe, prior_allocation, model_formula_a, model_formula_b, model_total, "
                    "quality, signers, signers_source, notes) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (run["id"], pid, row["ueid"], clean(row["parish"], 150), clean(row["city"], 80),
                     _default_letter_name(parish, row["parish"]), m["status"], row["noi_y1"], row["noi_y2"], row["noi_y3"],
                     c.noi_avg.quantize(C.CENT), row["noe"], row["model_prior"] or 0, row["model_a"], row["model_b"],
                     row["model_total"], Jsonb(quality), Jsonb(signers), "beacon" if pid else None,
                     Jsonb([_note("Beacon", f"Loaded from the model file {clean(filename, 100)}.", True)])))
                _refresh(cur, cur.fetchone()["id"])
    return run["id"]


# ---------------------------------------------------------------------------------------------
# edits (each re-checks that the run is this entity's and still open)
# ---------------------------------------------------------------------------------------------
def _open_letter(cur, org_id: int, run_id: int, letter_id: int, *, allow_posted: bool = False):
    cur.execute("SELECT * FROM portal.sma_letter_runs WHERE id = %s AND org_id = %s FOR UPDATE", (run_id, org_id))
    run = cur.fetchone()
    if not run:
        raise SmaError("That run was not found.")
    if run["status"] not in RUN_OPEN and not allow_posted:
        raise SmaError("This run has already been posted or closed, so its letters can no longer be changed here.")
    cur.execute("SELECT * FROM portal.sma_letters WHERE id = %s AND run_id = %s FOR UPDATE", (letter_id, run_id))
    L = cur.fetchone()
    if not L:
        raise SmaError("That parish was not found in this run.")
    return run, L


def _append_note(cur, letter_id: int, by: str, text: str, auto: bool = False) -> None:
    cur.execute("UPDATE portal.sma_letters SET notes = notes || %s WHERE id = %s", (Jsonb([_note(by, text, auto)]), letter_id))


def add_note(org_id: int, run_id: int, letter_id: int, by: str, text: str) -> None:
    text = clean(text, MAX_NOTE_CHARS)
    if not text:
        raise SmaError("Type a note first.")
    with db.connect() as conn:
        with conn.cursor() as cur:
            _open_letter(cur, org_id, run_id, letter_id, allow_posted=True)
            _append_note(cur, letter_id, by, text)


def update_letter(org_id: int, run_id: int, letter_id: int, by: str, *, letter_name=None, figures: dict | None = None,
                  reason: str = "") -> None:
    """Edit the name printed on the letter and/or the figures it is calculated from. Changing a figure needs a reason,
    which goes in the notes with the before and after, and marks the letter as edited (so it no longer has to tie to
    the model). The PDF is then out of date until it is rebuilt."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            run, L = _open_letter(cur, org_id, run_id, letter_id)
            sets, notes = {}, []
            if letter_name is not None:
                name = C.safe_text(letter_name, MAX_NAME_CHARS)
                if not name:
                    raise SmaError("The parish name cannot be blank.")
                if name != L["letter_name"]:
                    sets["letter_name"] = name
                    notes.append(f'Letter name changed from "{L["letter_name"]}" to "{name}".')
            if figures:
                labels = {"noi_y1": f"NOI {run['year1']}", "noi_y2": f"NOI {run['year2']}", "noi_y3": f"NOI {run['year3']}",
                          "noe": f"Operating expense {run['year3']}", "prior_allocation": f"{run['prior_year']} allocation"}
                changed, calc_changed = [], False
                for key, label in labels.items():
                    if key in figures:
                        new = _money(figures[key], label)
                        if new != C.D(L[key]).quantize(Decimal("0.01")):
                            sets[key] = new
                            changed.append(f"{label} {C.D(L[key]):,.0f} to {new:,.0f}")
                            calc_changed = calc_changed or key in CALC_INPUTS
                if changed:
                    reason = clean(reason, 500)
                    if not reason:
                        raise SmaError("Say why the figures are being changed. It is recorded in the notes.")
                    notes.append("Figures edited: " + "; ".join(changed) + f". Reason: {reason}")
                    if calc_changed:
                        # Only the numbers the allocation is CALCULATED from release the tie-out to the model. Last year's
                        # allocation is printed for comparison but is not part of the calculation, so changing it must not
                        # clear a blocking flag or a confirmed adjustment.
                        q = dict(L["quality"] or {})
                        q["edited"] = True
                        q["input_differs"] = []     # a person is knowingly overriding the model's figures (reason in the notes)
                        sets["quality"] = Jsonb(q)
                        sets["adjustment_kind"], sets["adjustment_text"], sets["adjustment_amount"] = "none", "", None
                        notes.append("Any adjustment was cleared because the figures changed. Confirm it again if it still applies.")
            if not sets:
                return
            cur.execute("UPDATE portal.sma_letters SET " + ", ".join(f"{k} = %s" for k in sets) + " WHERE id = %s",
                        (*sets.values(), letter_id))
            if "noi_y1" in sets or "noi_y2" in sets or "noi_y3" in sets:
                cur.execute("UPDATE portal.sma_letters SET noi_avg = (noi_y1 + noi_y2 + noi_y3) / 3 WHERE id = %s", (letter_id,))
            for n in notes:
                _append_note(cur, letter_id, by, n, auto=True)
            cur.execute("UPDATE portal.sma_letters SET quality = quality - 'build_error' WHERE id = %s", (letter_id,))
            _refresh(cur, letter_id)


def set_parish(org_id: int, run_id: int, letter_id: int, by: str, parish_id: int | None) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            run, L = _open_letter(cur, org_id, run_id, letter_id)
            if parish_id is None:
                cur.execute("UPDATE portal.sma_letters SET parish_id = NULL, match_status = 'unmatched', signers = '[]', "
                            "signers_source = NULL WHERE id = %s", (letter_id,))
                _append_note(cur, letter_id, by, "Parish match removed.", True)
            else:
                cur.execute("SELECT id, name, city FROM portal.parishes WHERE id = %s AND org_id = %s AND "
                            "COALESCE(is_congregation, TRUE)", (parish_id, org_id))
                p = cur.fetchone()
                if not p:
                    raise SmaError("That parish does not belong to this entity.")
                cur.execute("SELECT 1 FROM portal.sma_letters WHERE run_id = %s AND parish_id = %s AND id <> %s",
                            (run_id, parish_id, letter_id))
                if cur.fetchone():
                    raise SmaError("That parish is already matched to another row of this run.")
                cands = sma_signers.load_candidates([parish_id]).get(parish_id, [])
                name = _default_letter_name(p, L["model_name"])
                cur.execute("UPDATE portal.sma_letters SET parish_id = %s, match_status = 'manual', signers = %s, "
                            "signers_source = 'beacon', letter_name = CASE WHEN match_status = 'unmatched' THEN %s ELSE letter_name END "
                            "WHERE id = %s", (parish_id, Jsonb(cands), name, letter_id))
                _append_note(cur, letter_id, by, f"Matched by hand to Beacon parish {clean(p['name'], 80)}. Signers were pre-filled from Beacon.", True)
            cur.execute("UPDATE portal.sma_letters SET quality = quality - 'build_error' WHERE id = %s", (letter_id,))
            _refresh(cur, letter_id)


def set_adjustment(org_id: int, run_id: int, letter_id: int, by: str, kind: str, text: str = "", amount=None) -> None:
    if kind not in C.ADJUSTMENT_KINDS:
        raise SmaError("Choose one of the listed adjustments.")
    with db.connect() as conn:
        with conn.cursor() as cur:
            run, L = _open_letter(cur, org_id, run_id, letter_id)
            words = C.safe_text(text, 400)
            if kind != L["adjustment_kind"] and words and words == (L["adjustment_text"] or ""):
                words = ""         # the sentence on the form is the OLD kind's. Do not let it ride along with the new kind.
            amt = None
            if kind == C.ADJ_CUSTOM:
                whole = _money(amount, "The amount to print", allow_blank_zero=False)
                if whole != whole.to_integral_value():
                    raise SmaError("The amount to print must be whole dollars, as the letter prints whole dollars.")
                amt = int(whole)
                if not words:
                    raise SmaError("Write the sentence the letter will print about this adjustment.")
            elif kind != C.ADJ_NONE and not words:
                words = C.DEFAULT_ADJUSTMENT_TEXT[kind]
            if kind == C.ADJ_NONE:
                words = ""
            cur.execute("UPDATE portal.sma_letters SET adjustment_kind = %s, adjustment_text = %s, adjustment_amount = %s "
                        "WHERE id = %s", (kind, words, amt, letter_id))
            what = "cleared" if kind == C.ADJ_NONE else f"set to {kind.replace('_', ' ')}" + (f" ({amt:,})" if amt is not None else "")
            _append_note(cur, letter_id, by, f"Adjustment {what}." + (f' The letter says: "{words}"' if words else ""), True)
            cur.execute("UPDATE portal.sma_letters SET quality = quality - 'build_error' WHERE id = %s", (letter_id,))
            _refresh(cur, letter_id)


def set_excluded(org_id: int, run_id: int, letter_id: int, by: str, excluded: bool, reason: str = "") -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            run, L = _open_letter(cur, org_id, run_id, letter_id)
            if excluded:
                cur.execute("UPDATE portal.sma_letters SET status = 'excluded' WHERE id = %s", (letter_id,))
                _append_note(cur, letter_id, by, "Excluded from this run." + (f" Reason: {clean(reason, 300)}" if clean(reason, 300) else ""), True)
            else:
                cur.execute("UPDATE portal.sma_letters SET status = %s WHERE id = %s",
                            ("created" if L["current_version"] > 0 else "draft", letter_id))
                _append_note(cur, letter_id, by, "Included in this run again.", True)
            _refresh(cur, letter_id)
            _sync_run_status(cur, run_id)


# --- signers -----------------------------------------------------------------------------------
def _valid_email(email: str) -> bool:
    return bool(re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", email)) and len(email) <= 200


def _save_signers(cur, letter: dict, new: list[dict], by: str, source: str = "edited") -> None:
    before = sma_signers.describe(letter["signers"] or [], emails=True)
    after = sma_signers.describe(new, emails=True)
    cur.execute("UPDATE portal.sma_letters SET signers = %s, signers_source = %s WHERE id = %s",
                (Jsonb(new), source, letter["id"]))
    if before != after:
        _append_note(cur, letter["id"], by, f"Signers changed. Was: {before}. Now: {after}.", True)
    _refresh(cur, letter["id"])


def choose_signer(org_id: int, run_id: int, letter_id: int, by: str, role: str, index: int) -> None:
    """Make the index-th signer record of this role the chosen one (the others of the role become alternates)."""
    if role not in sma_signers.ROLE_ORDER:
        raise SmaError("Unknown signer role.")
    with db.connect() as conn:
        with conn.cursor() as cur:
            _, L = _open_letter(cur, org_id, run_id, letter_id)
            signers = [dict(s) for s in (L["signers"] or [])]
            of_role = [s for s in signers if s.get("role") == role]
            if not 0 <= index < len(of_role):
                raise SmaError("That signer is not in the list.")
            for i, s in enumerate(of_role):
                s["chosen"] = i == index
            _save_signers(cur, L, signers, by)


def unchoose_role(org_id: int, run_id: int, letter_id: int, by: str, role: str) -> None:
    """No signer for this role (for a parish that has no such officer). The records stay as alternates."""
    if role not in sma_signers.ROLE_ORDER:
        raise SmaError("Unknown signer role.")
    with db.connect() as conn:
        with conn.cursor() as cur:
            _, L = _open_letter(cur, org_id, run_id, letter_id)
            signers = [dict(s, chosen=False) if s.get("role") == role else dict(s) for s in (L["signers"] or [])]
            _save_signers(cur, L, signers, by)


def add_signer(org_id: int, run_id: int, letter_id: int, by: str, role: str, name: str, email: str) -> None:
    role, name, email = role, clean(name, 120), clean(email, 200).lower()
    if role not in sma_signers.ROLE_ORDER:
        raise SmaError("Choose a signer role.")
    if not name or not _valid_email(email):
        raise SmaError("A signer needs a name and a valid email address.")
    with db.connect() as conn:
        with conn.cursor() as cur:
            _, L = _open_letter(cur, org_id, run_id, letter_id)
            signers = [dict(s) for s in (L["signers"] or [])]
            if len(signers) >= 12:
                raise SmaError("That is more signers than a parish can have.")
            if any(s.get("role") == role and (s.get("email") or "").lower() == email for s in signers):
                raise SmaError("That person is already listed for this role.")
            for s in signers:
                if s.get("role") == role:
                    s["chosen"] = False
            signers.append({"role": role, "name": name, "email": email, "user_id": None, "title": "", "source": "manual", "chosen": True})
            _save_signers(cur, L, signers, by)


def remove_signer(org_id: int, run_id: int, letter_id: int, by: str, index: int) -> None:
    """Remove a signer that someone typed in by hand. Pre-filled people from Beacon are never removed, only unchosen."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            _, L = _open_letter(cur, org_id, run_id, letter_id)
            signers = [dict(s) for s in (L["signers"] or [])]
            if not 0 <= index < len(signers) or signers[index].get("source") != "manual":
                raise SmaError("Only a signer added by hand can be removed.")
            del signers[index]
            _save_signers(cur, L, signers, by)


def reset_signers(org_id: int, run_id: int, letter_id: int, by: str) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            _, L = _open_letter(cur, org_id, run_id, letter_id)
            if not L["parish_id"]:
                raise SmaError("Match the parish first.")
            fresh = sma_signers.load_candidates([L["parish_id"]]).get(L["parish_id"], [])
            _save_signers(cur, L, fresh, by, source="beacon")


# --- run-level ----------------------------------------------------------------------------------
def confirm_rates(org_id: int, run_id: int, by: str) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM portal.sma_letter_runs WHERE id = %s AND org_id = %s FOR UPDATE", (run_id, org_id))
            run = cur.fetchone()
            if not run or run["status"] not in RUN_OPEN:
                raise SmaError("That run cannot be changed.")
            cfg = dict(run["config"] or {})
            cfg["rates_confirmed_at"], cfg["rates_confirmed_by"] = _now(), clean(by, 120)
            cur.execute("UPDATE portal.sma_letter_runs SET config = %s, updated_at = NOW() WHERE id = %s", (Jsonb(cfg), run_id))


def set_cover(org_id: int, run_id: int, content: bytes, filename: str) -> int:
    pages = sma_pdf.check_cover_letter(content)
    sma_pdf.can_join(content)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM portal.sma_letter_runs WHERE id = %s AND org_id = %s FOR UPDATE", (run_id, org_id))
            run = cur.fetchone()
            if not run or run["status"] not in RUN_OPEN:
                raise SmaError("That run cannot be changed.")
            sha = hashlib.sha256(content).hexdigest()
            path = _path(run, "cover.pdf")
            gcs_client.upload_bytes(BUCKET, path, content, "application/pdf")
            cfg = dict(run["config"] or {})
            cfg["cover_sha256"] = sha
            cur.execute("UPDATE portal.sma_letter_runs SET cover_path = %s, cover_filename = %s, cover_pages = %s, "
                        "cover_uploaded_at = NOW(), config = %s, updated_at = NOW() WHERE id = %s",
                        (path, clean(filename, 150), pages, Jsonb(cfg), run_id))
            cur.execute("UPDATE portal.sma_letters SET quality = quality - 'build_error' WHERE run_id = %s", (run_id,))
            _refresh_run(cur, run_id)
    return pages


def cover_bytes(run: dict) -> bytes | None:
    if not run.get("cover_path"):
        return None
    got = gcs_client.download_bytes(BUCKET, run["cover_path"])
    return got[0] if got else None


# ---------------------------------------------------------------------------------------------
# Formstack templates
# ---------------------------------------------------------------------------------------------
def _template_names() -> tuple[str, str]:
    env = "prod" if BEACON_ENV == "prod" else "dev"
    return f"SMA letter template ({env})", f"SMA signing form template ({env})"


def sync_templates(user_id: int | None = None) -> dict:
    """Create or update the two Formstack Documents templates from the Word files in sma_templates/, remember their
    ids per environment, and check that Formstack found every merge field the letter needs. Uses no merges."""
    out = {}
    for setting, filename, name in ((SETTING_LETTER_DOC, "SMA_Letter_template.docx", _template_names()[0]),
                                    (SETTING_SIGNING_DOC, "SMA_Signing_form_template.docx", _template_names()[1])):
        with open(os.path.join(TEMPLATE_DIR, filename), "rb") as f:
            docx = f.read()
        existing = app_settings.get_setting(setting)
        doc_id = None
        if existing:
            try:
                fs.get_document(existing)
            except fs.FormstackError as exc:
                # Only a document that is really GONE is recreated. A network blip or a Formstack error must not create
                # a duplicate (the plan allows 10 active documents) and leave the old one orphaned.
                if "404" not in str(exc) and "does not have that document" not in str(exc):
                    raise
            else:
                fs.update_document(existing, docx, name=name)
                doc_id = existing
        if not doc_id:
            doc_id = fs.create_document(name, docx, output_name="{$Year} SMA - {$ParishName}")["id"]
            app_settings.set_setting(setting, doc_id, user_id)
        out[setting] = doc_id
    found = set(fs.field_names(out[SETTING_LETTER_DOC]))
    missing = sorted(LETTER_FIELDS - found)
    if missing:
        raise SmaError("Formstack did not find these merge fields in the letter template: " + ", ".join(missing))
    return out


def template_ids() -> dict:
    return {"letter": app_settings.get_setting(SETTING_LETTER_DOC), "signing": app_settings.get_setting(SETTING_SIGNING_DOC)}


def attach_templates(org_id: int, run_id: int, ids: dict) -> None:
    """Point a run at the Formstack documents the templates were just sent to (only while it has none yet)."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE portal.sma_letter_runs SET letter_template_doc_id = COALESCE(letter_template_doc_id, %s), "
                        "signing_template_doc_id = COALESCE(signing_template_doc_id, %s), updated_at = NOW() "
                        "WHERE id = %s AND org_id = %s AND status IN ('draft', 'created')",
                        (ids.get(SETTING_LETTER_DOC), ids.get(SETTING_SIGNING_DOC), run_id, org_id))


# ---------------------------------------------------------------------------------------------
# building letters
# ---------------------------------------------------------------------------------------------
def _buildable(L: dict, *, allow_failed: bool = False) -> bool:
    """Nothing blocks a build. `allow_failed` lets the per-letter button retry a letter whose last build failed."""
    blocking = BUILD_BLOCKING - ({"build_failed"} if allow_failed else set())
    return L["status"] in ("draft", "created") and not any(f["code"] in blocking for f in (L.get("flags") or []))


def build_status(run: dict, letters: list[dict]) -> dict:
    """What the Build button needs to know: can it run, how many are waiting, and why not."""
    cfg = run.get("config") or {}
    waiting = [L for L in letters if L["status"] == "draft"]
    ready_to_build = [L for L in waiting if _buildable(L)]
    reasons = []
    if not cfg.get("rates_confirmed_at"):
        reasons.append("Confirm the rates first.")
    if not run.get("cover_path"):
        reasons.append("Upload the cover letter first.")
    if not (run.get("letter_template_doc_id") or app_settings.get_setting(SETTING_LETTER_DOC)):
        reasons.append("Send the templates to Formstack first.")
    return {"can_build": not reasons and bool(ready_to_build), "reasons": reasons, "waiting": len(waiting),
            "buildable": len(ready_to_build), "blocked": len(waiting) - len(ready_to_build)}


def _sync_run_status(cur, run_id: int) -> None:
    """A run is 'created' once every parish that is in it has a letter, and goes back to 'draft' if one is waiting
    again (an excluded parish does not count either way). Called after anything that can change that."""
    cur.execute("SELECT status FROM portal.sma_letter_runs WHERE id = %s FOR UPDATE", (run_id,))
    run = cur.fetchone()
    if not run or run["status"] not in RUN_OPEN:
        return
    cur.execute("SELECT count(*) FILTER (WHERE status = 'draft') AS drafts, count(*) FILTER (WHERE status = 'created') AS built "
                "FROM portal.sma_letters WHERE run_id = %s", (run_id,))
    c = cur.fetchone()
    new = "created" if c["drafts"] == 0 and c["built"] > 0 else "draft"
    cur.execute("UPDATE portal.sma_letter_runs SET status = %s, updated_at = NOW(), "
                "letters_created_at = CASE WHEN %s AND letters_created_at IS NULL THEN NOW() ELSE letters_created_at END "
                "WHERE id = %s", (new, new == "created", run_id))


def _bump_merges(cur, run_id: int, test: bool, n: int) -> None:
    """Add to the run's merge counter in ONE statement, so it cannot overwrite a change another request just made to the
    run's settings (the cover letter hash, the rates confirmation)."""
    if n <= 0:
        return
    key = "merges_test" if test else "merges_real"
    cur.execute("UPDATE portal.sma_letter_runs SET config = jsonb_set(config, %s::text[], "
                "to_jsonb(COALESCE((config->>%s)::int, 0) + %s)), updated_at = NOW() WHERE id = %s", ([key], key, n, run_id))


def _record_build_error(letter_id: int, message: str) -> None:
    """Remember that this letter's build failed, so the run's Build button does not merge it again and again."""
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT quality FROM portal.sma_letters WHERE id = %s FOR UPDATE", (letter_id,))
                row = cur.fetchone()
                if not row:
                    return
                q = dict(row["quality"] or {})
                q["build_error"] = clean(message, 300)
                cur.execute("UPDATE portal.sma_letters SET quality = %s WHERE id = %s", (Jsonb(q), letter_id))
                _refresh(cur, letter_id)
    except Exception as exc:                      # recording the failure must never hide the failure itself
        log.warning("could not record a build error: %s", type(exc).__name__)


USER_FACING = (fs.FormstackError, sma_pdf.PdfError, SmaError)


def _why(exc: Exception) -> str:
    """The text for an admin: a message written for them, or a generic line for anything unexpected (which is logged)."""
    if isinstance(exc, USER_FACING):
        return str(exc)
    log.warning("unexpected build failure: %s", type(exc).__name__, exc_info=True)
    return f"Unexpected error ({type(exc).__name__}). It was logged."


def _build_one(run: dict, L: dict, doc_id: str, cover: bytes, user_id: int | None, reason: str) -> None:
    test = run["run_type"] == "test"
    pdf = fs.merge(doc_id, fields_for(run, L), test=test)
    pages = sma_pdf.check_letter(pdf)
    joined = sma_pdf.join(cover, pdf, title=f"{run['year']} Shared Ministry Allocation - {L['letter_name']}")
    version = (L["current_version"] or 0) + 1
    key = L["ueid"] or f"parish-{L['parish_id']}"
    path = _path(run, "letters", f"{re.sub(r'[^0-9A-Za-z-]+', '-', key)}-v{version}.pdf")
    gcs_client.upload_bytes(BUCKET, path, joined, "application/pdf")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM portal.sma_letters WHERE id = %s FOR UPDATE", (L["id"],))
            cur_L = cur.fetchone()
            if cur_L["status"] == "excluded":
                raise SmaError("This parish was excluded while its letter was being built. Include it again and build it.")
            if (cur_L["current_version"] or 0) + 1 != version:
                raise SmaError("This letter was built by someone else while this build was running. Reload and check it.")
            q = dict(cur_L["quality"] or {})
            q["letter_pages"] = pages
            q.pop("build_error", None)
            # the hash of what was MERGED (the snapshot), not of whatever the row holds now: an edit made while Formstack
            # was working must leave this PDF marked out of date, never fresh
            q["built_hash"] = current_hash(run, L)
            cur.execute("INSERT INTO portal.sma_letter_versions (letter_id, version, reason, pdf_path, pages, bytes, sha256, "
                        "merge_mode, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (L["id"], version, clean(reason, 200) or "original", path, pages + (run.get("cover_pages") or 0),
                         len(joined), hashlib.sha256(joined).hexdigest(), run["run_type"], user_id))
            cur.execute("UPDATE portal.sma_letters SET current_version = %s, status = 'created', quality = %s WHERE id = %s",
                        (version, Jsonb(q), L["id"]))
            _refresh(cur, L["id"])


def build_chunk(org_id: int, run_id: int, user_id: int | None, *, limit: int = 6, seconds: float = 40.0) -> dict:
    """Build up to `limit` waiting letters (or stop after `seconds`). Safe to call again and again: a per-run lock
    stops two overlapping builds from merging the same letter twice (a merge costs real allowance). A letter whose
    build fails is set aside with its reason, so pressing Build again does not spend a merge on it every time."""
    run = get_run(org_id, run_id)
    if not run:
        raise SmaError("That run was not found.")
    if run["status"] not in RUN_OPEN:
        raise SmaError("This run is already posted or closed.")
    letters = list_letters(run_id)
    st = build_status(run, letters)
    if st["reasons"]:
        raise SmaError(" ".join(st["reasons"]))
    test = run["run_type"] == "test"
    todo = [L for L in letters if L["status"] == "draft" and _buildable(L)]
    result = {"built": 0, "failed": 0, "remaining": len(todo), "blocked": st["blocked"], "errors": [], "busy": False,
              "mode": run["run_type"]}
    if not todo:
        return result
    doc_id = run.get("letter_template_doc_id") or app_settings.get_setting(SETTING_LETTER_DOC)
    with db.connect() as lock_conn:
        with lock_conn.cursor() as lock:
            lock.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS got", (f"sma_build:{run_id}",))
            if not lock.fetchone()["got"]:
                result["busy"] = True
                return result
            try:
                # Re-read AFTER taking the lock: another build may have finished between the read above and now, and
                # merging the same letters again would spend real allowance and clash on the version number.
                todo = [L for L in list_letters(run_id) if L["status"] == "draft" and _buildable(L)]
                result["remaining"] = len(todo)
                if not todo:
                    return result
                batch = todo[:max(1, limit)]
                fs.ensure_allowance(len(batch), test)
                cover = cover_bytes(run)
                if cover is None:
                    raise SmaError("The cover letter file could not be read. Upload it again.")
                started = time.monotonic()
                for L in batch:
                    if time.monotonic() - started > seconds:
                        break
                    try:
                        _build_one(run, L, doc_id, cover, user_id, "original")
                        result["built"] += 1
                    except Exception as exc:
                        why = _why(exc)
                        result["failed"] += 1
                        result["errors"].append(f"{L['letter_name']}: {why}")
                        _record_build_error(L["id"], why)
                        if isinstance(exc, fs.FormstackError) and "credentials" in str(exc).lower():
                            break
            finally:
                lock.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"sma_build:{run_id}",))
    result["remaining"] = len([L for L in list_letters(run_id) if L["status"] == "draft" and _buildable(L)])
    with db.connect() as conn:
        with conn.cursor() as cur:
            _bump_merges(cur, run_id, test, result["built"])
            _sync_run_status(cur, run_id)
    return result


def rebuild_letter(org_id: int, run_id: int, letter_id: int, user_id: int | None, by: str, reason: str) -> None:
    """Build a NEW version of one parish's letter (after a figure, name, adjustment or cover-letter change), or build a
    letter for the first time, or retry one whose last build failed."""
    run = get_run(org_id, run_id)
    if not run or run["status"] not in RUN_OPEN:
        raise SmaError("That run cannot be changed.")
    L = get_letter(run_id, letter_id)
    if not L or L["status"] == "excluded":
        raise SmaError("That parish is not in this run.")
    if not _buildable(L, allow_failed=True):
        raise SmaError("Settle the flags that block building first (parish match, adjustment, figures).")
    st = build_status(run, [L])
    if st["reasons"]:
        raise SmaError(" ".join(st["reasons"]))
    cover = cover_bytes(run)
    if cover is None:
        raise SmaError("The cover letter file could not be read. Upload it again.")
    test = run["run_type"] == "test"
    fs.ensure_allowance(1, test)
    doc_id = run.get("letter_template_doc_id") or app_settings.get_setting(SETTING_LETTER_DOC)
    with db.connect() as lock_conn:
        with lock_conn.cursor() as lock:
            lock.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS got", (f"sma_build:{run_id}",))
            if not lock.fetchone()["got"]:
                raise SmaError("Another build of this run is still running. Try again in a minute.")
            try:
                L = get_letter(run_id, letter_id)             # fresh, under the lock
                if not L or L["status"] == "excluded" or not _buildable(L, allow_failed=True):
                    raise SmaError("Settle the flags that block building first (parish match, adjustment, figures).")
                try:
                    _build_one(run, L, doc_id, cover, user_id, clean(reason, 200) or "rebuilt")
                except Exception as exc:
                    why = _why(exc)
                    _record_build_error(letter_id, why)
                    raise SmaError(why)
            finally:
                lock.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"sma_build:{run_id}",))
    with db.connect() as conn:
        with conn.cursor() as cur:
            _append_note(cur, letter_id, by, f"Letter rebuilt as version {(L['current_version'] or 0) + 1}. Reason: {clean(reason, 200) or 'rebuilt'}", True)
            _bump_merges(cur, run_id, test, 1)
            _sync_run_status(cur, run_id)


def letter_pdf(org_id: int, run_id: int, letter_id: int, version: int | None = None) -> tuple[bytes, str] | None:
    """(bytes, filename) of a built letter, or None. Entity-scoped through the run."""
    run = get_run(org_id, run_id)
    if not run:
        return None
    L = get_letter(run_id, letter_id)
    if not L:
        return None
    row = db.query_one("SELECT pdf_path, version FROM portal.sma_letter_versions WHERE letter_id = %s AND version = %s",
                       (letter_id, version or L["current_version"]))
    if not row:
        return None
    got = gcs_client.download_bytes(BUCKET, row["pdf_path"])
    if not got:
        return None
    safe = re.sub(r"[^0-9A-Za-z ._-]+", "", f"{run['year']} SMA letter - {L['letter_name']} v{row['version']}")[:120].strip() or "letter"
    return got[0], safe + ".pdf"
