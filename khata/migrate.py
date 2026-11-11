"""Schema migrations: numbered SQL files in db/migrations, applied once each, in order.

    uv run khata db migrate     # apply what's pending
    uv run khata db status      # what's applied, what's pending

A real deployment keeps its data across upgrades: never edit an applied
migration, add the next numbered file instead. Applied versions are recorded in
`khata.schema_migrations`; an advisory lock stops two processes migrating at once.
A database created before migrations existed (the original schema.sql) is
recognised and recorded as being at 001.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"
LOCK_ID = 7_301_955_201  # any constant; shared by every Khata process


def available() -> list[str]:
    return sorted(p.stem for p in MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))


def _ensure_table(conn: psycopg.Connection) -> None:
    conn.execute("CREATE SCHEMA IF NOT EXISTS khata")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS khata.schema_migrations (
               version    text PRIMARY KEY,
               applied_at timestamptz NOT NULL DEFAULT now())"""
    )


def applied(conn: psycopg.Connection) -> list[str]:
    exists = conn.execute(
        "SELECT to_regclass('khata.schema_migrations') IS NOT NULL"
    ).fetchone()[0]
    if not exists:
        return []
    return [
        r[0]
        for r in conn.execute(
            "SELECT version FROM khata.schema_migrations ORDER BY version"
        ).fetchall()
    ]


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply pending migrations in one transaction. Returns the versions applied."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        _ensure_table(conn)
        done = set(applied(conn))
        legacy = conn.execute(
            "SELECT to_regclass('khata.company') IS NOT NULL"
        ).fetchone()[0]
        first = available()[0]
        if legacy and first not in done:  # built from the old schema.sql
            conn.execute(
                "INSERT INTO khata.schema_migrations (version) VALUES (%s)", (first,)
            )
            done.add(first)
        new = []
        for version in available():
            if version in done:
                continue
            conn.execute((MIGRATIONS / f"{version}.sql").read_text())
            conn.execute(
                "INSERT INTO khata.schema_migrations (version) VALUES (%s)", (version,)
            )
            new.append(version)
        conn.execute("SET search_path = khata, public")
    return new


def migrate_url(url: str) -> list[str]:
    with psycopg.connect(url) as conn:
        return migrate(conn)


def pending_url(url: str) -> list[str]:
    with psycopg.connect(url) as conn:
        done = set(applied(conn))
    return [v for v in available() if v not in done]
