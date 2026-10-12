"""
statement_store.py -- Beacon Donor Management: where BUILT statements (a staff run's PDFs) are kept. Private files in the Beacon files bucket under
donor-statements/<parish id>/<run id>/<statement id>.pdf, never a public address; every download goes through a route that checks the signed-in person and the parish.
A member's own statement is NOT stored. The app role cannot delete anything, and Beacon never deletes a statement file (Plan section 4e: kept seven years).

  put(path, data) -> sha256 hex        get(path) -> bytes | None        path_for(parish_id, run_id, statement_id) -> str

Tests replace `put` and `get` with an in-memory dictionary, so no test writes to the real bucket.
"""
from __future__ import annotations

import hashlib
import os

_BEACON_ENV = os.environ.get("BEACON_ENV", "dev")
BUCKET = "cfm-beacon-files-prod" if _BEACON_ENV == "prod" else "cfm-beacon-files-dev"


def path_for(parish_id: int, run_id: int, statement_id: int) -> str:
    return f"donor-statements/{int(parish_id)}/{int(run_id)}/{int(statement_id)}.pdf"


def put(path: str, data: bytes) -> str:
    import gcs_client
    gcs_client.upload_bytes(BUCKET, path, data, "application/pdf")
    return hashlib.sha256(data).hexdigest()


def get(path: str) -> bytes | None:
    import gcs_client
    got = gcs_client.download_bytes(BUCKET, path)
    return got[0] if got else None
