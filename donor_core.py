"""
donor_core.py -- Beacon Donor Management: shared building blocks.

New file per the standing main.py rule. See "Donor Management Plan.md".

Everything the other donor_* modules share and nothing that touches a request:
  * Ctx            who is acting, at which parish, with which roles and capabilities. Built once at
                   the route boundary (donor_roles.build_ctx) -- services never read the request.
  * exceptions     DonorError and its children. Routes turn them into friendly messages.
  * tx()           one transaction helper (reuse a cursor, or open a connection and commit).
  * validators     dates, e-mail, phone, enum values, age and the "minor" rule.
  * log_change()   THE ONE writer of donor.change_log (requirement NF-04): the service layer calls it,
                   screens never do.

Pure Python plus db.py. Compatible with Python 3.11 (the Cloud Run image).
"""
from __future__ import annotations

import datetime as dt
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal

import db


# ── Exceptions ──────────────────────────────────────────────────────────────────────────────────
class DonorError(Exception):
    """Base class. `message` is safe to show to the person using the screen."""

    def __init__(self, message: str, field: str | None = None, details=None):
        super().__init__(message)
        self.message = message
        self.field = field
        self.details = details          # optional structured payload (e.g. duplicate candidates)


class PermissionDenied(DonorError):
    pass


class NotFound(DonorError):
    pass


class InvalidInput(DonorError):
    pass


class Conflict(DonorError):
    pass


# ── Fixed vocabularies (match the CHECK constraints in migrations 076 / 077) ────────────────────
GENDERS = ("female", "male", "nonbinary", "unknown")
MARITAL_STATUSES = ("single", "married", "widowed", "divorced", "separated", "unknown")
RECORD_TYPES = ("person", "organization")
CONTACT_KINDS = ("email", "phone")
CONTACT_SUBTYPES = ("home", "cell", "work", "other")
HOUSEHOLD_POSITIONS = ("primary_adult", "secondary_adult", "child")
CONNECTION_KINDS = ("member", "giver", "visitor")
STATEMENT_OPTIONS = ("individual", "joint", "none")
STATEMENT_DELIVERIES = ("email", "print")
DIOCESAN_CATEGORIES = ("active_member", "inactive_member", "non_member", "transferred_out", "removed", "deceased",
                       "organization", "renter")   # organization and renter added by migration 085 (MS-11)
MEMBER_CATEGORIES = ("active_member", "inactive_member")
LEAVING_CATEGORIES = ("transferred_out", "removed", "deceased")
HOW_JOINED = ("baptism", "transfer", "confirmation", "reception", "reaffirmation", "other")
REMOVAL_REASONS = ("transferred", "moved", "deceased", "inactive", "other")
CANONICAL_STANDINGS = ("not_set", "baptized_member", "communicant", "communicant_in_good_standing")
SACRAMENT_KINDS = ("baptism", "confirmation", "reception", "reaffirmation", "marriage", "burial")
TRANSFER_DIRECTIONS = ("incoming", "outgoing")
TRANSFER_STATUSES = ("requested", "issued", "received", "accepted", "cancelled")
NOTE_VISIBILITIES = ("clergy", "staff")
TASK_STATUSES = ("pending", "accepted", "done", "cancelled")
PLACEHOLDER_KINDS = ("open_plate", "anonymous")
GRADES = ("pre_k", "k", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "college", "graduate")

