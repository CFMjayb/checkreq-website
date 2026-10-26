"""
documents.py -- 26-162 Phase 2: filing the documents of an onboarding, offboarding or benefits case (Plan.md section 5).

THE RULE (Jay, 2026-10-22: "Follow Plan.md section 5: SharePoint, two copies"; confirmed again 2026-10-26): SharePoint holds every
file, Postgres (hr.case_documents) holds only the drive and item ids and the facts about the file. Every HR document is written
TWICE, in the entity's own library:

  primary copy   <the employing unit's folder>\\HRPayroll\\Personnel Files\\<Last>, <First> (<case number>)          types 1-2
                 <the requesting parish's folder>\\HRPayroll\\Benefits Enrollment\\<Last>, <First> (<case number>)   type 3
  second copy    800. HR Administration\\Personnel Files\\<Last>, <First> (<case number>)
                 800. HR Administration\\Benefits Enrollment\\<parish code> <parish name>\\<Last>, <First>
  I-9s           ONE copy only, in 800. HR Administration\\I-9\\<Last>, <First> (<case number>) (USCIS advises keeping I-9s apart
                 from personnel files); copy_sp_* stays empty.

FAILS CLOSED (a deliberate choice). The two top folders -- <unit>\\HRPayroll and 800. HR Administration -- must ALREADY EXIST;
this module creates only the folders UNDER them. Those two folders carry their own SharePoint permissions (set when they are
created, see migrations\\setup_hr_document_folders.py); a folder this code created on its own would inherit the permissions of its
parent, and a file with a Social Security number in it must never land in a folder whoever can browse the parish folder can open.
If either is missing the upload is refused and nothing is written anywhere.

All or nothing. Both copies are written, then the database row; if the second copy or the row fails, the file(s) just uploaded
are taken back (best effort, and the message says if one could not be). A filed document is never deleted by anything else: a
replacement SUPERSEDES the earlier one (hr.case_documents.superseded_by_id) and both files stay.

File names carry no person: <doc type>_<case number>_<yyyymmdd>.<ext>, with _2, _3 added if that name is taken in either folder
(a second upload the same day never overwrites the first; Graph's upload overwrites silently).

WHO MAY FILE AND WHO MAY OPEN (Plan.md section 5, "Sensitivity"): each document type is standard, restricted or i9. Restricted and
standard open for hr_admin, benefits_admin and beacon_admin at the case's entity; an I-9 opens for hr_admin ONLY. Anyone who may not
open a kind of document may not file one either (an upload that the uploader then could not open would be a trap). The screens are
reachable only to those three roles anyway; the check is repeated here, per document, so the rule does not depend on the screen.

Nothing here imports FastAPI. The routes are in hr_documents.py. The storage object is passed in so tests can use an in-memory one.
"""
from __future__ import annotations

import datetime as dt
import re

import db
import path_guard
import rbac
import sharepoint_client
import upload_guard

from . import cases, clock
from .errors import HrError

STAFF_KINDS = ("onboard", "offboard", "change")
BENEFITS_KINDS = ("benefits_enroll", "benefits_end")
PERSONNEL = "Personnel Files"
BENEFITS = "Benefits Enrollment"
I9_FOLDER = "I-9"

# who may open (and so file) each kind of document
VIEW_ROLES = {
    "standard": frozenset({"hr_admin", "benefits_admin", "beacon_admin"}),
    "restricted": frozenset({"hr_admin", "benefits_admin", "beacon_admin"}),
    "i9": frozenset({"hr_admin"}),
}

# An entity with no SharePoint library of its own files in another entity's library. Explicit and short on purpose: a new diocese
# must NEVER fall back to somebody else's library (its staff's documents would land in a stranger's folder tree). Claggett has no
# folder structure yet (Jay, 2026-10-22); when it gets one, give it its own sp_parish_* settings and this entry stops applying.
FALLBACK_LIBRARY_ORG = {"Claggett": "EDOM"}

MAX_BYTES = upload_guard.MAX_UPLOAD_BYTES

