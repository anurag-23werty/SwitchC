"""Async connection pool for Khata's database (schema `khata`)."""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


def make_pool(url: str, max_size: int = 10) -> AsyncConnectionPool:
    """A pool whose connections see the `khata` schema and return dict rows.

    Open it with `await pool.open()` (or `async with pool:`); nothing connects until then.
    """
    return AsyncConnectionPool(
        url,
        min_size=1,
        max_size=max_size,
        open=False,
        kwargs={
            "options": "-c search_path=khata,public",
            "row_factory": dict_row,
            "autocommit": False,
        },
    )
