"""
statement_templates.py -- Beacon Donor Management: giving-statement TEMPLATES (Plan section 4a), versions, copies and each parish's default.

Two kinds of template, one table (donor.stmt_template):
  GLOBAL   made by Cornerstone, visible to every parish that uses People & Giving. Only a Cornerstone person who is also a Beacon Admin may change one
           (is_cornerstone_editor: holds beacon_admin AND cornerstone_employee), never a diocese's Beacon Admin: a global change touches every diocese.
  PARISH   owned by one parish. A parish gets one by COPYING a global template; the copy records which global template and version it came from and is
           never changed by anything the global owner does later (a newer global version is only OFFERED, "see what changed, copy the changes in").

A template's content is stored in immutable VERSIONS (donor.stmt_template_version: a save is a new version, nothing is updated in place), so every
statement can say which version made it. A template holds no HTML: its wording goes through statement_text (an allow-list cleaner and a fixed list of merge
fields) and its look is a few choices from an approved list.

Each parish has ONE default template: a single column on its settings row (statement_template_id), so two defaults cannot exist. It has to be one of the
PARISH's own templates (a copy). When a parish has none, ensure_default makes a copy of the Cornerstone Standard, marks it the default and records that it
was made automatically (Jay, decision 3).

Operations: templates_list, template_get, template_copy, template_save, template_retire, default_set, ensure_default, resolve (for the builders),
sample_data (made-up data for previews).
Capabilities: statements.run reads; statements.templates changes the parish's own; a global change needs is_cornerstone_editor. Giving has to be on.
"""
from __future__ import annotations

import copy as _copy

from psycopg.types.json import Jsonb

import statement_text as TXT
from donor_core import Ctx, InvalidInput, NotFound, PermissionDenied, clean_text, log_change, need_giving, tx

STANDARD_NAME = "Cornerstone Standard Year-End Statement"
ACCENTS = ("green", "navy", "burgundy", "gray")
SIZES = ("small", "normal")
TEXT_PARTS = ("notice", "opening", "closing", "signer", "questions")
TEXT_LABELS = {"notice": "Notice (a short note at the top right)", "opening": "Opening text", "closing": "Closing text (thank-you and the tax wording)",
               "signer": "Signer lines", "questions": "Questions line"}
MAX_VERSIONS_SHOWN = 20

# The Cornerstone Standard. The tax wording follows what TouchPoint's default statement carries; it is a DRAFT until the diocese's treasurer or counsel reads it
# (Plan decision 15).
DEFAULT_PARTS = {
    "letterhead": {"logo": True},
    "notice": "",
    "opening": "",
    "closing": ("Thank you for your generous support of {{parish_name}}.\n\n"
                "No goods or services were provided to you by {{parish_legal_name}} in connection with any contribution listed above, except where a value for goods "
                "or services is shown; any other value received consisted entirely of intangible religious benefits."),
    "signer": "With gratitude,\n\n{{signer_name}}\n{{signer_title}}",
    "questions": "Please review this list. If you find an error or have a question, please contact {{contact_email}}.",
    "sections": {"summary_by_fund": True, "gift_list": True, "check_numbers": True},
    "look": {"size": "normal", "accent": "green"},
}


# ── Who may change a global template ────────────────────────────────────────────────────────────
def is_cornerstone_editor(user_id: int) -> bool:
    """A Cornerstone person who is also a Beacon Admin. Tests replace this function (like the other role reads)."""
    try:
        import rbac
        return bool(rbac.user_has_role(user_id, "beacon_admin") and rbac.user_has_role(user_id, "cornerstone_employee"))
    except Exception:
        return False