_BAD_SEGMENT_CHARS = re.compile(r'[\\/:*?"<>|%#\x00-\x1f]')


# ------------------------------------------------------------------ pure rules

def clean_segment(text: str | None) -> str:
    """A folder or file name part SharePoint and our URLs accept: no \\ / : * ? " < > | % # or control characters (a '#' or a '%'
    would break the unencoded Graph URL), runs of spaces collapsed, no leading or trailing space or dot. Empty -> 'Unnamed'."""
    t = _BAD_SEGMENT_CHARS.sub(" ", str(text or ""))
    t = re.sub(r"\s+", " ", t).strip().strip(".").strip()
    return t or "Unnamed"


def can_view(sensitivity: str, roles) -> bool:
    return bool(VIEW_ROLES.get(sensitivity, frozenset()) & set(roles or ()))


def person_folder(case: dict, *, with_case_number: bool = True) -> str:
    name = f"{clean_segment(case.get('last_name'))}, {clean_segment(case.get('first_name'))}"
    return f"{name} ({clean_segment(case['case_number'])})" if with_case_number else name


def split_path(path: str | None) -> list[str]:
    return [clean_segment(p) for p in str(path or "").split("/") if p.strip()]


def plan_filing(case: dict, sensitivity: str, settings: dict, unit: dict | None) -> dict:
    """Where a document goes. {'roots': [parts, ...], 'targets': [parts, ...]} where every `parts` is a list of folder names under the
    library root; targets[0] is the primary copy (recorded in sp_*), targets[1] the second copy (copy_sp_*), absent for an I-9.
    `roots` are the folders that must already exist. Raises HrError('refused') when the case has no folder to file into."""
    hr_root = split_path(settings.get("hr_folder") or "800. HR Administration")
    sub = clean_segment(settings.get("entity_hr_subfolder") or "HRPayroll")
    person = person_folder(case)
    if sensitivity == "i9":
        return {"roots": [hr_root], "targets": [hr_root + [I9_FOLDER, person]]}
    if not unit or not (unit.get("sp_folder_path") or "").strip():
        who = (unit or {}).get("name") or "this case's unit"
        raise HrError("refused", f"{who} has no SharePoint folder on record yet, so there is nowhere to file this document. "
                                 "Set it up with the HR folder setup (see the HR documents quick guide), then try again.")
    unit_root = split_path(unit["sp_folder_path"]) + [sub]
    if case["case_kind"] in BENEFITS_KINDS:
        unit_label = clean_segment(f"{unit.get('code') or ''} {unit.get('name') or ''}")
        return {"roots": [unit_root, hr_root],
                "targets": [unit_root + [BENEFITS, person],
                            hr_root + [BENEFITS, unit_label, person_folder(case, with_case_number=False)]]}
    return {"roots": [unit_root, hr_root],
            "targets": [unit_root + [PERSONNEL, person], hr_root + [PERSONNEL, person]]}


def file_stem(doc_type_key: str, case_number: str, day: dt.date) -> str:
    return f"{clean_segment(doc_type_key)}_{clean_segment(case_number)}_{day:%Y%m%d}"


def free_name(taken, stem: str, ext: str) -> str:
    """stem.ext, or stem_2.ext, stem_3.ext ... -- the first name not in `taken` (compared case-insensitively, as SharePoint does)."""
    low = {str(t).casefold() for t in taken}
    n = 1
    while True:
        name = f"{stem}.{ext}" if n == 1 else f"{stem}_{n}.{ext}"
        if name.casefold() not in low:
            return name
        n += 1


def check_parts(parts: list[str]) -> None:
    try:
        path_guard.validate_rel_path("/".join(parts))
    except path_guard.PathGuardError:
        raise HrError("invalid", "A folder name for this document is not allowed. Check the person's name and the unit's folder.")


# ------------------------------------------------------------------ storage (SharePoint)

