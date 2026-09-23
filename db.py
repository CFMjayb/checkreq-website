"""
db.py — PostgreSQL access for the 26-129 check-request website (checkreq schema).

Same connection pattern as 26-124's pg_store.py: Cloud Run uses
INSTANCE_CONNECTION_NAME (unix socket /cloudsql/<name>); local dev uses
PGHOST/PGPORT (Cloud SQL Auth Proxy) + PGPASSWORD.
"""
from __future__ import annotations

import contextvars
import os
import threading
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


def _connkwargs(dbname: str | None = None) -> dict:
    # .strip() -- 2026-09-21 H7 incident: Secret-Manager-mounted PGPASSWORD
    # consistently failed auth from inside Cloud Run while the identical
    # byte-verified value worked as a plain literal env var and over a
    # separate local TCP connection -- same bug class as 26-141's
    # qbo_client.py._secret() fix. See db_pre_h7_password_strip_fix.py.
    dbname = dbname or os.environ.get("PGDATABASE", "cfmqbo")
    user   = os.environ.get("PGUSER", "postgres").strip()
    pwd    = os.environ.get("PGPASSWORD", "").strip()
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


# ---------------------------------------------------------------------------
# One connection per page load (2026-09-23, Jay: "initial login should pull
# all queries for one connection"). Measured before this change: /portal
# opened 95 separate connections, a typical page ~60 -- every query()/
# query_one() call opened and closed its own. RequestConnectionMiddleware
# (wired in main.py, outermost) gives each HTTP request one lazily-opened
# AUTOCOMMIT connection that query()/query_one() reuse, closed when the
# request finishes.
#
# Deliberately unchanged:
#   - connect() (explicit write transactions) always opens its OWN
#     connection, so commit/rollback behaviour is byte-identical to before.
#   - query(dbname=...) overrides (fund_account_masks) use their own
#     connection -- the shared one is always the environment's PGDATABASE.
#   - Outside a request (scripts, plain threads, which don't inherit
#     contextvars) there is no holder, so the old per-call path is used.
# Autocommit makes each query() its own transaction, which is exactly what
# the old connect-per-call path did, and means one failed statement can't
# leave the shared connection stuck in an aborted transaction.
# Backup of the prior version: db_pre_request_connection.py.
# ---------------------------------------------------------------------------
_request_conn: contextvars.ContextVar = contextvars.ContextVar("beacon_request_conn", default=None)


class _RequestConnection:
    """Mutable holder, so a connection opened inside a threadpool-run sync
    route (which gets a COPY of the context) is still visible to the
    middleware that closes it."""

    def __init__(self):
        self._conn = None
        self._lock = threading.Lock()
        self._closed = False

    def get(self):
        with self._lock:
            if self._closed:
                return None
            if self._conn is None or self._conn.closed or self._conn.broken:
                self._conn = psycopg.connect(**_connkwargs(), row_factory=dict_row, autocommit=True)
            return self._conn

    def close(self):
        with self._lock:
            self._closed = True
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None


class RequestConnectionMiddleware:
    """Pure ASGI middleware (not BaseHTTPMiddleware) so it adds no extra
    task hop. FastAPI BackgroundTasks run before self.app() returns, so
    they still see an open connection."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        holder = _RequestConnection()
        token = _request_conn.set(holder)
        try:
            await self.app(scope, receive, send)
        finally:
            _request_conn.reset(token)
            holder.close()


def query(sql: str, params: tuple = (), dbname: str | None = None) -> list[dict]:
    holder = _request_conn.get()
    if holder is not None and dbname is None:
        conn = holder.get()
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
    with connect(dbname) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def query_one(sql: str, params: tuple = (), dbname: str | None = None) -> dict | None:
    rows = query(sql, params, dbname=dbname)
    return rows[0] if rows else None

# H6 deploy-identity-split verification marker (2026-09-19) -- comment-only, no behavior change.