LABELS = {
    "pre_k": "Pre-K", "k": "Kindergarten", "college": "College", "graduate": "Graduate school",
    **{str(n): f"Grade {n}" for n in range(1, 13)},
    "finance_view_detail": "Finance view (individual gifts)",
    "female": "Female", "male": "Male", "nonbinary": "Nonbinary", "unknown": "Unknown",
    "single": "Single", "married": "Married", "widowed": "Widowed", "divorced": "Divorced",
    "separated": "Separated",
    "primary_adult": "Primary Adult", "secondary_adult": "Secondary Adult", "child": "Child",
    "member": "Member", "giver": "Giver", "visitor": "Visitor",
    "individual": "Individual", "joint": "Joint with spouse", "none": "No statement",
    "email": "Email", "print": "Print",
    "active_member": "Active member", "inactive_member": "Inactive member", "non_member": "Not a member",
    "transferred_out": "Transferred out", "removed": "Removed", "deceased": "Deceased",
    "organization": "Organization", "renter": "Renter",
    "baptism": "Baptism", "transfer": "Transfer", "confirmation": "Confirmation", "reception": "Reception",
    "reaffirmation": "Reaffirmation", "marriage": "Marriage", "burial": "Burial", "other": "Other",
    "transferred": "Transferred", "moved": "Moved", "inactive": "Inactive",
    "not_set": "Not set", "baptized_member": "Baptized member", "communicant": "Communicant",
    "communicant_in_good_standing": "Communicant in good standing",
    "incoming": "Incoming", "outgoing": "Outgoing",
    "requested": "Requested", "issued": "Issued", "received": "Received", "accepted": "Accepted",
    "cancelled": "Cancelled",
    "clergy": "Clergy only", "staff": "Parish staff",
    "pending": "Pending", "done": "Done",
    "home": "Home", "cell": "Cell", "work": "Work",
    "open_plate": "Open Plate", "anonymous": "Anonymous",
    # Phase 2 (giving)
    "tax_deductible": "Tax-deductible", "non_deductible": "Not deductible", "non_gift_receipt": "Non-gift receipt",
    "in_kind": "In-kind", "stock": "Stock",
    "recorded": "Recorded", "voided": "Voided", "reversed": "Reversed", "returned": "Returned",
    "open": "Open", "closed": "Closed", "reconciled": "Reconciled",
    "deposit": "Deposit", "non_deposit": "Non-deposit",
    "one_time": "One time", "weekly": "Weekly", "monthly": "Monthly", "quarterly": "Quarterly", "annual": "Annual",
    "active": "Active", "unrestricted": "Unrestricted", "donor_restricted": "Donor restricted",
    "built": "Built", "incomplete": "Incomplete", "posted": "Posted", "failed": "Failed", "superseded": "Superseded",
    "on_track": "On track", "behind": "Behind", "paid_in_full": "Paid in full", "no_pledge": "No pledge",
}


def label(value: str | None) -> str:
    if not value:
        return ""
    return LABELS.get(value, str(value).replace("_", " ").capitalize())


# ── Capabilities and Ctx ────────────────────────────────────────────────────────────────────────
# Role key -> capabilities. 'parish_admin' is the EXISTING portal role (read, never written, by
# donor_roles.effective_roles). The rest are donor.role keys. See the Plan, section 3, and the
# Requirements doc's role table. Giving capabilities are listed here so one table tells the whole story.
_PEOPLE_BASE = {"people.view", "people.create"}
_STAFF = _PEOPLE_BASE | {"people.edit", "notes.staff", "minors.details"}
_MEMBERSHIP = {"membership.view", "membership.edit", "sacrament.view", "sacrament.edit", "transfer.edit"}
_FINANCE = {
    "people.view", "people.create", "batch.view", "batch.open", "batch.line", "batch.close",
    "gift.correct", "funds.manage", "pledges.manage", "giving.read", "totals.read", "statements.run",
}
CAPS_BY_ROLE: dict[str, frozenset] = {
    "parish_admin": frozenset(_STAFF),
    "clergy": frozenset(_STAFF | _MEMBERSHIP | {"standing.review", "notes.clergy"}),
    "membership_editor": frozenset(_STAFF | _MEMBERSHIP),
    "gift_entry": frozenset(_PEOPLE_BASE | {"batch.view", "batch.open", "batch.line"}),
    "finance": frozenset(_FINANCE),
    "finance_supervisor": frozenset(_FINANCE | {"batch.reopen"}),
    "finance_view": frozenset({"totals.read"}),
    # Read-only: the individual gifts, pledges and totals (TouchPoint's FinanceViewOnlyDetail). Holds no write capability at all.
    "finance_view_detail": frozenset({"people.view", "giving.read", "totals.read"}),
    "diocesan_finance": frozenset({"giving.read.diocese", "totals.read"}),
}
FINANCE_ROLES = ("finance", "finance_supervisor", "finance_view", "finance_view_detail", "diocesan_finance")   # rule 5: never self-granted
# Also never self-granted (Jay, 2026-10-09): Clergy opens the clergy-only pastoral notes and canonical standing, so a second person
# has to give it. Together with FINANCE_ROLES this is "every sensitive role needs a second person".
SECOND_PERSON_ROLES = ("clergy",)
PARISH_DONOR_ROLES = ("clergy", "membership_editor", "gift_entry", "finance", "finance_supervisor", "finance_view_detail", "finance_view")


def caps_for(roles, is_parish_manager: bool = False, can_activate: bool = False) -> frozenset:
    caps: set = set()
    for r in roles:
        caps |= CAPS_BY_ROLE.get(r, frozenset())
    if is_parish_manager:
        caps.add("roles.manage")
    if can_activate:
        caps.add("parish.activate")
    return frozenset(caps)