class SharePointStorage:
    """The few SharePoint calls filing needs, for one entity's library. Paths are lists of folder names UNDER the library root."""

    def __init__(self, hostname: str, site_path: str, library_folder: str | None):
        self.hostname, self.site_path = hostname, site_path
        self.root = (library_folder or "").strip("/")
        self._token = self._site = None

    def _ready(self) -> None:
        if self._token is None:
            self._token = sharepoint_client.get_access_token(self.hostname)
            self._site = sharepoint_client.get_site_id(self._token, self.hostname, self.site_path)

    def _full(self, parts) -> str:
        return "/".join(([self.root] if self.root else []) + list(parts))

    def _children(self, parts) -> list[dict]:
        self._ready()
        return sharepoint_client.list_folder(self._token, self._site, self._full(parts))

    def folder_exists(self, parts) -> bool:
        parts = list(parts)
        want = parts[-1].casefold()
        return any(e["is_folder"] and e["name"].casefold() == want for e in self._children(parts[:-1]))

    def ensure_path(self, parts, existing_depth: int = 0) -> None:
        """Creates the folders of `parts` below the first `existing_depth` (which must already exist). Create-if-missing only."""
        parts = list(parts)
        for i in range(existing_depth, len(parts)):
            self._ready()
            sharepoint_client.ensure_folder(self._token, self._site, self._full(parts[:i]), parts[i])

    def names_in(self, parts) -> set[str]:
        return {e["name"].casefold() for e in self._children(parts)}

    def upload(self, parts, filename: str, data: bytes, content_type: str) -> dict:
        self._ready()
        item = sharepoint_client.upload_bytes(self._token, self._site, self._full(parts), filename, data, content_type)
        drive = (item.get("parentReference") or {}).get("driveId")
        if not drive or not item.get("id"):
            raise RuntimeError("SharePoint did not return the file's ids")
        return {"drive_id": drive, "item_id": item["id"]}

    def download(self, drive_id: str, item_id: str) -> bytes:
        self._ready()
        return sharepoint_client.download_item_bytes(self._token, drive_id, item_id)

    def delete_item(self, drive_id: str, item_id: str) -> None:
        self._ready()
        sharepoint_client.delete_item(self._token, drive_id, item_id)


def library_for(org_id: int) -> dict:
    """The SharePoint library an entity's HR documents go to (checkreq.organizations.sp_parish_*), or the explicit fallback."""
    org = db.query_one("SELECT code, sp_parish_hostname, sp_parish_site_path, sp_parish_library_folder "
                       "FROM checkreq.organizations WHERE id = %s", (org_id,))
    if not org:
        raise HrError("not_found", "Unknown entity.")
    if org["sp_parish_hostname"] and org["sp_parish_site_path"]:
        return dict(org)
    other = FALLBACK_LIBRARY_ORG.get(org["code"])
    if other:
        row = db.query_one("SELECT code, sp_parish_hostname, sp_parish_site_path, sp_parish_library_folder "
                           "FROM checkreq.organizations WHERE code = %s", (other,))
        if row and row["sp_parish_hostname"] and row["sp_parish_site_path"]:
            return dict(row)
    raise HrError("refused", f"{org['code']} has no SharePoint document library set up for HR documents yet.")


def storage_for(org_id: int) -> SharePointStorage:
    lib = library_for(org_id)
    return SharePointStorage(lib["sp_parish_hostname"], lib["sp_parish_site_path"], lib["sp_parish_library_folder"])


# ------------------------------------------------------------------ reading

def roles_at(user_id: int, org_id: int) -> set[str]:
    return set(rbac.get_role_keys(user_id, org_id))


