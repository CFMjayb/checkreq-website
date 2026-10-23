"""
logic/hr_lifecycle -- 26-162 Beacon Onboarding and Offboarding: the business rules, kept apart from the screens and the API.

Plan: Claude Working Folder\\26-162 Beacon Onboarding and Offboarding\\Plan - DRAFT.md (decisions in its section 0).

    conditions.py   the small grammar on a checklist item ("clergy", "lay and pension_eligible", "org=Claggett", ...)
    dates.py        due dates from an anchor date (hire, last day, benefits effective), business days, the Before / Day 1 buckets
    eligibility.py  scheduled hours -> health (1,500 hrs/yr) and lay pension (1,000 hrs/yr) eligibility
    templates.py    the versioned checklist templates (schema hr, loaded from templates\\checklist_templates.csv)
    cases.py        open, preview, update, item status, enrollments, close / withdraw, start offboarding
    board.py        the HR Status Board: one row per open case, a chip per section, the summary strip
    roster_link.py  completing a case adds the person to (or inactivates them on) portal.staff_roster

Nothing here imports FastAPI. Screens (hr_cases.py) and the service API (hr_api.py) only check access, call one function
here and shape the answer, so the API maps 1:1 onto named tools and can become an MCP server without rework.

THE HARD RULE (same as 26-158's "Beacon never stores pay"): nothing in schema hr holds an SSN/ITIN, a birth date, dependent
details, a bank or account number, salary, TAC, housing amounts or retirement deferrals. Those stay on the signed forms.
Free-text notes pass through privacy.check_text() before they are saved.
"""
