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


def _connkwargs() -> dict:
    dbname = os.environ.get("PGDATABASE", "cfmqbo")
    user   = os.environ.get("PGUSER", "postgres")
    pwd    = os.environ.get("PGPASSWORD", "")
    inst   = os.environ.get("INSTANCE_CONNECTION_NAME", "")
    if inst:
        return dict(host=f"/cloudsql/{inst}", dbname=dbname, user=user, password=pwd)
    host = os.environ.get("PGHOST", "127.0.0.1")
    port = int(os.environ.get("PGPORT", "5432"))
    return dict(host=host, port=port, dbname=dbname, user=user, password=pwd, sslmode="disable")


@contextmanager
def connect():
    conn = psycopg.connect(**_connkwargs(), row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None