def list_for_case(case_id: int, org_ids: list[int], user_id: int) -> list[dict]:
    """Every document filed on the case (the current ones first), each with the label of its type, who filed it, the checklist item
    it belongs to, and `can_view` for this user. Metadata only: no file is read."""
    c = cases._scoped_case(case_id, org_ids)
    roles = roles_at(user_id, c["org_id"])
    rows = db.query(
        "SELECT d.id, d.case_item_id, d.doc_type_key, d.file_name, d.content_type, d.size_bytes, d.uploaded_at, d.retain_until, "
        "       d.superseded_by_id, (d.copy_sp_item_id IS NOT NULL) AS has_copy, t.label AS doc_label, t.sensitivity, "
        "       u.display_name AS uploaded_by_name, i.item_key, i.label AS item_label "
        "FROM hr.case_documents d JOIN hr.doc_types t ON t.key = d.doc_type_key "
        "LEFT JOIN checkreq.app_users u ON u.id = d.uploaded_by_user_id "
        "LEFT JOIN hr.case_items i ON i.id = d.case_item_id "
        "WHERE d.case_id = %s ORDER BY (d.superseded_by_id IS NOT NULL), d.uploaded_at DESC, d.id DESC", (c["id"],))
    out = []
    for r in rows:
        d = dict(r)
        d["is_current"] = d["superseded_by_id"] is None
        d["can_view"] = can_view(d["sensitivity"], roles)
        out.append(d)
    return out


def doc_type_options(user_id: int, org_id: int) -> list[dict]:
    """The document types this user may file (and open), for the general Upload File control on the case page."""
    roles = roles_at(user_id, org_id)
    return [dict(r) for r in db.query("SELECT key, label, sensitivity FROM hr.doc_types WHERE is_active ORDER BY sort_order, label")
            if can_view(r["sensitivity"], roles)]


def read_file(case_id: int, doc_id: int, org_ids: list[int], user_id: int, *, copy: bool = False, storage=None) -> tuple[bytes, dict]:
    """(bytes, the document row) for a document of this case, from the primary copy (or the second one). Refuses a case outside the
    caller's entities (not found), a document of another case (not found) and a kind of document the caller may not open."""
    c = cases._scoped_case(case_id, org_ids)
    d = db.query_one(
        "SELECT d.*, t.label AS doc_label, t.sensitivity FROM hr.case_documents d JOIN hr.doc_types t ON t.key = d.doc_type_key "
        "WHERE d.id = %s AND d.case_id = %s", (doc_id, c["id"]))
    if not d:
        raise HrError("not_found", "That document was not found on this case.")
    if not can_view(d["sensitivity"], roles_at(user_id, c["org_id"])):
        raise HrError("refused", "Your role cannot open this kind of document.")
    if copy:
        if not d["copy_sp_item_id"]:
            raise HrError("not_found", "This document has no second copy.")
        drive, item = d["copy_sp_drive_id"], d["copy_sp_item_id"]
    else:
        drive, item = d["sp_drive_id"], d["sp_item_id"]
    store = storage or storage_for(c["org_id"])
    try:
        data = store.download(drive, item)
    except HrError:
        raise
    except Exception:
        raise HrError("unavailable", "SharePoint could not return the file just now. Try again in a minute.")
    return data, dict(d)


# ------------------------------------------------------------------ filing

def _undo(store, uploaded: list[dict]) -> int:
    """Takes back files this call just uploaded; the number that could NOT be removed."""
    failed = 0
    for u in uploaded:
        try:
            store.delete_item(u["drive_id"], u["item_id"])
        except Exception:
            failed += 1
    return failed