# ── Parts: validate, clean, default ─────────────────────────────────────────────────────────────
def normalize_parts(parts_in: dict | None) -> dict:
    """The clean, complete parts dict for whatever was submitted. Wording is cleaned and its merge fields checked; unknown keys are dropped; a missing
    part takes the Standard's value. Raises InvalidInput with a plain message naming the part."""
    src = parts_in or {}
    out = _copy.deepcopy(DEFAULT_PARTS)
    out["letterhead"]["logo"] = bool((src.get("letterhead") or {}).get("logo", out["letterhead"]["logo"]))
    for k in TEXT_PARTS:
        if k in src:
            try:
                out[k] = TXT.clean_rich(src.get(k))
            except TXT.TextError as e:
                raise InvalidInput(f"{TEXT_LABELS[k]}: {e}", k)
        else:
            try:
                out[k] = TXT.clean_rich(out[k])
            except TXT.TextError:
                out[k] = ""
    sec = src.get("sections") or {}
    for k in ("summary_by_fund", "gift_list", "check_numbers"):
        out["sections"][k] = bool(sec.get(k, out["sections"][k]))
    look = src.get("look") or {}
    size, accent = look.get("size", out["look"]["size"]), look.get("accent", out["look"]["accent"])
    if size not in SIZES:
        raise InvalidInput("Pick a type size from the list.", "size")
    if accent not in ACCENTS:
        raise InvalidInput("Pick a colour from the list.", "accent")
    out["look"] = {"size": size, "accent": accent}
    return out


def parts_from_form(form) -> dict:
    """The parts a template edit form posted (text boxes and tick boxes)."""
    return {"letterhead": {"logo": form.get("logo") is not None}, **{k: form.get(k) or "" for k in TEXT_PARTS},
            "sections": {"summary_by_fund": form.get("summary_by_fund") is not None, "gift_list": form.get("gift_list") is not None, "check_numbers": form.get("check_numbers") is not None},
            "look": {"size": form.get("size") or "normal", "accent": form.get("accent") or "green"}}


def safe_parts(parts) -> dict:
    """Parts read back from the database, cleaned AGAIN and completed, so a row that was written some other way can never reach a PDF unclean. Never raises."""
    try:
        return normalize_parts(parts if isinstance(parts, dict) else None)
    except InvalidInput:
        base = _copy.deepcopy(DEFAULT_PARTS)
        for k in TEXT_PARTS:
            base[k] = TXT.clean_rich(base[k])
        return base


# ── Reads ───────────────────────────────────────────────────────────────────────────────────────
def _need_read(ctx: Ctx) -> None:
    need_giving(ctx, "statements.run", "You do not have permission to see the statement templates.")


def _need_change(ctx: Ctx) -> None:
    need_giving(ctx, "statements.templates", "Only Finance can change the parish's statement templates.")


def _latest_version(c, template_id: int) -> dict | None:
    c.execute("SELECT * FROM donor.stmt_template_version WHERE template_id = %s ORDER BY version_no DESC LIMIT 1", (template_id,))
    return c.fetchone()


def ensure_global_standard(c) -> dict:
    """The Cornerstone Standard global template, created (with its first version) the first time anyone needs it. Safe to call again and from two places at once."""
    c.execute("SELECT * FROM donor.stmt_template WHERE scope = 'global' AND LOWER(name) = LOWER(%s)", (STANDARD_NAME,))
    row = c.fetchone()
    if row is None:
        c.execute("INSERT INTO donor.stmt_template (scope, name, description, created_by_user_id) VALUES ('global', %s, %s, 0) ON CONFLICT DO NOTHING",
                  (STANDARD_NAME, "The year-end giving statement every parish can start from. Copy it to your parish to change the wording."))
        c.execute("SELECT * FROM donor.stmt_template WHERE scope = 'global' AND LOWER(name) = LOWER(%s)", (STANDARD_NAME,))
        row = c.fetchone()
    if _latest_version(c, row["id"]) is None:
        c.execute("INSERT INTO donor.stmt_template_version (template_id, version_no, parts, note, created_by_user_id) VALUES (%s, 1, %s, %s, 0) ON CONFLICT DO NOTHING",
                  (row["id"], Jsonb(normalize_parts(None)), "First version. The tax wording is a draft until the diocese reads it."))
    return row


