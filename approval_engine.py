"""
approval_engine.py — builds the approval chain for a payment_request.

Replaces Power Automate Flow 1's chain-construction logic (Plan.md). Pure
functions, no side effects — callers persist the result and drive the actual
approve/reject/escalate state machine.

Chain rule (per Plan.md, updated 2026-07-31 by the Approval Workflow
Corrections plan):
  1. Active approval_rules for the request's program_area, ordered by serial_group.
  2. If amount >= the REQUESTING ORG's own global_approval_threshold (Jay's
     correction: this used to be a per-approver-row threshold_amount that
     could drift inconsistently within one entity -- now a single, clearly-
     owned value per organization), every active global_approvers row for
     THAT org is appended as one additional serial group after the program
     area's own rules. Only one of them needs to actually approve to clear
     the group (see main.py's _perform_approval -- this module only builds
     the chain, it doesn't decide how many sign-offs a group needs)."""
from __future__ import annotations

import db


def build_approval_chain(program_area_id: int, org_id: int, amount: float) -> list[dict]:
    """Return an ordered list of {serial_group, approver_user_id, approver_email,
    approver_name, must_approve, backup_approver_id} steps. Empty list means
    no approval rule is configured for this program area — caller must not
    silently let the request through; treat as a data-setup gap."""
    rules = db.query(
        """
        SELECT ar.serial_group, ar.approver_user_id, ar.approval_limit,
               ar.must_approve_flag, ar.must_approve_threshold,
               ar.backup_approver_id, u.email, u.display_name
        FROM checkreq.approval_rules ar
        JOIN checkreq.app_users u ON u.id = ar.approver_user_id
        WHERE ar.program_area_id = %s AND ar.is_active = TRUE
        ORDER BY ar.serial_group, ar.approver_user_id
        """,
        (program_area_id,),
    )

    chain = []
    for r in rules:
        # An approver is included if the amount is within their limit, OR
        # their must-approve flag fires above must_approve_threshold.
        included = (
            float(r["approval_limit"]) >= amount
            or (r["must_approve_flag"] and amount > float(r["must_approve_threshold"]))
        )
        if included:
            chain.append({
                "serial_group": r["serial_group"],
                "approver_user_id": r["approver_user_id"],
                "approver_email": r["email"],
                "approver_name": r["display_name"],
                "backup_approver_id": r["backup_approver_id"],
            })

    org_row = db.query_one(
        "SELECT global_approval_threshold FROM checkreq.organizations WHERE id = %s",
        (org_id,),
    )
    threshold = float(org_row["global_approval_threshold"]) if org_row else 5000.0

    global_rows = []
    if amount >= threshold:
        global_rows = db.query(
            """
            SELECT ga.approver_user_id, ga.backup_approver_id, ga.serial_group,
                   u.email, u.display_name
            FROM checkreq.global_approvers ga
            JOIN checkreq.app_users u ON u.id = ga.approver_user_id
            WHERE ga.is_active = TRUE AND ga.org_id = %s
            """,
            (org_id,),
        )
    if global_rows:
        next_group = (max((c["serial_group"] for c in chain), default=0)) + 1
        for r in global_rows:
            chain.append({
                "serial_group": next_group,
                "approver_user_id": r["approver_user_id"],
                "approver_email": r["email"],
                "approver_name": r["display_name"],
                "backup_approver_id": r["backup_approver_id"],
                # Only one Global Approver needs to actually approve to clear
                # this group -- see main.py's _perform_approval short-circuit.
                # Found missing here by task #83's own end-to-end test (a
                # 2-approver EDOM group materialized with any_one_suffices
                # False on both rows despite task #80's main.py-side work
                # being complete) -- the flag was never actually set on this
                # chain step to begin with.
                "any_one_suffices": True,
            })

    return chain


def describe_chain(chain: list[dict]) -> str:
    """Human-readable summary for the ApprovalChainSummary field (mirrors the
    Plan.md example: 'Submitted by ...\\nApproved by ...')."""
    if not chain:
        return "No approval rule configured for this program area — needs setup."
    groups: dict[int, list[str]] = {}
    for step in chain:
        groups.setdefault(step["serial_group"], []).append(step["approver_name"] or step["approver_email"])
    lines = []
    for grp in sorted(groups):
        names = ", ".join(groups[grp])
        lines.append(f"Group {grp}: {names}")
    return "\n".join(lines)
