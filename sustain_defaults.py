"""
sustain_defaults.py -- which of a diocese's projection defaults are still unset, and the plain words to say so (26-159 Congregational
Vitality, Parish Sustainability Review). Pure functions: no database, no web code.

Why this exists: the "Can we afford a priest?" calculator starts from the diocese's own rates (sustain.assumption_default). A diocese
that has not set them yet (production's Southwestern Virginia on 2026-10-18) used to crash the parish's review page with a 500, because
logic.sustain.inputs.build_inputs raises InputError("The diocese has not set: pension_rate, ...") and the page did not catch it. The
pages now ask missing_defaults() first, show a short notice instead of the calculator, and turn the leftover InputError, if one still
reaches a JSON call, into friendly_error()'s sentence. Nothing here invents a value: whether to set one is the diocese's decision.
"""
from __future__ import annotations

from collections.abc import Mapping

# The defaults build_inputs will not run without (health_annual may stay blank: the page then says "Health insurance not included.").
REQUIRED_KEYS = ("pension_rate", "income_growth", "pledge_growth", "salary_growth", "other_exp_growth", "safe_draw_rate")

LABELS = {
    "pension_rate": "pension rate",
    "income_growth": "giving and income growth",
    "pledge_growth": "pledge growth",
    "salary_growth": "priest pay raise",
    "other_exp_growth": "other costs growth",
    "safe_draw_rate": "safe endowment draw",
}

# What logic.sustain.inputs.build_inputs starts its error with when a default is missing (logic/ is a byte copy kept in step with the
# Neighborhood project, so this file recognizes the words rather than changing that module).
MISSING_PREFIX = "The diocese has not set:"

NOT_SET_SENTENCE = "The diocese has not set its projection defaults yet."


def missing_defaults(defaults: Mapping | None) -> list[str]:
    """The required default keys that are absent or blank, in REQUIRED_KEYS order. [] when the diocese has set them all."""
    have = defaults or {}
    return [k for k in REQUIRED_KEYS if have.get(k) is None]


def labels(keys) -> list[str]:
    """Plain-words names for default keys (an unknown key is shown as it is)."""
    return [LABELS.get(k, k) for k in keys]


def friendly_error(message: str) -> str:
    """The message to show a person. The 'diocese has not set: pension_rate, ...' error becomes one plain sentence with no field names
    in it; every other message (a salary out of range, a report that cannot be projected) is returned unchanged."""
    if isinstance(message, str) and message.startswith(MISSING_PREFIX):
        return NOT_SET_SENTENCE
    return message