def _visible(c, ctx: Ctx, template_id: int) -> dict:
    c.execute("SELECT * FROM donor.stmt_template WHERE id = %s AND (scope = 'global' OR parish_id = %s)", (template_id, ctx.parish_id))
    row = c.fetchone()
    if not row:
        raise NotFound("That template was not found.")
    return row


def templates_list(ctx: Ctx, *, include_retired: bool = False) -> dict:
    """{globals: [...], parish: [...], default_id}. Each row carries its latest version number and, for a parish copy, whether a newer global version exists."""
    _need_read(ctx)
    with tx() as c:
        ensure_global_standard(c)
        c.execute("SELECT t.*, (SELECT MAX(version_no) FROM donor.stmt_template_version v WHERE v.template_id = t.id) AS latest_version "
                  "  FROM donor.stmt_template t WHERE (t.scope = 'global' OR t.parish_id = %s)" + ("" if include_retired else " AND t.status = 'active'") +
                  " ORDER BY t.scope DESC, LOWER(t.name)", (ctx.parish_id,))
        rows = c.fetchall()
        c.execute("SELECT statement_template_id FROM donor.parish_settings WHERE parish_id = %s", (ctx.parish_id,))
        default_id = (c.fetchone() or {}).get("statement_template_id")
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        r["is_default"] = r["id"] == default_id
        g = by_id.get(r.get("copied_from_template_id"))
        r["newer_global_version"] = g["latest_version"] if g and r.get("copied_from_version_no") and g["latest_version"] > r["copied_from_version_no"] else None
        r["copied_from_name"] = g["name"] if g else None
    return {"globals": [r for r in rows if r["scope"] == "global"], "parish": [r for r in rows if r["scope"] == "parish"], "default_id": default_id}


def template_get(ctx: Ctx, template_id: int, *, version_no: int | None = None) -> dict:
    """The template with one version's parts (the latest unless asked), the version list, and what the caller may do with it."""
    _need_read(ctx)
    with tx() as c:
        t = _visible(c, ctx, int(template_id))
        c.execute("SELECT version_no, note, created_by_user_id, created_at FROM donor.stmt_template_version WHERE template_id = %s ORDER BY version_no DESC LIMIT %s", (t["id"], MAX_VERSIONS_SHOWN))
        versions = c.fetchall()
        if not versions:
            raise NotFound("That template has no versions yet.")
        c.execute("SELECT * FROM donor.stmt_template_version WHERE template_id = %s AND version_no = %s", (t["id"], int(version_no) if version_no else versions[0]["version_no"]))
        v = c.fetchone()
        if not v:
            raise NotFound("That version was not found.")
        c.execute("SELECT statement_template_id FROM donor.parish_settings WHERE parish_id = %s", (ctx.parish_id,))
        default_id = (c.fetchone() or {}).get("statement_template_id")
        newer = None
        if t.get("copied_from_template_id"):
            g = _latest_version(c, t["copied_from_template_id"])
            if g and t.get("copied_from_version_no") and g["version_no"] > t["copied_from_version_no"]:
                newer = g["version_no"]
    is_global = t["scope"] == "global"
    can_edit = is_cornerstone_editor(ctx.user_id) if is_global else ctx.can("statements.templates")
    return {"template": t, "version": v, "parts": safe_parts(v["parts"]), "versions": versions, "is_default": t["id"] == default_id, "newer_global_version": newer,
            "can_edit": bool(can_edit and t["status"] == "active"), "can_copy": bool(is_global and ctx.can("statements.templates")),
            "can_default": bool((not is_global) and ctx.can("statements.templates") and t["status"] == "active")}


