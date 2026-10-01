"""Postgres = the pipeline's operational state: run log, idempotency keys, and a durable work queue.
Google Sheets stays the business-facing system of record."""
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

pool: ConnectionPool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id               TEXT PRIMARY KEY,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    channel          TEXT NOT NULL,
    filename         TEXT,
    file_sha256      TEXT,
    file_size        INTEGER,
    pdf              BYTEA,
    status           TEXT NOT NULL,      -- RECEIVED | PROCESSING | RETRY_SCHEDULED | SUCCESS | DUPLICATE | NEEDS_REVIEW | REJECTED | FAILED
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TIMESTAMPTZ,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    duration_ms      INTEGER,
    error            TEXT,
    duplicate_of     TEXT,
    extracted        JSONB,
    order_id         TEXT,
    sheet_url        TEXT,
    events           JSONB NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS runs_created_idx ON runs (created_at DESC);
CREATE INDEX IF NOT EXISTS runs_queue_idx ON runs (status, next_attempt_at);

-- Layer 1 idempotency: exact same file bytes
CREATE TABLE IF NOT EXISTS documents (
    file_sha256  TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL
);

-- Layer 2 idempotency: same order content arriving as a different file (re-scan, re-fax, other channel)
CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT PRIMARY KEY,
    content_key  TEXT NOT NULL UNIQUE,
    run_id       TEXT NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    sheet_row    INTEGER,
    fields       JSONB NOT NULL
);
"""


def init():
    global pool
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=10,
                          kwargs={"row_factory": dict_row}, open=True)
    with conn() as c:
        c.execute(SCHEMA)


@contextmanager
def conn():
    """Yields a connection inside a transaction (commit on success, rollback on error)."""
    assert pool is not None
    with pool.connection() as c:
        yield c