@dataclass(frozen=True)
class Ctx:
    """Who is acting, where. Immutable. `roles` are donor role keys held AT THIS PARISH, plus the marker
    'parish_admin' when the person holds the existing portal Parish Admin role here."""
    user_id: int
    user_label: str
    parish_id: int
    parish_name: str = ""
    org_id: int | None = None
    roles: frozenset = frozenset()
    caps: frozenset = frozenset()
    is_diocesan_admin: bool = False          # Beacon Admin at the parish's own diocese
    # Beacon Admin or Setup Admin at the parish's own diocese (Jay, 2026-10-09: "A Beacon Admin or a Setup Admin should be able to
    # self assign any role"). The one exception to "nobody gives themselves a finance role or Clergy": it waives that refusal and the
    # "must already have a login at this parish" check for the person's OWN grants. Every such grant is noted as self-assigned.
    may_self_assign: bool = False
    settings: dict = field(default_factory=dict)

    def can(self, capability: str) -> bool:
        return capability in self.caps

    def require(self, capability: str, message: str | None = None) -> None:
        if capability not in self.caps:
            raise PermissionDenied(message or "You do not have permission to do that for this parish.")

    @property
    def is_clergy(self) -> bool:
        return "notes.clergy" in self.caps

    def has_role(self, role_key: str) -> bool:
        return role_key in self.roles


def need_people(ctx: Ctx, capability: str, message: str | None = None) -> None:
    """The gate every people / membership service starts with: this parish must have Donor Management
    turned on (donor.parish_settings.people_enabled) AND the person must hold the capability."""
    if not ctx.settings.get("people_enabled"):
        raise PermissionDenied("People and membership records are not turned on for this parish yet.")
    ctx.require(capability, message)


def need_giving(ctx: Ctx, capability: str, message: str | None = None) -> None:
    """Same for the giving side (Phase 2): donor.parish_settings.giving_enabled plus the capability."""
    if not ctx.settings.get("giving_enabled"):
        raise PermissionDenied("Giving records are not turned on for this parish yet.")
    ctx.require(capability, message)


def make_ctx(user_id: int, parish_id: int, roles=(), *, user_label: str = "", parish_name: str = "",
             org_id: int | None = None, is_parish_manager: bool = False, can_activate: bool = False,
             is_diocesan_admin: bool = False, may_self_assign: bool | None = None, settings: dict | None = None) -> Ctx:
    """Build a Ctx from explicit facts (what donor_roles.build_ctx does after it has read them). Used by
    tests and by scripts so the capability table above is the only place capabilities are defined.
    `may_self_assign` defaults to is_diocesan_admin (a Beacon Admin may); a Setup Admin passes it True without being a Beacon
    Admin. Someone who may give themselves any role also manages roles at the parish, so it adds roles.manage."""
    roles = frozenset(roles)
    if may_self_assign is None:
        may_self_assign = bool(is_diocesan_admin)
    return Ctx(
        user_id=user_id, user_label=user_label or f"User #{user_id}", parish_id=parish_id,
        parish_name=parish_name or f"Parish #{parish_id}", org_id=org_id, roles=roles,
        caps=caps_for(roles, is_parish_manager or may_self_assign, can_activate), is_diocesan_admin=is_diocesan_admin,
        may_self_assign=bool(may_self_assign), settings=dict(settings or {}),
    )


# ── Transactions ────────────────────────────────────────────────────────────────────────────────
@contextmanager
def tx(cur=None):
    """Yield a dict-row cursor. If `cur` is given the caller owns the transaction (used by merge and
    import so a whole operation commits or rolls back together). Otherwise open a connection, commit on
    success, roll back on any exception."""
    if cur is not None:
        yield cur
        return
    with db.connect() as conn:
        with conn.cursor() as c:
            yield c


# ── Validators ──────────────────────────────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")


def today() -> dt.date:
    return dt.date.today()


def clean_text(value, *, field: str = "", max_len: int = 500) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) > max_len:
        raise InvalidInput(f"{field or 'Text'} is too long (limit {max_len} characters).", field)
    return s


def _two_digit_year(y: int) -> int:
    return y + (1900 if y > (today().year % 100) else 2000) if y < 100 else y


def _real(y: int, mo: int, da: int) -> bool:
    try:
        dt.date(y, mo, da)
        return 1850 <= y <= 2200
    except ValueError:
        return False