def resolve(c, parish_id: int, template_id: int | None = None) -> dict:
    """What a builder needs to render with: {template, version_no, parts, source}. With no template given: the parish's default; failing that the global Standard's
    latest version; failing that the built-in Standard. Reads only (it never creates the parish's automatic copy: ensure_default does)."""
    if template_id is None:
        c.execute("SELECT statement_template_id FROM donor.parish_settings WHERE parish_id = %s", (parish_id,))
        template_id = (c.fetchone() or {}).get("statement_template_id")
    if template_id is not None:
        c.execute("SELECT * FROM donor.stmt_template WHERE id = %s AND (scope = 'global' OR parish_id = %s)", (template_id, parish_id))
        t = c.fetchone()
        v = _latest_version(c, t["id"]) if t else None
        if t and v:
            return {"template": t, "version_no": v["version_no"], "version_id": v["id"], "parts": safe_parts(v["parts"]), "source": "default" if t["scope"] == "parish" else "global"}
    c.execute("SELECT * FROM donor.stmt_template WHERE scope = 'global' AND LOWER(name) = LOWER(%s) AND status = 'active'", (STANDARD_NAME,))
    g = c.fetchone()
    v = _latest_version(c, g["id"]) if g else None
    if g and v:
        return {"template": g, "version_no": v["version_no"], "version_id": v["id"], "parts": safe_parts(v["parts"]), "source": "standard"}
    return {"template": None, "version_no": None, "version_id": None, "parts": safe_parts(None), "source": "built-in"}


# ── Writes ──────────────────────────────────────────────────────────────────────────────────────
def _copy_global_into_parish(c, parish_id: int, user_id: int, g: dict, name: str, *, automatic: bool) -> int:
    v = _latest_version(c, g["id"])
    c.execute("INSERT INTO donor.stmt_template (scope, parish_id, name, description, copied_from_template_id, copied_from_version_no, created_automatically, created_by_user_id) "
              "VALUES ('parish', %s, %s, %s, %s, %s, %s, %s) RETURNING id", (parish_id, name, g.get("description"), g["id"], v["version_no"], automatic, user_id))
    new_id = c.fetchone()["id"]
    c.execute("INSERT INTO donor.stmt_template_version (template_id, version_no, parts, note, created_by_user_id) VALUES (%s, 1, %s, %s, %s)",
              (new_id, Jsonb(safe_parts(v["parts"])), f"Copied from {g['name']} version {v['version_no']}." + (" Made automatically." if automatic else ""), user_id))
    return new_id


def _unique_name(c, parish_id: int, wanted: str) -> str:
    n, name = 1, wanted
    while True:
        c.execute("SELECT 1 AS x FROM donor.stmt_template WHERE scope = 'parish' AND parish_id = %s AND LOWER(name) = LOWER(%s)", (parish_id, name))
        if not c.fetchone():
            return name
        n += 1
        name = f"{wanted} ({n})"


def template_copy(ctx: Ctx, global_template_id: int, name: str | None = None) -> dict:
    """Copy a global template into this parish. The copy is the parish's own from then on."""
    _need_change(ctx)
    with tx() as c:
        g = _visible(c, ctx, int(global_template_id))
        if g["scope"] != "global":
            raise InvalidInput("Only a Cornerstone template can be copied to the parish.", "template")
        if g["status"] != "active":
            raise InvalidInput("That Cornerstone template is retired.", "template")
        wanted = clean_text(name, field="name", max_len=100) or f"{g['name']} (our copy)"
        new_id = _copy_global_into_parish(c, ctx.parish_id, ctx.user_id, g, _unique_name(c, ctx.parish_id, wanted), automatic=False)
        log_change(c, ctx, "stmt_template", new_id, "Template", None, f"copied from {g['name']}", kind="create", scope="parish")
    return {"id": new_id}


