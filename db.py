"""
db.py — PostgreSQL access for the 26-129 check-request website (checkreq schema).

Same connection pattern as 26-124's pg_store.py: Cloud Run uses
INSTANCE_CONNECTION_NAME (unix socket /cloudsql/<name>); local dev uses
PGHOST/PGPORT (Cloud SQL Auth Proxy) + PGPASSWORD.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


def _connkwargs(dbname: str | None = None) -> dict:
    dbname = dbname or os.environ.get("PGDATABASE", "cfmqbo")
    user   = os.environ.get("PGUSER", "postgres")
    pwd    = os.environ.get("PGPASSWORD", "")
    inst   = os.environ.get("INSTANCE_CONNECTION_NAME", "")
    if inst:
        return dict(host=f"/cloudsql/{inst}", dbname=dbname, user=user, password=pwd)
    host = os.environ.get("PGHOST", "127.0.0.1")
    port = int(os.environ.get("PGPORT", "5432"))
    return dict(host=host, port=port, dbname=dbname, user=user, password=pwd, sslmode="disable")


@contextmanager
def connect(dbname: str | None = None):
    """dbname: explicit override, bypassing PGDATABASE. Only needed for a
    table that deliberately has no dev/prod split (e.g. fund_account_masks,
    2026-09-19 -- see cornerstone_mode.py) and must be reached the same way
    regardless of which environment this app is running as."""
    conn = psycopg.connect(**_connkwargs(dbname), row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params: tuple = (), dbname: str | None = None) -> list[dict]:
    with connect(dbname) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def query_one(sql: str, params: tuple = (), dbname: str | None = None) -> dict | None:
    rows = query(sql, params, dbname=dbname)
    return rows[0] if rows else None
