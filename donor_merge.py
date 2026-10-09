"""
donor_merge.py -- Beacon Donor Management: find likely duplicates, merge them field by field, keep a
merge history, and let "not a duplicate" stop a pair from coming back (PF-08, PF-20).

Who may merge: a merge changes a profile that other parishes rely on, so
  * a parish (people.edit) may merge two people only when NEITHER is connected to any other parish, and
  * a duplicate that spans parishes can be merged only by the diocese's Beacon Admin (ctx.is_diocesan_admin),
    and only for people whose connected parishes all belong to that diocese (the route passes the
    diocese's parish ids; the service re-checks them).

How a merge works (all in ONE transaction, nothing is deleted):
  * the survivor keeps its own profile values unless a field is blank (then the merged person's value fills
    the gap) or the caller chose the merged person's value for that field
  * contacts, household place, spouse link, parish connections, membership, sacramental records, transfer
    letters, notes, tasks, history, and (when the giving tables exist) gifts, pledges and soft credits all
    move to the survivor; a conflict (the same e-mail twice, two canonical memberships) resolves in favour
    of the survivor and is reported
  * the merged person's record stays, archived, with "merged into" in merge_history
"""
from __future__ import annotations

import json

import db
from donor_core import (
    Ctx, InvalidInput, NotFound, PermissionDenied, diff_fields, log_change, need_people, new_batch_id, person_label,
    tx,
)
from donor_people import EDITABLE_FIELDS, _person_row

_PHASE2_MOVES = (("gift", "person_id"), ("pledge", "person_id"), ("pledge", "joint_with_person_id"),
                 ("soft_credit", "person_id"))
_KIND_RANK = {"member": 3, "giver": 2, "visitor": 1}


def _table_exists(c, name: str) -> bool:
    c.execute("SELECT to_regclass(%s) AS t", (f"donor.{name}",))
    return c.fetchone()["t"] is not None


def _giving_rows(c, person_id: int) -> int:
    """Gifts, pledges and soft credits that name this person, in ANY parish. Merging would rewrite them (including gifts in
    closed batches, which are never edited), so a person who has any cannot be merged away."""
    n = 0
    for table, col in _PHASE2_MOVES:
        if _table_exists(c, table):
            c.execute(f"SELECT COUNT(*) AS n FROM donor.{table} WHERE {col} = %s", (person_id,))
            n += c.fetchone()["n"]
    return n


_GIVING_BLOCK = ("This person has giving records (gifts, pledges or soft credits). Merging would change closed gift records, which are "
                 "never edited, so this merge is not allowed. Keep both records, or have Finance correct the gifts first.")


def duplicate_candidates(ctx: Ctx, *, parish_ids: list[int] | None = None, limit: int = 200) -> list[dict]:
    """Likely duplicate pairs: same last name and same first name (or goes-by) with a matching or unknown
    birth date, or the same e-mail address, or the same organization name. Pairs already marked
    'not a duplicate' are left out. By default only people at THIS parish; the diocese passes its parish ids
    to look across parishes (Beacon Admin only)."""
    if parish_ids is not None:
        # The diocese's own duplicate search stands on the diocese-admin rule, not on a parish's donor roles.
        if not ctx.is_diocesan_admin:
            raise PermissionDenied("Only the diocese can look for duplicates across parishes.")
        if not ctx.settings.get("people_enabled"):
            raise PermissionDenied("People and membership records are not turned on for this parish yet.")
        scope_ids = sorted(set(parish_ids))
    else:
        need_people(ctx, "people.view")
        scope_ids = [ctx.parish_id]
    rows = db.query(
        """
        WITH mine AS (SELECT DISTINCT person_id FROM donor.parish_connection
                       WHERE parish_id = ANY(%s) AND archived_at IS NULL),
        pairs AS (
          SELECT p1.id AS a, p2.id AS b,
                 CASE WHEN p1.record_type = 'organization' THEN 'same organization name'
                      WHEN LOWER(p1.last_name) = LOWER(p2.last_name) AND LOWER(p1.first_name) = LOWER(p2.first_name)
                           THEN 'same name' ELSE 'same name (goes by)' END AS reason
            FROM donor.person p1 JOIN donor.person p2 ON p1.id < p2.id AND p1.record_type = p2.record_type
           WHERE p1.id IN (SELECT person_id FROM mine) AND p2.id IN (SELECT person_id FROM mine)
             AND NOT p1.is_placeholder AND NOT p2.is_placeholder AND p1.archived_at IS NULL AND p2.archived_at IS NULL
             AND ((p1.record_type = 'organization' AND LOWER(p1.org_name) = LOWER(p2.org_name))
               OR (p1.record_type = 'person' AND LOWER(p1.last_name) = LOWER(p2.last_name)
                   AND (LOWER(p1.first_name) = LOWER(p2.first_name)
                        OR LOWER(COALESCE(p1.goes_by, p1.first_name)) = LOWER(COALESCE(p2.goes_by, p2.first_name)))
                   AND (p1.birth_date IS NULL OR p2.birth_date IS NULL OR p1.birth_date = p2.birth_date)))
          UNION
          SELECT LEAST(c1.person_id, c2.person_id), GREATEST(c1.person_id, c2.person_id), 'same email'
            FROM donor.person_contact c1 JOIN donor.person_contact c2
              ON c1.kind = 'email' AND c2.kind = 'email' AND c1.person_id < c2.person_id AND LOWER(c1.value) = LOWER(c2.value)
           WHERE c1.archived_at IS NULL AND c2.archived_at IS NULL
             AND c1.person_id IN (SELECT person_id FROM mine) AND c2.person_id IN (SELECT person_id FROM mine)
        )
        SELECT DISTINCT ON (a, b) a, b, reason FROM pairs
         WHERE NOT EXISTS (SELECT 1 FROM donor.not_duplicate nd WHERE nd.person_a = pairs.a AND nd.person_b = pairs.b)
         ORDER BY a, b LIMIT %s
        """, (scope_ids, limit))
    out = []
    for r in rows:
        a, b = _person_row_safe(r["a"]), _person_row_safe(r["b"])
        out.append({"a": a, "b": b, "reason": r["reason"]})
    return out