def template_save(ctx: Ctx, template_id: int, parts_in: dict, note: str | None = None, *, name: str | None = None, description: str | None = None) -> dict:
    """Save new wording as a NEW VERSION of a template (never an edit in place). A parish template needs statements.templates and belongs to this parish; a
    global template needs a Cornerstone editor. Returns the new version number."""
    need_giving(ctx, "statements.run", "You do not have permission to change statement templates.")
    parts = normalize_parts(parts_in)
    note = clean_text(note, field="note", max_len=200)
    with tx() as c:
        t = _visible(c, ctx, int(template_id))
        c.execute("SELECT * FROM donor.stmt_template WHERE id = %s FOR UPDATE", (t["id"],))
        t = c.fetchone()
        if t["scope"] == "global":
            if not is_cornerstone_editor(ctx.user_id):
                raise PermissionDenied("Only Cornerstone can change a Cornerstone template. Copy it to your parish to change the wording.")
        else:
            _need_change(ctx)
        if t["status"] != "active":
            raise InvalidInput("A retired template cannot be changed.", "template")
        new_name = clean_text(name, field="name", max_len=100) if name is not None else None
        if new_name and new_name != t["name"]:
            c.execute("SELECT 1 AS x FROM donor.stmt_template WHERE id <> %s AND LOWER(name) = LOWER(%s) AND ((scope = 'global' AND %s = 'global') OR (scope = 'parish' AND parish_id = %s))",
                      (t["id"], new_name, t["scope"], t["parish_id"]))
            if c.fetchone():
                raise InvalidInput("A template with that name already exists.", "name")
            c.execute("UPDATE donor.stmt_template SET name = %s WHERE id = %s", (new_name, t["id"]))
            log_change(c, ctx, "stmt_template", t["id"], "Name", t["name"], new_name, kind="update", scope="parish")
        if description is not None:
            c.execute("UPDATE donor.stmt_template SET description = %s WHERE id = %s", (clean_text(description, field="description", max_len=300), t["id"]))
        last = _latest_version(c, t["id"])
        number = (last["version_no"] if last else 0) + 1
        c.execute("INSERT INTO donor.stmt_template_version (template_id, version_no, parts, note, created_by_user_id) VALUES (%s, %s, %s, %s, %s)", (t["id"], number, Jsonb(parts), note, ctx.user_id))
        log_change(c, ctx, "stmt_template", t["id"], "Version", number - 1 if number > 1 else None, number, kind="update", scope="parish")
    return {"id": t["id"], "version_no": number}


def template_retire(ctx: Ctx, template_id: int, retire: bool = True) -> dict:
    """Retire (or bring back) a template. It is never deleted. The parish's default cannot be retired."""
    with tx() as c:
        t = _visible(c, ctx, int(template_id))
        if t["scope"] == "global":
            if not is_cornerstone_editor(ctx.user_id):
                raise PermissionDenied("Only Cornerstone can retire a Cornerstone template.")
        else:
            _need_change(ctx)
        if retire:
            c.execute("SELECT 1 AS x FROM donor.parish_settings WHERE statement_template_id = %s", (t["id"],))
            if c.fetchone():
                raise InvalidInput("A template that is some parish's default cannot be retired. Choose another default first.", "template")
        c.execute("UPDATE donor.stmt_template SET status = %s WHERE id = %s", ("retired" if retire else "active", t["id"]))
        log_change(c, ctx, "stmt_template", t["id"], "Status", t["status"], "retired" if retire else "active", kind="update", scope="parish")
    return {"id": t["id"], "status": "retired" if retire else "active"}


def default_set(ctx: Ctx, template_id: int) -> dict:
    """Make one of the parish's own templates its default. It has to be the PARISH's copy, active."""
    _need_change(ctx)
    with tx() as c:
        t = _visible(c, ctx, int(template_id))
        if t["scope"] != "parish":
            raise InvalidInput("The default has to be one of the parish's own templates. Copy the Cornerstone template to the parish first.", "template")
        if t["status"] != "active":
            raise InvalidInput("A retired template cannot be the default.", "template")
        c.execute("INSERT INTO donor.parish_settings (parish_id) VALUES (%s) ON CONFLICT (parish_id) DO NOTHING", (ctx.parish_id,))
        c.execute("SELECT statement_template_id FROM donor.parish_settings WHERE parish_id = %s FOR UPDATE", (ctx.parish_id,))
        old = c.fetchone()["statement_template_id"]
        if old != t["id"]:
            c.execute("UPDATE donor.parish_settings SET statement_template_id = %s, updated_by_user_id = %s, updated_at = NOW() WHERE parish_id = %s", (t["id"], ctx.user_id, ctx.parish_id))
            log_change(c, ctx, "parish_settings", ctx.parish_id, "statement_template_id", old, t["id"], kind="update", scope="parish")
    return {"id": t["id"]}


