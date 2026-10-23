"""
db.py — PostgreSQL access for the 26-129 check-request website (checkreq schema).

Same connection pattern as 26-124's pg_store.py: Cloud Run uses
INSTANCE_CONNECTION_NAME (unix socket /cloudsql/<name>); local dev uses
PGHOST/PGPORT (Cloud SQL Auth Proxy) + PGPASSWORD.
"""
from __future__ import annotations

import atexit
import contextvars
import os
import threading
import weakref
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
#
# 2026-10-23 (Jay: "we should not be re-opening connections ever"): the holder is now
# keyed by target database, and query()/query_one() ALSO reuse one connection outside a
# request -- a per-thread holder for scripts, loaders, tests and plain threads (a 200-row
# load had taken an hour because every query opened a connection). query(dbname=...)
# overrides (fund_account_masks) reuse one connection per request per database. A
# connection that died while idle is replaced and a plain read is retried once.
# connect() is still one connection per explicit write transaction.
# Backup of the prior version: db_pre_thread_connection.py.
# Autocommit makes each query() its own transaction, which is exactly what
# the old connect-per-call path did, and means one failed statement can't
# leave the shared connection stuck in an aborted transaction.
# Backup of the prior version: db_pre_request_connection.py.
# ---------------------------------------------------------------------------
_request_conn: contextvars.ContextVar = contextvars.ContextVar("beacon_request_conn", default=None)


class _RequestConnection:
    """Mutable holder, so a connection opened inside a threadpool-run sync
    route (which gets a COPY of the context) is still visible to the
    middleware that closes it. Holds one autocommit connection per target:
    the environment's own database, or a named override (e.g. cfmqbo)."""

    def __init__(self):
        self._conns: dict = {}
        self._lock = threading.Lock()
        self._closed = False

    @staticmethod
    def _key(kw: dict) -> tuple:
        return (kw.get("host"), kw.get("port"), kw["dbname"], kw["user"])

    def get(self, dbname: str | None = None):
        kw = _connkwargs(dbname)
        key = self._key(kw)
        with self._lock:
            if self._closed:
                return None
            conn = self._conns.get(key)
            if conn is None or conn.closed or conn.broken:
                conn = psycopg.connect(**kw, row_factory=dict_row, autocommit=True)
                self._conns[key] = conn
            return conn

    def drop(self, dbname: str | None = None) -> None:
        """Forget (and close) the connection for a target, so the next get() opens a fresh one."""
        key = self._key(_connkwargs(dbname))
        with self._lock:
            conn = self._conns.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def close(self):
        with self._lock:
            self._closed = True
            conns, self._conns = list(self._conns.values()), {}
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass


# Outside a web request (a script, a loader, a test, a plain thread) each thread keeps its own holder, so a run of a few
# hundred queries opens one connection, not one per query. Closed at interpreter exit.
_thread = threading.local()
_thread_holders: "weakref.WeakSet[_RequestConnection]" = weakref.WeakSet()


def _holder() -> _RequestConnection:
    h = _request_conn.get()
    if h is not None:
        return h
    h = getattr(_thread, "holder", None)
    if h is None:
        h = _thread.holder = _RequestConnection()
        _thread_holders.add(h)
    return h


@atexit.register
def _close_thread_holders() -> None:
    for h in list(_thread_holders):
        h.close()


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


def _is_read(sql: str) -> bool:
    return sql.lstrip().lower().startswith(("select", "with", "show", "table"))


def _run(conn, sql: str, params: tuple) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query(sql: str, params: tuple = (), dbname: str | None = None) -> list[dict]:
    holder = _holder()
    conn = holder.get(dbname)
    if conn is not None:
        try:
            return _run(conn, sql, params)
        except psycopg.OperationalError:
            # the connection died while idle (server restart, idle timeout): replace it, and retry a plain read once.
            # A write is never retried, since it may already have run.
            if not (conn.closed or conn.broken) or not _is_read(sql):
                raise
            holder.drop(dbname)
            conn = holder.get(dbname)
            if conn is None:
                raise
            return _run(conn, sql, params)
    with connect(dbname) as conn:  # the request is already over and its holder closed: one-off connection, as before
        return _run(conn, sql, params)


def query_one(sql: str, params: tuple = (), dbname: str | None = None) -> dict | None:
    rows = query(sql, params, dbname=dbname)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = (), dbname: str | None = None) -> int:
    """One write statement (INSERT / UPDATE / DELETE / DDL, no result rows) on the SAME shared autocommit connection
    query() uses. Returns the rowcount. Autocommit makes the statement its own transaction, which is exactly what the
    one-statement `with db.connect() as conn: cur.execute(...)` it replaces did, minus a brand-new connection (about
    0.4 s through the proxy) per call. Never retried: a write that died on a broken connection may already have run.
    For several statements that must commit together use connect() (own connection, own transaction) or
    shared_transaction()."""
    conn = _holder().get(dbname)
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount
    with connect(dbname) as conn:  # the request is already over and its holder closed: one-off connection, as before
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


@contextmanager
def shared_transaction(dbname: str | None = None):
    """Yield a dict-row connection inside ONE transaction on the shared connection: commit on success, roll back on any
    exception. Nested uses become savepoints, so an inner failure the caller catches rolls back only the inner part.
    Use for read/write units of work that do not need a connection of their own (no session advisory lock, no FOR UPDATE
    that must be released before the next step): those keep using connect()."""
    conn = _holder().get(dbname)
    if conn is None:  # holder closed: one-off connection, as before
        with connect(dbname) as one_off:
            yield one_off
        return
    with conn.transaction():
        yield conn

# H6 deploy-identity-split verification marker (2026-09-19) -- comment-only, no behavior change.
