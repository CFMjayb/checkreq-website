"""
logic/overview_docs -- 26-163 Beacon Overview Library: the business rules, kept apart from the screens.

    sanitize.py   the allowlist sanitizer (standard library only): fragment and document formats, CSS scrubbing
    render.py     link rewriting and the iframe page (build_srcdoc), no database
    store.py      who may read (cornerstone_employee at any org) and the two reads: list_docs, get_doc

Nothing here imports FastAPI. The screens and the two read tools (overview_docs.py) only check access, call one function here and
shape the answer, so each tool maps 1:1 onto a named tool (beacon_list_overview_docs, beacon_get_overview_doc) and can become an
MCP server later without rework. Documents are written by Tools\\publish_overview_docs.py, never by a Beacon route.

This file is deliberately empty of imports: the sanitizer and renderer must be usable (and testable) without a database.
"""