def _person_row_safe(pid: int) -> dict:
    p = db.query_one("SELECT id, record_type, first_name, middle_name, last_name, org_name, birth_date FROM donor.person WHERE id = %s", (pid,))
    p["name"] = person_label(p)
    return p


def not_a_duplicate(ctx: Ctx, person_a: int, person_b: int, *, cur=None) -> dict:
    """Mark a pair as 'not a duplicate' so it stops reappearing. The caller must be connected to both (or be the
    diocese's Beacon Admin)."""
    need_people(ctx, "people.edit")
    if person_a == person_b:
        raise InvalidInput("Pick two different people.")
    a, b = sorted((person_a, person_b))
    with tx(cur) as c:
        if not ctx.is_diocesan_admin:
            for pid in (a, b):
                c.execute("SELECT 1 AS x FROM donor.parish_connection WHERE person_id = %s AND parish_id = %s", (pid, ctx.parish_id))
                if not c.fetchone():
                    raise NotFound("That person was not found at this parish.")
        c.execute("INSERT INTO donor.not_duplicate (person_a, person_b, marked_by_user_id, marked_by_parish_id) "
                  "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING person_a", (a, b, ctx.user_id, ctx.parish_id))
        created = c.fetchone() is not None
        if created:
            log_change(c, ctx, "not_duplicate", a, "pair", None, f"{a},{b}", person_id=a, kind="link")
        return {"person_a": a, "person_b": b, "created": created}


def _check_scope(c, ctx: Ctx, survivor_id: int, merged_id: int, diocese_parish_ids) -> None:
    """Who may merge these two: a parish only when NEITHER person is connected anywhere else; otherwise the
    diocese's Beacon Admin, and only for people connected solely to that diocese's parishes."""
    c.execute("SELECT DISTINCT parish_id FROM donor.parish_connection WHERE person_id = ANY(%s)", ([survivor_id, merged_id],))
    parishes = {r["parish_id"] for r in c.fetchall()}
    local_only = parishes <= {ctx.parish_id}
    if local_only:
        # Even a local merge needs both people to belong to this parish.
        for pid in (survivor_id, merged_id):
            c.execute("SELECT 1 AS x FROM donor.parish_connection WHERE person_id = %s AND parish_id = %s", (pid, ctx.parish_id))
            if not c.fetchone():
                raise NotFound("That person was not found at this parish.")
    else:
        if not ctx.is_diocesan_admin:
            raise PermissionDenied("These people are connected to more than one parish. Only the diocese can merge them.")
        if diocese_parish_ids is None or not parishes <= set(diocese_parish_ids):
            raise PermissionDenied("The diocese can only merge people who are connected to its own parishes.")


