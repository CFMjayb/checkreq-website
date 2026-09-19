"""
path_guard.py -- confinement checks for client-supplied SharePoint path
components (Security Assessment 2026-09-19, finding H4).

WHY THIS EXISTS. parish_documents.py and cornerstone_documents.py build
Microsoft Graph paths (`.../drive/root:/{root}/{rel_path}:/content`) from
values the browser sends: a `{rel_path:path}` URL segment on download, a
`rel_path` form field on delete, and `UploadFile.filename` on upload. None
of those were normalized. The installed urllib3 removes dot-segments before
sending, so a rel_path containing `..` produced a request URL pointing
OUTSIDE the intended parish/entity folder -- and the delete routes' plain
`rel_path.startswith("Parish Files/")` check is satisfied by a path that
later climbs back out. A parish user could plausibly read, overwrite, or
delete another parish's (or the diocese's) files on the same drive.

Two pure functions, both raise PathGuardError (a ValueError) so a route can
turn any violation into a plain 400 without leaking which check tripped:

  safe_rel_path(rel_path, allowed_root)
      Rejects any path component that is empty, "." or "..", any backslash,
      NUL, or a leading "/", plus the handful of characters SharePoint/
      OneDrive itself forbids in a name (: * ? " < > |) -- no legitimate
      file can contain them, and ":" in particular is Graph's own
      path-addressing delimiter (`root:/path:/content`), so allowing it
      would hand a client a way to alter the Graph request itself. Then
      posixpath.normpath()s the joined path and requires it to still sit
      strictly under allowed_root. Returns the normalized full path.

  safe_filename(name)
      Reduces an uploaded filename to its basename (either separator),
      rejects empty / "." / ".." / anything containing "..", and the same
      SharePoint-forbidden characters. Returns the clean basename.

Standard library only; importable from anywhere with no cycle risk.
"""
from __future__ import annotations

import posixpath

# Characters no legitimate SharePoint/OneDrive item name can contain (the
# service itself refuses them), plus NUL. Rejecting them costs nothing for
# real files and removes the Graph-syntax-injection angle (":" especially).
# "%" is also rejected: `requests` leaves a valid %XX escape in a URL path
# untouched and Graph percent-DECODES path segments server-side, so a
# component containing a literal "%2F" or "%2e%2e" would sail past a plain
# ".." check here and still resolve as a separator / dot-segment at Graph.
# SharePoint does technically allow "%" in a name; no file in any of this
# app's document areas has one (inventoried 2026-09-19), and a file that
# did would simply 400 here rather than be reachable.
_FORBIDDEN_CHARS = frozenset('\\:*?"<>|%\x00')


class PathGuardError(ValueError):
    """A client-supplied path/filename failed confinement. Message is safe
    to show to the user (it never echoes the offending value)."""


def _check_component(component: str) -> None:
    if component in ("", ".", ".."):
        raise PathGuardError("Invalid path.")
    if any(ch in _FORBIDDEN_CHARS for ch in component):
        raise PathGuardError("Invalid path.")


def validate_rel_path(rel_path: str) -> str:
    """Component-level checks only (no root needed): a relative,
    "/"-separated path with no empty / "." / ".." components, no leading
    "/", no backslash, NUL, or SharePoint-forbidden character. Returns
    rel_path unchanged; raises PathGuardError. Routes call this FIRST, before
    spending a Graph call on folder resolution; safe_rel_path() below runs
    it again and then adds the root-containment check."""
    if not isinstance(rel_path, str) or not rel_path:
        raise PathGuardError("Invalid path.")
    if rel_path.startswith("/") or "\\" in rel_path or "\x00" in rel_path:
        raise PathGuardError("Invalid path.")
    for component in rel_path.split("/"):
        _check_component(component)
    return rel_path


def safe_rel_path(rel_path: str, allowed_root: str) -> str:
    """Validate `rel_path` (client-supplied, "/"-separated, relative) and
    return posixpath.normpath(f"{allowed_root}/{rel_path}"), guaranteed to
    start with allowed_root + "/". Raises PathGuardError otherwise."""
    validate_rel_path(rel_path)

    root = (allowed_root or "").strip("/")
    if not root:
        # A drive-root anchor. Every component was already vetted above, so
        # the normalized value cannot climb; just make sure it did not.
        joined = posixpath.normpath(rel_path)
        if joined.startswith("/") or joined == "." or joined.startswith("../") or joined == "..":
            raise PathGuardError("Invalid path.")
        return joined

    joined = posixpath.normpath(f"{root}/{rel_path}")
    if not joined.startswith(root + "/"):
        raise PathGuardError("Invalid path.")
    return joined


def safe_filename(name: str | None) -> str:
    """The bare, confinement-safe basename of an uploaded filename. Raises
    PathGuardError if nothing usable remains."""
    raw = (name or "").replace("\\", "/")
    base = posixpath.basename(raw).strip()
    if not base or base in (".", "..") or ".." in base:
        raise PathGuardError("Invalid file name.")
    if any(ch in _FORBIDDEN_CHARS for ch in base):
        raise PathGuardError("Invalid file name.")
    return base
