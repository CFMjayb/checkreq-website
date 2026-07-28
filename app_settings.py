"""
app_settings.py — tiny generic key/value settings store (checkreq.app_settings,
migrations/014_app_settings.sql). First use: email Test Mode (Jay, 2026-07-28)
-- see email_client.py's _apply_test_mode() and main.py's /admin/test-mode route.

Deliberately a live, in-app-toggleable setting rather than a Cloud Run env var
-- this app's env vars can only be changed via `gcloud run services update`,
which this project has repeatedly needed Jay's own elevated login for (the
ambient service-account identity lacks `iam.serviceaccounts.actAs` on the
Cloud Run service's runtime SA). A CFO flipping Test Mode on/off while
actively testing should not need a redeploy or a terminal.
"""
from __future__ import annotations

import db


def get_setting(key: str, default: str | None = None) -> str | None:
    """Fails soft to `default` on any DB error -- a settings-read hiccup must
    never crash whatever feature is checking it (matches email_client.py's
    own stated fail-soft philosophy)."""
    try:
        row = db.query_one("SELECT value FROM checkreq.app_settings WHERE key = %s", (key,))
        return row["value"] if row else default
    except Exception:
        return default


def set_setting(key: str, value: str, updated_by_user_id: int | None = None) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkreq.app_settings (key, value, updated_by_user_id, updated_at) "
                "VALUES (%s, %s, %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                "updated_by_user_id = EXCLUDED.updated_by_user_id, updated_at = NOW()",
                (key, value, updated_by_user_id),
            )