def ensure_default(parish_id: int, user_id: int = 0, *, cur=None) -> dict:
    """The parish's default template, made automatically the first time one is needed: a copy of the Cornerstone Standard, marked the default and recorded as made
    automatically (never silently changed later). Callable for a member's click (user 0) as well as staff; it has no permission check of its own, so the CALLER decides
    who may trigger a statement. Returns the resolve() dict."""
    with tx(cur) as c:
        c.execute("INSERT INTO donor.parish_settings (parish_id) VALUES (%s) ON CONFLICT (parish_id) DO NOTHING", (parish_id,))
        c.execute("SELECT statement_template_id FROM donor.parish_settings WHERE parish_id = %s FOR UPDATE", (parish_id,))
        have = c.fetchone()["statement_template_id"]
        if have is None:
            g = ensure_global_standard(c)
            new_id = _copy_global_into_parish(c, parish_id, user_id, g, _unique_name(c, parish_id, f"{STANDARD_NAME} (our copy)"), automatic=True)
            c.execute("UPDATE donor.parish_settings SET statement_template_id = %s, updated_at = NOW() WHERE parish_id = %s", (new_id, parish_id))
            c.execute("INSERT INTO donor.change_log (table_name, row_id, person_id, field, old_value, new_value, kind, scope, user_id, parish_id, reason) "
                      "VALUES ('parish_settings', %s, NULL, 'statement_template_id', NULL, %s, 'update', 'parish', %s, %s, 'Default statement template made automatically')",
                      (parish_id, str(new_id), user_id, parish_id))
        return resolve(c, parish_id)


# ── Made-up data for previews (never a real person) ─────────────────────────────────────────────
def sample_data() -> dict:
    """An invented donor and four invented gifts, built into the code, for the template preview."""
    import datetime as dt
    from decimal import Decimal as D
    import statement_rules as SR
    y = dt.date.today().year - 1 if dt.date.today().month < 12 else dt.date.today().year
    gifts = []
    for i, (m, day, amt, fund, chk) in enumerate(((1, 14, "100.00", "General Fund", "1042"), (3, 2, "250.00", "General Fund", "1077"), (6, 9, "75.00", "Outreach", ""), (11, 20, "500.00", "Building Fund", "1150")), start=1):
        gifts.append({"id": 900000 + i, "gift_date": dt.date(y, m, day), "postmark_date": None, "gift_type": "tax_deductible", "status": "recorded", "check_number": chk,
                      "goods_value": D("0.00"), "amount": D(amt), "reverses_gift_id": None, "reclass_of_gift_id": None, "replaces_gift_id": None,
                      "in_kind_description": None, "stock_symbol": None, "stock_shares": None, "splits": [{"fund_id": 900 + i, "fund_name": fund, "amount": D(amt)}]})
    period = (dt.date(y, 1, 1), dt.date(y, 12, 31))
    stmt = SR.build_statement(gifts, *period)
    profile = {"legal_name": "Example Episcopal Church", "ein": "12-3456789", "address1": "100 Church Street", "address2": None, "city": "Exampleville", "state": "MD",
               "postal_code": "21000", "signer_name": "The Rev. Sample Person", "signer_title": "Rector", "contact_email": "office@example.org", "use_logo": False}
    return {"statement": stmt, "period_from": period[0], "period_to": period[1], "year": y, "profile": profile,
            "donor": {"person_id": 0, "name": "Pat Example", "address": ["12 Sample Street", "Exampleville, MD 21000"], "envelope_number": None},
            "recorded_as_of": dt.date(y + 1, 1, 15)}
