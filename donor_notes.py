"""
donor_notes.py -- Beacon Donor Management: notes and tasks about a person (SR-05, SR-06).

Parish-scoped. A note is limited by role: 'clergy' (pastoral notes) is readable and writable only by
clergy, 'staff' by anyone who can edit people. Notes are archived, never deleted. Tasks have an owner,
a due date and a status (pending, accepted, done, cancelled). Nothing is e-mailed here: the notice on
assignment and completion (SR-06) is a later step.

Operations: note_add  note_archive  notes_for_person  task_add  task_update  tasks_for_person  tasks_for_user
"""
from __future__ import annotations

import donor_roles
from donor_core import (
    Ctx, InvalidInput, NotFound, PermissionDenied, check_enum, clean_text, log_change, need_people, parse_date, tx,
    NOTE_VISIBILITIES, TASK_STATUSES,
)
from donor_people import require_connection


def _can_read_visibility(ctx: Ctx, visibility: str) -> bool:
    return ctx.can("notes.clergy") if visibility == "clergy" else ctx.can("notes.staff")


def note_add(ctx: Ctx, person_id: int, body: str, visibility: str = "staff", keywords: str | None = None, *, cur=None) -> dict:
    need_people(ctx, "notes.staff")
    vis = check_enum(visibility, NOTE_VISIBILITIES, field="who can read it", allow_blank=False)
    if vis == "clergy":
        ctx.require("notes.clergy", "Only clergy can write a clergy-only note.")
    text = clean_text(body, field="note", max_len=8000)
    if not text:
        raise InvalidInput("A note cannot be empty.", "body")
    with tx(cur) as c:
        require_connection(c, ctx, person_id, include_archived=False)
        c.execute("INSERT INTO donor.note (person_id, parish_id, visibility, keywords, body, author_user_id) "
                  "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                  (person_id, ctx.parish_id, vis, clean_text(keywords, field="keywords", max_len=200), text, ctx.user_id))
        nid = c.fetchone()["id"]
        # The change log records THAT a note was written, never its words (a pastoral note must not leak
        # through a history screen).
        log_change(c, ctx, "note", nid, vis, None, "note added", person_id=person_id, kind="create", scope="parish")
        return {"id": nid}


def notes_for_person(ctx: Ctx, person_id: int, include_archived: bool = False) -> list[dict]:
    """This parish's notes for the person that the caller's role may read. A clergy-only note is simply
    not returned to anyone else."""
    need_people(ctx, "notes.staff")
    with tx() as c:
        require_connection(c, ctx, person_id)
        c.execute("SELECT * FROM donor.note WHERE person_id = %s AND parish_id = %s"
                  + ("" if include_archived else " AND archived_at IS NULL")
                  + " AND (visibility = 'staff' OR %s) ORDER BY created_at DESC, id DESC",
                  (person_id, ctx.parish_id, ctx.can("notes.clergy")))
        return c.fetchall()


def note_archive(ctx: Ctx, note_id: int, *, cur=None) -> dict:
    need_people(ctx, "notes.staff")
    with tx(cur) as c:
        c.execute("SELECT * FROM donor.note WHERE id = %s AND parish_id = %s FOR UPDATE", (note_id, ctx.parish_id))
        n = c.fetchone()
        if not n or not _can_read_visibility(ctx, n["visibility"]):
            raise NotFound("That note was not found.")
        if n["archived_at"]:
            return {"id": note_id, "archived": True, "changed": False}
        c.execute("UPDATE donor.note SET archived_at = NOW(), archived_by_user_id = %s WHERE id = %s", (ctx.user_id, note_id))
        log_change(c, ctx, "note", note_id, "archived", None, "true", person_id=n["person_id"], kind="archive", scope="parish")
        return {"id": note_id, "archived": True, "changed": True}


# ── Tasks ───────────────────────────────────────────────────────────────────────────────────────
def _check_assignee(ctx: Ctx, user_id: int | None) -> None:
    if user_id is None:
        return
    if not donor_roles.user_exists(user_id) or not donor_roles.user_at_parish(user_id, ctx.parish_id):
        raise InvalidInput("A task can only be assigned to someone with a Beacon login at this parish.", "assigned_to")


def task_add(ctx: Ctx, person_id: int, title: str, details: str | None = None, due_date=None,
             assigned_to_user_id: int | None = None, *, cur=None) -> dict:
    need_people(ctx, "notes.staff")
    t = clean_text(title, field="title", max_len=200)
    if not t:
        raise InvalidInput("A task needs a title.", "title")
    due = parse_date(due_date, field="due date")
    _check_assignee(ctx, assigned_to_user_id)
    with tx(cur) as c:
        require_connection(c, ctx, person_id, include_archived=False)
        c.execute("INSERT INTO donor.task (person_id, parish_id, title, details, due_date, assigned_to_user_id, created_by_user_id) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                  (person_id, ctx.parish_id, t, clean_text(details, field="details", max_len=2000), due,
                   assigned_to_user_id, ctx.user_id))
        tid = c.fetchone()["id"]
        log_change(c, ctx, "task", tid, "title", None, t, person_id=person_id, kind="create", scope="parish")
        return {"id": tid}


