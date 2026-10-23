"""
conditions.py -- the condition on a checklist item (hr.template_items.condition). Pure: no database.

Grammar (Plan section 9). Blank = always applies.
    tokens     clergy | lay | health_eligible | pension_eligible | housing_provided
               org=<entity code>            e.g. org=Claggett
               enrolled:<coverage>          true when the linked onboarding case enrolled that coverage (offboarding)
    operators  not, and, or      ("and" binds tighter than "or"; no parentheses in v1)

Facts (a dict): is_clergy (bool), health_eligible / pension_eligible (bool or None = hours not known yet),
housing_provided (bool or None), org_code (str), enrolled (set of coverage keys, or None when no coverage has been
recorded for the person at all -- then every enrolled:<coverage> counts as true, so offboarding never silently drops the
coverage-end and continuation-notice items).

Unknown eligibility counts as eligible, so an item is never silently dropped before the hours are entered; HR marks it
N/A if it turns out not to apply. Unknown housing counts as "no" (the worksheet appears only when housing is marked yes).
"""
from __future__ import annotations

COVERAGES = {"medical", "dental", "life", "std", "ltd", "retirement", "clergy_pension"}
SIMPLE = {"clergy", "lay", "health_eligible", "pension_eligible", "housing_provided"}


class ConditionError(ValueError):
    pass


def _tokens(text: str) -> list[str]:
    return [t for t in (text or "").replace("(", " ( ").replace(")", " ) ").split() if t]


def validate(text: str | None) -> None:
    """Raises ConditionError for a condition the evaluator would not understand (used by the template loader)."""
    toks = _tokens(text or "")
    if not toks:
        return
    if "(" in toks or ")" in toks:
        raise ConditionError("Parentheses are not supported in conditions yet.")
    expect_atom = True
    for t in toks:
        lt = t.lower()
        if expect_atom:
            if lt == "not":
                continue
            _check_atom(t)
            expect_atom = False
        else:
            if lt not in ("and", "or"):
                raise ConditionError(f"Expected 'and' or 'or' before '{t}'.")
            expect_atom = True
    if expect_atom:
        raise ConditionError("A condition cannot end with an operator.")


def _check_atom(t: str) -> None:
    lt = t.lower()
    if lt in SIMPLE:
        return
    if lt.startswith("org="):
        if not t[4:]:
            raise ConditionError("org= needs an entity code.")
        return
    if lt.startswith("enrolled:"):
        if lt[9:] not in COVERAGES:
            raise ConditionError(f"Unknown coverage in '{t}'. Use one of: {', '.join(sorted(COVERAGES))}.")
        return
    raise ConditionError(f"Unknown condition '{t}'.")


def _atom(t: str, facts: dict) -> bool:
    lt = t.lower()
    if lt == "clergy":
        return bool(facts.get("is_clergy"))
    if lt == "lay":
        return not facts.get("is_clergy")
    if lt in ("health_eligible", "pension_eligible"):
        v = facts.get(lt)
        return True if v is None else bool(v)
    if lt == "housing_provided":
        return bool(facts.get("housing_provided"))
    if lt.startswith("org="):
        return (facts.get("org_code") or "").lower() == t[4:].lower()
    if lt.startswith("enrolled:"):
        enrolled = facts.get("enrolled")
        if enrolled is None:  # no coverage recorded anywhere yet: unknown, so the ending item stays on the list
            return True
        return lt[9:] in enrolled
    raise ConditionError(f"Unknown condition '{t}'.")


def evaluate(text: str | None, facts: dict) -> bool:
    toks = _tokens(text or "")
    if not toks:
        return True
    validate(text)
    # split on "or" into terms, each term an "and" chain of (not)* atoms
    terms, cur = [], []
    for t in toks:
        if t.lower() == "or":
            terms.append(cur)
            cur = []
        else:
            cur.append(t)
    terms.append(cur)
    for term in terms:
        ok, neg = True, False
        for t in term:
            lt = t.lower()
            if lt == "and":
                continue
            if lt == "not":
                neg = not neg
                continue
            v = _atom(t, facts)
            if neg:
                v, neg = (not v), False
            ok = ok and v
        if ok:
            return True
    return False


def describe(text: str | None) -> str:
    """A plain-words version for the screen: 'lay and pension_eligible' -> 'lay staff and pension eligible'."""
    if not (text or "").strip():
        return "always"
    words = {"clergy": "clergy", "lay": "lay staff", "health_eligible": "health eligible",
             "pension_eligible": "pension eligible", "housing_provided": "housing provided"}
    out = []
    for t in _tokens(text):
        lt = t.lower()
        if lt in words:
            out.append(words[lt])
        elif lt.startswith("org="):
            out.append(t[4:])
        elif lt.startswith("enrolled:"):
            out.append(f"enrolled in {lt[9:].replace('_', ' ')}")
        else:
            out.append(lt)
    return " ".join(out)