def _date_parts(s: str, field: str) -> tuple[int, int, int]:
    """(year, month, day) from typed text. ISO (2026-10-09); US month/day/year with slashes, hyphens, dots or spaces (10/9/2026, 10-9-26);
    or digits alone (10092026, 100926, and 7 digits such as 0521988 for 05/2/1988) when exactly ONE reading is a real date (Jay, 2026-10-10:
    "I entered 0521988 and it didn't understand it"). A date that could be read two ways is refused with a message asking for slashes.
    The month always comes first: day and month order is never guessed."""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.fullmatch(r"(\d{1,2})[/\-. ]+(\d{1,2})[/\-. ]+(\d{2}|\d{4})", s)
    if m:
        return _two_digit_year(int(m.group(3))), int(m.group(1)), int(m.group(2))
    if re.fullmatch(r"\d{6,8}", s):
        if len(s) == 6:                                             # MMDDYY
            cands = [(s[0:2], s[2:4], s[4:6])]
        elif len(s) == 7:                                           # M DD YYYY, or MM D YYYY
            cands = [(s[0:1], s[1:3], s[3:7]), (s[0:2], s[2:3], s[3:7])]
        else:                                                       # MMDDYYYY, or YYYYMMDD
            cands = [(s[0:2], s[2:4], s[4:8]), (s[4:6], s[6:8], s[0:4])]
        readings = {(_two_digit_year(int(y)), int(mo), int(da)) for mo, da, y in cands if _real(_two_digit_year(int(y)), int(mo), int(da))}
        if len(readings) == 1:
            return next(iter(readings))
        if len(readings) > 1:
            raise InvalidInput(f"'{s}' could be read more than one way. Please type it with slashes, like 10/9/1953.", field)
    raise InvalidInput(f"'{s}' is not a date. Use MM/DD/YYYY.", field)


def parse_date(value, *, field: str = "date", allow_future: bool = True) -> dt.date | None:
    """Real dates only. Accepts a date or datetime, ISO text (2026-10-09), US text (10/9/2026, 10/9/26, 10-9-26), or digits alone when only one
    reading is a real date (05021988, 0521988). Blank is None. A two-digit year later than this year's is read as 19xx. Never guesses
    day/month order: month/day/year."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, dt.datetime):
        d = value.date()
    elif isinstance(value, dt.date):
        d = value
    else:
        s = str(value).strip()
        d = None
        y, mo, da = _date_parts(s, field)
        try:
            d = dt.date(y, mo, da)
        except ValueError:
            raise InvalidInput(f"'{s}' is not a real calendar date.", field)
    if d.year < 1850:
        raise InvalidInput(f"{d.isoformat()} is too far in the past for a {field}.", field)
    if not allow_future and d > today():
        raise InvalidInput(f"The {field} cannot be in the future.", field)
    return d


def digits_of(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def clean_email(value, *, field: str = "email") -> str | None:
    s = clean_text(value, field=field, max_len=254)
    if s is None:
        return None
    s = s.lower()
    if not _EMAIL_RE.match(s):
        raise InvalidInput(f"'{s}' is not a valid email address.", field)
    return s


def clean_phone(value, *, field: str = "phone") -> str | None:
    """US numbers (10 digits, or 11 starting with 1) are stored as (410) 555-0142. Anything else with at
    least 7 digits is kept as typed. Fewer than 7 digits is rejected."""
    s = clean_text(value, field=field, max_len=40)
    if s is None:
        return None
    d = digits_of(s)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return f"({d[0:3]}) {d[3:6]}-{d[6:10]}"
    if len(d) < 7:
        raise InvalidInput(f"'{s}' is not a valid phone number.", field)
    return s


def check_enum(value, allowed, *, field: str, allow_blank: bool = True):
    if value is None or (isinstance(value, str) and not value.strip()):
        if allow_blank:
            return None
        raise InvalidInput(f"{field} is required.", field)
    v = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if v not in allowed:
        raise InvalidInput(f"'{value}' is not an allowed {field}. Allowed: {', '.join(allowed)}.", field)
    return v


def to_bool(value, *, field: str = "value") -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "t", "x"):
        return True
    if s in ("0", "false", "no", "n", "off", "f", ""):
        return False
    raise InvalidInput(f"'{value}' is not yes or no.", field)


def to_id(value, *, field: str = "id", label: str | None = None) -> int:
    """A database id typed or posted by a person or a browser. A word, a blank, or a number too big for the column is an
    InvalidInput (a message), never an unhandled error."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise InvalidInput(f"Pick {label or field.replace('_', ' ')}.", field)
    if n < 1 or n > 2_000_000_000:
        raise InvalidInput(f"Pick {label or field.replace('_', ' ')}.", field)
    return n