_NEXT = {"pending": {"accepted", "done", "cancelled"}, "accepted": {"done", "cancelled", "pending"},
         "done": {"pending"}, "cancelled": {"pending"}}


def task_update(ctx: Ctx, task_id: int, *, status: str | None = None, title: str | None = None,
                due_date=None, assigned_to_user_id: int | None = None, set_assignee: bool = False,
                cur=None) -> dict:
    """Change a task's status (pending, accepted, done, cancelled), title, due date or assignee. Done and
    cancelled tasks can be reopened (set back to pending)."""
    need_people(ctx, "notes.staff")
    with tx(cur) as c:
        c.execute("SELECT * FROM donor.task WHERE id = %s AND parish_id = %s FOR UPDATE", (task_id, ctx.parish_id))
        t = c.fetchone()
        if not t:
            raise NotFound("That task was not found at this parish.")
        changed = []
        sets, params = [], []
        if status is not None:
            st = check_enum(status, TASK_STATUSES, field="status", allow_blank=False)
            if st != t["status"]:
                if st not in _NEXT[t["status"]]:
                    raise InvalidInput(f"A {t['status']} task cannot become {st}.", "status")
                sets.append("status = %s")
                params.append(st)
                sets.append("completed_at = " + ("NOW()" if st == "done" else "NULL"))
                log_change(c, ctx, "task", task_id, "status", t["status"], st, person_id=t["person_id"], scope="parish")
                changed.append("status")
        if title is not None:
            nt = clean_text(title, field="title", max_len=200)
            if not nt:
                raise InvalidInput("A task needs a title.", "title")
            if nt != t["title"]:
                sets.append("title = %s")
                params.append(nt)
                log_change(c, ctx, "task", task_id, "title", t["title"], nt, person_id=t["person_id"], scope="parish")
                changed.append("title")
        if due_date is not None:
            nd = parse_date(due_date, field="due date")
            if nd != t["due_date"]:
                sets.append("due_date = %s")
                params.append(nd)
                log_change(c, ctx, "task", task_id, "due_date", t["due_date"], nd, person_id=t["person_id"], scope="parish")
                changed.append("due_date")
        if set_assignee and assigned_to_user_id != t["assigned_to_user_id"]:
            _check_assignee(ctx, assigned_to_user_id)
            sets.append("assigned_to_user_id = %s")
            params.append(assigned_to_user_id)
            log_change(c, ctx, "task", task_id, "assigned_to", t["assigned_to_user_id"], assigned_to_user_id,
                       person_id=t["person_id"], scope="parish")
            changed.append("assigned_to")
        if sets:
            c.execute(f"UPDATE donor.task SET {', '.join(sets)}, updated_at = NOW() WHERE id = %s", (*params, task_id))
        return {"id": task_id, "changed": changed}


def tasks_for_person(ctx: Ctx, person_id: int, include_closed: bool = True) -> list[dict]:
    need_people(ctx, "notes.staff")
    with tx() as c:
        require_connection(c, ctx, person_id)
        c.execute("SELECT * FROM donor.task WHERE person_id = %s AND parish_id = %s"
                  + ("" if include_closed else " AND status IN ('pending', 'accepted')")
                  + " ORDER BY (status IN ('done','cancelled')), due_date NULLS LAST, id", (person_id, ctx.parish_id))
        return c.fetchall()


def tasks_for_user(ctx: Ctx, include_closed: bool = False) -> list[dict]:
    """The caller's own open tasks at this parish, with the person's name."""
    need_people(ctx, "notes.staff")
    with tx() as c:
        c.execute("SELECT t.*, p.first_name, p.last_name, p.org_name, p.record_type FROM donor.task t "
                  "JOIN donor.person p ON p.id = t.person_id "
                  "WHERE t.parish_id = %s AND t.assigned_to_user_id = %s"
                  + ("" if include_closed else " AND t.status IN ('pending', 'accepted')")
                  + " ORDER BY t.due_date NULLS LAST, t.id", (ctx.parish_id, ctx.user_id))
        return c.fetchall()
