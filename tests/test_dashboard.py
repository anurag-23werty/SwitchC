"""The read-only dashboard: every view answers from Postgres, and nothing writes."""

from __future__ import annotations

import psycopg
from fastapi.testclient import TestClient

from khata.dashboard import create_app


def _counts(url: str) -> tuple:
    with psycopg.connect(url) as c:
        return tuple(
            c.execute(f"SELECT count(*) FROM khata.{t}").fetchone()[0]
            for t in ("invoices", "decisions", "audit_log", "rules")
        )


def test_every_view_answers_and_nothing_is_written(test_db_url):
    before = _counts(test_db_url)
    with TestClient(create_app(test_db_url, token="")) as c:
        page = c.get("/")
        assert page.status_code == 200 and "Khata" in page.text
        s = c.get("/api/summary").json()
        assert s["policy_version"] == 1 and s["llm"]["cost_usd"] == 0
        assert "PAYMENT_DRAFTED" in s["by_state"]  # the seeded history
        rows = c.get("/api/invoices").json()
        assert rows and {"id", "state", "flags", "vendor_code"} <= set(rows[0])
        drafted = c.get("/api/invoices", params={"state": "PAYMENT_DRAFTED"}).json()
        assert drafted and all(r["state"] == "PAYMENT_DRAFTED" for r in drafted)
        assert c.get("/api/invoices", params={"state": "PAID"}).status_code == 400
        one = c.get(f"/api/invoices/{rows[-1]['id']}").json()
        assert {"history", "decisions", "notes", "tool_calls", "llm_calls"} <= set(one)
        assert c.get("/api/invoices/999999").status_code == 404
        assert c.get("/api/audit", params={"limit": 5}).status_code == 200
        r = c.get("/api/rules").json()
        assert r["versions"][0]["version"] == 1 and r["proposals"] == []
        for method in (c.post, c.put, c.delete):
            assert method("/api/invoices/1").status_code == 405
    assert _counts(test_db_url) == before


def test_a_token_is_required_when_one_is_set(test_db_url):
    with TestClient(create_app(test_db_url, token="s3cret")) as c:
        assert c.get("/api/summary").status_code == 401
        assert c.get("/", params={"token": "nope"}).status_code == 401
        ok = c.get("/api/summary", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert c.get("/", params={"token": "s3cret"}).status_code == 200