def to_money(value, *, field: str = "amount", allow_negative: bool = False) -> Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise InvalidInput(f"{field} is required.", field)
    try:
        d = Decimal(str(value).replace("$", "").replace(",", "").strip())
    except Exception:
        raise InvalidInput(f"'{value}' is not an amount.", field)
    if not d.is_finite() or abs(d) > Decimal("9999999999.99"):           # Infinity, NaN, or too big for the NUMERIC(12,2) columns
        raise InvalidInput(f"'{value}' is not a usable amount.", field)
    if d != d.quantize(Decimal("0.01")):
        raise InvalidInput(f"{field} can have at most two decimal places.", field)
    if d < 0 and not allow_negative:
        raise InvalidInput(f"{field} cannot be negative.", field)
    return d.quantize(Decimal("0.01"))


def age_on(birth_date: dt.date | None, on: dt.date | None = None) -> int | None:
    if not birth_date:
        return None
    on = on or today()
    return on.year - birth_date.year - ((on.month, on.day) < (birth_date.month, birth_date.day))


def is_minor(birth_date: dt.date | None, *, household_position: str | None = None,
             deceased_date: dt.date | None = None, on: dt.date | None = None) -> bool:
    """Under 18 is a minor. With no birth date, a person recorded as a Child in a household is treated as a
    minor too (the conservative reading: a child with an unknown age must not leak into a directory).
    A person with a death date is no longer treated as a minor for directory purposes."""
    if deceased_date:
        return False
    a = age_on(birth_date, on)
    if a is not None:
        return a < 18
    return household_position == "child"


# SQL fragment, kept in one place so search, directory and export agree with is_minor() above.
# `p` is the person alias, `hm` a LEFT JOINed current household_member alias.
MINOR_SQL = (
    "(p.deceased_date IS NULL AND ("
    "(p.birth_date IS NOT NULL AND p.birth_date > (CURRENT_DATE - INTERVAL '18 years')) "
    "OR (p.birth_date IS NULL AND hm.position = 'child')))"
)


def person_label(p: dict) -> str:
    """'Last, First' for lists, the organization name for organizations."""
    if not p:
        return ""
    if p.get("record_type") == "organization":
        return p.get("org_name") or ""
    first = (p.get("goes_by") or p.get("first_name") or "").strip()
    last = (p.get("last_name") or "").strip()
    if last and first:
        return f"{last}, {first}"
    return last or first


def person_full_name(p: dict) -> str:
    """'Margaret A. Ellis' for headings."""
    if not p:
        return ""
    if p.get("record_type") == "organization":
        return p.get("org_name") or ""
    mid = (p.get("middle_name") or "").strip()
    parts = [p.get("first_name"), (mid[0] + ".") if len(mid) == 1 else mid, p.get("last_name"), p.get("suffix")]
    return " ".join(x.strip() for x in parts if x and x.strip())


def initials(p: dict) -> str:
    if p.get("record_type") == "organization":
        return ((p.get("org_name") or "?")[:2]).upper()
    f = (p.get("first_name") or "")[:1]
    l = (p.get("last_name") or "")[:1]
    return (f + l).upper() or "?"


# ── Change log ──────────────────────────────────────────────────────────────────────────────────
def ser(value) -> str | None:
    """Canonical text form of a value for change_log (and for undo to read back)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def log_change(cur, ctx: Ctx, table: str, row_id: int | None, field: str | None, old, new, *,
               person_id: int | None = None, kind: str = "update", scope: str = "profile",
               batch_id=None, reason: str | None = None, undo_of_id: int | None = None) -> int:
    """The only writer of donor.change_log. Called inside the same transaction as the change itself."""
    cur.execute(
        "INSERT INTO donor.change_log (table_name, row_id, person_id, field, old_value, new_value, kind, scope,"
        " user_id, parish_id, batch_id, reason, undo_of_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (table, row_id, person_id, field, ser(old), ser(new), kind, scope, ctx.user_id, ctx.parish_id,
         str(batch_id) if batch_id else None, reason, undo_of_id),
    )
    return cur.fetchone()["id"]


def new_batch_id() -> str:
    return str(uuid.uuid4())


def diff_fields(old_row: dict, new_values: dict) -> list[tuple[str, object, object]]:
    """(field, old, new) for every key in new_values whose value differs from old_row."""
    out = []
    for k, v in new_values.items():
        if old_row.get(k) != v:
            out.append((k, old_row.get(k), v))
    return out