def file_document(case_id: int, org_ids: list[int], *, user_id: int, filename: str, data: bytes, declared_type: str | None,
                  doc_type_key: str | None = None, item_key: str | None = None, storage=None) -> dict:
    """File one document on a case in both places and record it. Returns {'id', 'file_name', 'doc_label', 'replaced', 'copies'}.
    Raises HrError: not_found (case or item), invalid (the file, the type), refused (role, no folder, folders not set up, SharePoint)."""
    c = cases._scoped_case(case_id, org_ids)
    item = None
    if item_key:
        item = db.query_one("SELECT * FROM hr.case_items WHERE case_id = %s AND item_key = %s", (c["id"], item_key))
        if not item:
            raise HrError("not_found", "That checklist item is not on this case.")
        if not item["doc_type_key"]:
            raise HrError("invalid", "That checklist item does not take a document.")
        doc_type_key = item["doc_type_key"]
    if not doc_type_key:
        raise HrError("invalid", "Pick the kind of document first.")
    dtype = db.query_one("SELECT key, label, sensitivity FROM hr.doc_types WHERE key = %s AND is_active", (doc_type_key,))
    if not dtype:
        raise HrError("invalid", "That kind of document is not on the list.")
    if not can_view(dtype["sensitivity"], roles_at(user_id, c["org_id"])):
        raise HrError("refused", f"Your role cannot file {dtype['label'].lower()} (it is also the role that may open it).")

    if not data:
        raise HrError("invalid", "That file is empty.")
    if len(data) > MAX_BYTES:
        raise HrError("invalid", f"That file is over the {MAX_BYTES // (1024 * 1024)} MB limit.")
    ok, ctype = upload_guard.sniff_allowed(data, declared_type)
    if not ok:
        raise HrError("invalid", ctype)
    ext = upload_guard.ALLOWED_TYPES[ctype]

    unit = None
    if c.get("parish_id"):
        row = db.query_one("SELECT id, code, name, sp_folder_path FROM portal.parishes WHERE id = %s", (c["parish_id"],))
        unit = dict(row) if row else None
    settings = cases.settings(c["org_id"])
    plan = plan_filing(c, dtype["sensitivity"], settings, unit)
    for parts in plan["roots"] + plan["targets"]:
        check_parts(parts)

    store = storage or storage_for(c["org_id"])
    try:
        for root in plan["roots"]:
            if not store.folder_exists(root):
                raise HrError("refused", f"The folder '{root[-1]}' is not set up in SharePoint yet, so nothing was filed. "
                                         "It has to be created with its own permissions first (HR folder setup).")
        taken: set[str] = set()
        for parts in plan["targets"]:
            depth = max((len(r) for r in plan["roots"] if parts[:len(r)] == r), default=0)
            store.ensure_path(parts, depth)
            taken |= store.names_in(parts)
        name = free_name(taken, file_stem(doc_type_key, c["case_number"], clock.today(c["org_id"])), ext)
        uploaded: list[dict] = []
        try:
            for parts in plan["targets"]:
                uploaded.append(store.upload(parts, name, data, ctype))
        except Exception:
            left = _undo(store, uploaded)
            raise HrError("refused", "SharePoint refused the upload, so nothing was filed."
                          + (" One file already uploaded could not be taken back; tell the administrator." if left else ""))
    except HrError:
        raise
    except Exception:
        raise HrError("refused", "SharePoint could not be reached just now, so nothing was filed. Try again in a minute.")

    primary, second = uploaded[0], (uploaded[1] if len(uploaded) > 1 else None)
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO hr.case_documents (case_id, case_item_id, doc_type_key, sp_drive_id, sp_item_id, copy_sp_drive_id, "
                    " copy_sp_item_id, file_name, content_type, size_bytes, uploaded_by_user_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (c["id"], item["id"] if item else None, doc_type_key, primary["drive_id"], primary["item_id"],
                     second["drive_id"] if second else None, second["item_id"] if second else None, name, ctype, len(data), user_id))
                new_id = cur.fetchone()["id"]
                cur.execute(
                    "UPDATE hr.case_documents SET superseded_by_id = %s WHERE case_id = %s AND doc_type_key = %s AND id <> %s "
                    " AND superseded_by_id IS NULL AND case_item_id IS NOT DISTINCT FROM %s RETURNING id",
                    (new_id, c["id"], doc_type_key, new_id, item["id"] if item else None))
                replaced = [r["id"] for r in cur.fetchall()]
                cases._event(cur, c["id"], "document_added", user_id,
                             {"document": new_id, "doc_type": doc_type_key, "item": item_key or None,
                              "copies": len(uploaded), "replaced": replaced}, item_id=item["id"] if item else None)
            conn.commit()
    except Exception:
        left = _undo(store, uploaded)
        raise HrError("refused", "The document could not be recorded, so nothing was filed."
                      + (" A file already uploaded could not be taken back; tell the administrator." if left else ""))
    return {"id": new_id, "file_name": name, "doc_label": dtype["label"], "replaced": len(replaced), "copies": len(uploaded),
            "content_type": ctype}