def merge_preview(ctx: Ctx, a_id: int, b_id: int, *, diocese_parish_ids: list[int] | None = None) -> dict:
    """Both people side by side for the merge screen, with counts of what would move. Same permission rule as
    the merge itself, so the preview never shows more than the merge may touch."""
    if not ctx.settings.get("people_enabled"):
        raise PermissionDenied("People and membership records are not turned on for this parish yet.")
    if not (ctx.is_diocesan_admin or ctx.can("people.edit")):
        raise PermissionDenied("You do not have permission to merge people.")
    if a_id == b_id:
        raise InvalidInput("Pick two different people to compare.")
    with tx() as c:
        c.execute("SELECT id FROM donor.person WHERE id = ANY(%s)", ([a_id, b_id],))
        if len(c.fetchall()) != 2:
            raise NotFound("One of those people was not found.")
        _check_scope(c, ctx, a_id, b_id, diocese_parish_ids)
        out = {}
        for key, pid in (("a", a_id), ("b", b_id)):
            row = _person_row(c, pid)
            c.execute("SELECT kind, subtype, value, is_preferred FROM donor.person_contact WHERE person_id = %s AND archived_at IS NULL ORDER BY kind, id", (pid,))
            row["contacts"] = c.fetchall()
            # What moves is shown only as far as the viewer could see those records anyway: this parish's own, and only the
            # kinds their role may open (a clergy-only note must not be hinted at by a count).
            counts = {}
            if ctx.can("sacrament.view"):
                c.execute("SELECT COUNT(*) AS n FROM donor.sacramental_event WHERE person_id = %s AND parish_id = %s", (pid, ctx.parish_id))
                counts["sacramental records"] = c.fetchone()["n"]
            if ctx.can("notes.staff"):
                c.execute("SELECT COUNT(*) AS n FROM donor.note WHERE person_id = %s AND parish_id = %s AND (visibility = 'staff' OR %s)",
                          (pid, ctx.parish_id, ctx.can("notes.clergy")))
                counts["notes"] = c.fetchone()["n"]
                c.execute("SELECT COUNT(*) AS n FROM donor.task WHERE person_id = %s AND parish_id = %s", (pid, ctx.parish_id))
                counts["tasks"] = c.fetchone()["n"]
            if ctx.can("sacrament.view"):
                c.execute("SELECT COUNT(*) AS n FROM donor.transfer_letter WHERE person_id = %s AND parish_id = %s", (pid, ctx.parish_id))
                counts["transfer letters"] = c.fetchone()["n"]
            row["counts"] = counts
            row["has_giving"] = _giving_rows(c, pid) > 0       # a yes/no only: the amounts are for Finance
            row["name"] = person_label(row)
            out[key] = row
        # Only the person merged AWAY has their giving rows rewritten, so a person with giving records can be the one kept
        # but never the one merged away. If both have giving there is no valid direction.
        out["blocked"] = _GIVING_BLOCK if (out["a"]["has_giving"] and out["b"]["has_giving"]) else None
        out["fields"] = [f for f in EDITABLE_FIELDS if out["a"]["record_type"] != "organization" or f in ("org_name", "in_directory", "hide_phone_in_directory", "do_not_mail", "do_not_call", "do_not_email")]
        return out


