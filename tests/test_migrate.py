"""Migrations: a fresh database gets the full schema once; old databases are recognised;
the service refuses to start on a database that is behind."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql

from khata import migrate
from khata.config import get_settings

DB = "khata_migrate_test"


@pytest.fixture
def empty_db():
    admin = get_settings().khata_database_url
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as c:
            c.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DB)))
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB)))
    except psycopg.OperationalError:
        pytest.skip("khata-db not reachable; run `make up`")
    return admin.rsplit("/", 1)[0] + "/" + DB


def test_fresh_database_is_migrated_once(empty_db):
    assert migrate.pending_url(empty_db) == migrate.available()
    assert migrate.migrate_url(empty_db) == migrate.available()
    assert migrate.migrate_url(empty_db) == []
    assert migrate.pending_url(empty_db) == []
    with psycopg.connect(empty_db) as c:
        assert c.execute("SELECT count(*) FROM khata.company").fetchone()[0] == 0


def test_a_database_from_the_old_schema_file_is_recognised(empty_db):
    with psycopg.connect(empty_db) as c:
        c.execute((migrate.MIGRATIONS / "001_initial.sql").read_text())
        c.commit()
    applied = migrate.migrate_url(empty_db)
    assert "001_initial" not in applied  # recorded as already there, not re-run
    assert migrate.pending_url(empty_db) == []


@pytest.mark.asyncio
async def test_the_service_refuses_a_database_that_is_behind(empty_db):
    from khata.runtime import Khata

    with pytest.raises(RuntimeError, match="khata db migrate"):
        async with Khata.open(database_url=empty_db, guard=False):
            pass