def person_merge(ctx: Ctx, survivor_id: int, merged_id: int, field_choices: dict | None = None, *,
                 diocese_parish_ids: list[int] | None = None, cur=None) -> dict:
    """Merge `merged_id` into `survivor_id`. See the module docstring for who may and what moves.
    field_choices: {profile_field: 'survivor' | 'merged'} (default: survivor, filling blanks from merged)."""
    if not ctx.settings.get("people_enabled"):
        raise PermissionDenied("People and membership records are not turned on for this parish yet.")
    if not (ctx.is_diocesan_admin or ctx.can("people.edit")):
        raise PermissionDenied("You do not have permission to merge people.")
    if survivor_id == merged_id:
        raise InvalidInput("Pick two different people to merge.")
    field_choices = {k: v for k, v in (field_choices or {}).items() if k in EDITABLE_FIELDS}
    for k, v in field_choices.items():
        if v not in ("survivor", "merged"):
            raise InvalidInput(f"Choose the survivor's value or the merged person's value for {k}.")
    with tx(cur) as c:
        c.execute("SELECT id FROM donor.person WHERE id = ANY(%s) ORDER BY id FOR UPDATE", ([survivor_id, merged_id],))
        if len(c.fetchall()) != 2:
            raise NotFound("One of those people was not found.")
        if _giving_rows(c, merged_id) > 0:
            raise InvalidInput(_GIVING_BLOCK)
        surv, mrg = _person_row(c, survivor_id), _person_row(c, merged_id)
        if surv["record_type"] != mrg["record_type"]:
            raise InvalidInput("A person cannot be merged with an organization.")
        if surv["is_placeholder"] or mrg["is_placeholder"]:
            raise InvalidInput("A placeholder donor (Open Plate, Anonymous) cannot be merged.")
        _check_scope(c, ctx, survivor_id, merged_id, diocese_parish_ids)

        moved: dict = {}
        notes: list[str] = []
        batch = new_batch_id()

        # 1. Profile fields
        new_vals: dict = {}
        for f in EDITABLE_FIELDS:
            sv, mv = surv.get(f), mrg.get(f)
            choice = field_choices.get(f)
            if choice == "merged":
                if mv is not None or f not in ("first_name", "last_name", "org_name"):
                    new_vals[f] = mv
            elif choice == "survivor":
                continue                                     # an explicit choice to keep the survivor's value, even if blank
            elif sv in (None, "") and mv not in (None, ""):
                new_vals[f] = mv                             # no choice made: fill a blank from the merged person
        if surv["databank_contact_id"] is None and mrg["databank_contact_id"] is not None:
            new_vals["databank_contact_id"] = mrg["databank_contact_id"]
        merged_final = {**surv, **new_vals}
        if merged_final.get("birth_date") and merged_final.get("deceased_date") and \
                merged_final["deceased_date"] < merged_final["birth_date"]:
            raise InvalidInput("The chosen values would put the death date before the birth date.")
        diffs = diff_fields(surv, new_vals)
        if diffs:
            c.execute(f"UPDATE donor.person SET {', '.join(f'{k} = %s' for k, _, _ in diffs)}, updated_at = NOW(), "
                      "updated_by_user_id = %s, updated_by_parish_id = %s WHERE id = %s",
                      (*[v for _, _, v in diffs], ctx.user_id, ctx.parish_id, survivor_id))
            for k, o, n in diffs:
                log_change(c, ctx, "person", survivor_id, k, o, n, person_id=survivor_id, kind="merge", batch_id=batch)
        moved["profile_fields"] = len(diffs)

        # 2. Contacts
        c.execute("SELECT * FROM donor.person_contact WHERE person_id = %s AND archived_at IS NULL ORDER BY kind, is_preferred DESC, id", (merged_id,))
        n_moved = n_dup = 0
        for ct in c.fetchall():
            c.execute("SELECT 1 AS x FROM donor.person_contact WHERE person_id = %s AND kind = %s AND archived_at IS NULL "
                      "AND LOWER(value) = LOWER(%s)", (survivor_id, ct["kind"], ct["value"]))
            if c.fetchone():
                c.execute("UPDATE donor.person_contact SET archived_at = NOW(), archived_by_user_id = %s, is_preferred = FALSE WHERE id = %s",
                          (ctx.user_id, ct["id"]))
                n_dup += 1
                continue
            c.execute("SELECT 1 AS x FROM donor.person_contact WHERE person_id = %s AND kind = %s AND is_preferred AND archived_at IS NULL",
                      (survivor_id, ct["kind"]))
            has_pref = c.fetchone() is not None
            c.execute("UPDATE donor.person_contact SET person_id = %s, is_preferred = %s WHERE id = %s",
                      (survivor_id, (not has_pref) and ct["is_preferred"], ct["id"]))
            n_moved += 1
        # any archived contacts keep their history with the survivor
        c.execute("UPDATE donor.person_contact SET person_id = %s, is_preferred = FALSE WHERE person_id = %s", (survivor_id, merged_id))
        c.execute("SELECT kind FROM donor.person_contact WHERE person_id = %s AND archived_at IS NULL GROUP BY kind "
                  "HAVING SUM(CASE WHEN is_preferred THEN 1 ELSE 0 END) = 0", (survivor_id,))
        for r in c.fetchall():
            c.execute("UPDATE donor.person_contact SET is_preferred = TRUE WHERE id = (SELECT id FROM donor.person_contact "
                      "WHERE person_id = %s AND kind = %s AND archived_at IS NULL ORDER BY id LIMIT 1)", (survivor_id, r["kind"]))
        moved["contacts"] = n_moved
        if n_dup:
            notes.append(f"{n_dup} contact detail(s) were already on the surviving record.")

        # 3. Household place
        c.execute("SELECT * FROM donor.household_member WHERE person_id = %s AND left_at IS NULL", (merged_id,))
        hm = c.fetchone()
        c.execute("SELECT 1 AS x FROM donor.household_member WHERE person_id = %s AND left_at IS NULL", (survivor_id,))
        survivor_has_household = c.fetchone() is not None
        if hm:
            if survivor_has_household:
                c.execute("UPDATE donor.household_member SET left_at = NOW(), is_primary_contact = FALSE WHERE id = %s", (hm["id"],))
                notes.append("The merged person's household place was dropped (the surviving record already has one).")
                moved["household"] = 0
            else:
                c.execute("UPDATE donor.household_member SET person_id = %s WHERE id = %s", (survivor_id, hm["id"]))
                moved["household"] = 1
        c.execute("UPDATE donor.household_member SET person_id = %s WHERE person_id = %s", (survivor_id, merged_id))

        # 4. Spouse link
        c.execute("SELECT * FROM donor.spouse_link WHERE person_id = %s AND ended_at IS NULL", (merged_id,))
        msl = c.fetchone()
        c.execute("SELECT * FROM donor.spouse_link WHERE person_id = %s AND ended_at IS NULL", (survivor_id,))
        ssl = c.fetchone()
        if msl:
            if msl["spouse_id"] == survivor_id:
                c.execute("UPDATE donor.spouse_link SET ended_at = NOW(), ended_by_user_id = %s, ended_reason = 'merged' "
                          "WHERE link_key = %s AND ended_at IS NULL", (ctx.user_id, msl["link_key"]))
                notes.append("The two people were linked as spouses. That link was ended by the merge.")
            elif ssl:
                c.execute("UPDATE donor.spouse_link SET ended_at = NOW(), ended_by_user_id = %s, ended_reason = 'merged' "
                          "WHERE link_key = %s AND ended_at IS NULL", (ctx.user_id, msl["link_key"]))
                notes.append("The merged person's spouse link was ended (the surviving record already has a spouse).")
            else:
                c.execute("UPDATE donor.spouse_link SET person_id = %s WHERE person_id = %s AND ended_at IS NULL", (survivor_id, merged_id))
                c.execute("UPDATE donor.spouse_link SET spouse_id = %s WHERE spouse_id = %s AND ended_at IS NULL", (survivor_id, merged_id))
                moved["spouse_link"] = 1
        # remaining (ended) rows: keep history pointing at the merged record unless that would make a self-link

        # 5. Parish connections and membership
        c.execute("SELECT * FROM donor.parish_connection WHERE person_id = %s ORDER BY id", (merged_id,))
        n_conn = 0
        for mc in c.fetchall():
            c.execute("SELECT * FROM donor.parish_connection WHERE person_id = %s AND parish_id = %s", (survivor_id, mc["parish_id"]))
            sc = c.fetchone()
            c.execute("SELECT 1 AS x FROM donor.parish_connection WHERE person_id = %s AND is_canonical AND archived_at IS NULL "
                      "AND parish_id <> %s", (survivor_id, mc["parish_id"]))
            canonical_elsewhere = c.fetchone() is not None
            if sc:
                kind = mc["kind"] if _KIND_RANK[mc["kind"]] > _KIND_RANK[sc["kind"]] else sc["kind"]
                env = sc["envelope_number"] or mc["envelope_number"]
                make_canon = bool(sc["is_canonical"] or (mc["is_canonical"] and not canonical_elsewhere and mc["archived_at"] is None))
                if make_canon:
                    kind = "member"
                live_merged = mc["archived_at"] is None
                c.execute("UPDATE donor.parish_connection SET archived_at = NOW(), archived_by_user_id = %s, is_canonical = FALSE, "
                          "updated_at = NOW() WHERE id = %s", (ctx.user_id, mc["id"]))
                archived = None if (sc["archived_at"] is None or live_merged) else sc["archived_at"]
                c.execute("UPDATE donor.parish_connection SET kind = %s, envelope_number = %s, is_canonical = %s, archived_at = %s, "
                          "updated_at = NOW() WHERE id = %s", (kind, env, make_canon, archived, sc["id"]))
            else:
                canon = bool(mc["is_canonical"] and not canonical_elsewhere)
                if mc["is_canonical"] and not canon:
                    notes.append("The merged person was a canonical member elsewhere too. Only one canonical membership is kept.")
                c.execute("UPDATE donor.parish_connection SET person_id = %s, is_canonical = %s, updated_at = NOW() WHERE id = %s",
                          (survivor_id, canon, mc["id"]))
            n_conn += 1
            # membership at that parish
            c.execute("SELECT * FROM donor.membership WHERE person_id = %s AND parish_id = %s", (merged_id, mc["parish_id"]))
            mm = c.fetchone()
            if mm:
                c.execute("SELECT * FROM donor.membership WHERE person_id = %s AND parish_id = %s", (survivor_id, mc["parish_id"]))
                sm = c.fetchone()
                if sm:
                    fill = {k: mm[k] for k in ("status_code_id", "how_joined", "join_date", "removal_date", "removal_reason")
                            if sm[k] is None and mm[k] is not None}
                    if sm["canonical_standing"] == "not_set" and mm["canonical_standing"] != "not_set":
                        fill.update({"canonical_standing": mm["canonical_standing"], "standing_reviewed_on": mm["standing_reviewed_on"],
                                     "standing_reviewed_by_user_id": mm["standing_reviewed_by_user_id"]})
                    if fill:
                        c.execute(f"UPDATE donor.membership SET {', '.join(f'{k} = %s' for k in fill)}, updated_at = NOW() WHERE id = %s",
                                  (*fill.values(), sm["id"]))
                    # the merged person's own membership row stays where it is, as history on the archived record
                else:
                    c.execute("UPDATE donor.membership SET person_id = %s WHERE id = %s", (survivor_id, mm["id"]))
        moved["connections"] = n_conn

        # 6. Everything else that hangs off a person
        for table, col in (("sacramental_event", "person_id"), ("sacramental_event", "related_person_id"),
                           ("transfer_letter", "person_id"), ("note", "person_id"), ("task", "person_id")):
            c.execute(f"UPDATE donor.{table} SET {col} = %s WHERE {col} = %s", (survivor_id, merged_id))
            moved[f"{table}.{col}"] = c.rowcount
        for table, col in _PHASE2_MOVES:
            if _table_exists(c, table):
                c.execute(f"UPDATE donor.{table} SET {col} = %s WHERE {col} = %s", (survivor_id, merged_id))
                moved[f"{table}.{col}"] = c.rowcount
        # change_log rows are never rewritten (the runtime role may only mark them undone). The merged person's
        # history stays under its own id and donor_changelog.changes_for_person reads it through merge_history.

        # 7. Close out the merged record (kept, archived) and write the history
        snap = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in mrg.items()}
        c.execute("UPDATE donor.person SET archived_at = NOW(), archived_by_user_id = %s, archive_reason = %s, updated_at = NOW() "
                  "WHERE id = %s", (ctx.user_id, f"Merged into person {survivor_id}", merged_id))
        c.execute("INSERT INTO donor.merge_history (survivor_person_id, merged_person_id, merged_by_user_id, merged_by_parish_id, "
                  "field_choices, moved_counts, merged_snapshot) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                  (survivor_id, merged_id, ctx.user_id, ctx.parish_id, json.dumps(field_choices), json.dumps(moved),
                   json.dumps(snap, default=str)))
        mh = c.fetchone()["id"]
        log_change(c, ctx, "person", survivor_id, None, f"person {merged_id}", "Merged in", person_id=survivor_id, kind="merge", batch_id=batch)
        log_change(c, ctx, "person", merged_id, "archived", None, f"Merged into person {survivor_id}", person_id=merged_id, kind="merge", batch_id=batch)
        return {"survivor_id": survivor_id, "merged_id": merged_id, "merge_history_id": mh, "moved": moved, "notes": notes}


def merge_history_for(ctx: Ctx, person_id: int) -> list[dict]:
    """Merges into this person (they were the survivor), visible to a parish connected to them."""
    need_people(ctx, "people.view")
    with tx() as c:
        c.execute("SELECT 1 AS x FROM donor.parish_connection WHERE person_id = %s AND parish_id = %s", (person_id, ctx.parish_id))
        if not c.fetchone():
            raise NotFound("That person was not found at this parish.")
        c.execute("SELECT id, merged_person_id, merged_by_user_id, merged_by_parish_id, merged_at, moved_counts "
                  "FROM donor.merge_history WHERE survivor_person_id = %s ORDER BY merged_at DESC", (person_id,))
        return c.fetchall()
