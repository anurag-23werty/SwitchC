"""A read-only web view of Khata's books: `uv run khata dashboard` -> http://127.0.0.1:8088

Invoice queue, each invoice's full history (states, checks, agent notes, LLM
and tool calls, decisions, payment draft), the audit trail, policy versions and
the Policy Steward's proposals, and running metrics. It reads Postgres and
changes nothing: decisions are made in the Switch room, where the team works.

Bound to localhost by default. Set KHATA_DASHBOARD_TOKEN to require
`Authorization: Bearer <token>` (or `?token=`) when it is exposed anywhere else.
"""

from __future__ import annotations

import hmac
import json
import os
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg_pool import AsyncConnectionPool

from khata import repo, steward
from khata.config import ROOT, get_settings
from khata.db import make_pool

PAGE = Path(__file__).with_name("dashboard.html")
STATES = [
    "RECEIVED",
    "EXTRACTED",
    "VALIDATED",
    "MATCHED",
    "REVIEWED",
    "AWAITING_APPROVAL",
    "ON_HOLD",
    "APPROVED",
    "PAYMENT_DRAFTED",
    "REJECTED",
]


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


async def _all(pool: AsyncConnectionPool, sql: str, *args: Any) -> list[dict]:
    async with pool.connection() as conn:
        return await (await conn.execute(sql, args)).fetchall()


def create_app(database_url: str | None = None, token: str | None = None) -> FastAPI:
    url = database_url or get_settings().khata_database_url
    token = token if token is not None else os.environ.get("KHATA_DASHBOARD_TOKEN", "")
    pool = make_pool(url, max_size=4)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await pool.open()
        yield
        await pool.close()

    app = FastAPI(title="Khata dashboard", lifespan=lifespan, docs_url=None)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if token:
            given = request.headers.get("authorization", "").removeprefix("Bearer ")
            given = given or request.query_params.get("token", "")
            if not hmac.compare_digest(given, token):
                return JSONResponse({"error": "unauthorised"}, status_code=401)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return PAGE.read_text()

    @app.get("/api/summary")
    async def summary() -> dict[str, Any]:
        by_state = {
            r["state"]: {"n": r["n"], "total": r["total"]}
            for r in await _all(
                pool,
                "SELECT state, count(*) AS n, COALESCE(sum(total), 0) AS total "
                "FROM invoices GROUP BY state",
            )
        }
        decisions = await _all(
            pool,
            "SELECT decider_kind, decision, count(*) AS n FROM decisions "
            "GROUP BY 1, 2 ORDER BY 1, 2",
        )
        llm = (
            await _all(
                pool,
                """SELECT count(*) AS calls, count(*) FILTER (WHERE cached) AS cached,
                          count(*) FILTER (WHERE NOT ok) AS failed,
                          COALESCE(sum(prompt_tokens), 0) AS prompt_tokens,
                          COALESCE(sum(output_tokens), 0) AS output_tokens,
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
                            FILTER (WHERE NOT cached AND ok) AS median_ms
                   FROM llm_calls""",
            )
        )[0]
        tools = (
            await _all(
                pool,
                "SELECT count(*) AS calls, count(*) FILTER (WHERE NOT allowed) AS denied "
                "FROM tool_calls",
            )
        )[0]
        flags = await _all(
            pool,
            "SELECT f AS flag, count(*) AS n FROM invoices, unnest(flags) AS f "
            "GROUP BY f ORDER BY n DESC, f",
        )
        async with pool.connection() as conn:
            policy = await repo.get_policy(conn)
        finished = sum(
            by_state.get(s, {}).get("n", 0) for s in ("PAYMENT_DRAFTED", "REJECTED")
        )
        auto = next((d["n"] for d in decisions if d["decision"] == "auto_approve"), 0)
        return _json(
            {
                "by_state": by_state,
                "waiting": sum(
                    by_state.get(s, {}).get("n", 0)
                    for s in ("AWAITING_APPROVAL", "ON_HOLD")
                ),
                "drafted_total": by_state.get("PAYMENT_DRAFTED", {}).get(
                    "total", Decimal(0)
                ),
                "finished": finished,
                "auto_approved": auto,
                "decisions": decisions,
                "flags": flags,
                "llm": {**llm, "cost_usd": 0},
                "tools": tools,
                "policy_version": policy.version,
                "eval": _latest_eval(),
            }
        )

    @app.get("/api/invoices")
    async def invoices(state: str | None = None) -> list[dict[str, Any]]:
        if state and state not in STATES:
            raise HTTPException(400, f"unknown state {state}")
        rows = await _all(
            pool,
            """SELECT i.id, i.invoice_no, i.invoice_date, i.total, i.state, i.flags, i.route,
                      i.approvers, i.source_file, i.received_at, i.updated_at,
                      v.code AS vendor_code, v.name AS vendor_name,
                      (SELECT d.rule_version FROM decisions d WHERE d.invoice_id = i.id
                       ORDER BY d.id DESC LIMIT 1) AS rule_version
               FROM invoices i LEFT JOIN vendors v ON v.id = i.vendor_id
               WHERE %s::text IS NULL OR i.state = %s
               ORDER BY i.id DESC LIMIT 500""",
            state,
            state,
        )
        return _json(rows)

    @app.get("/api/invoices/{invoice_id}")
    async def invoice(invoice_id: int) -> dict[str, Any]:
        async with pool.connection() as conn:
            try:
                inv = await repo.get_invoice(conn, invoice_id)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            q = conn.execute
            inv["route"] = await (
                await q(
                    "SELECT route, approval_steps, approvers, callback_instruction, "
                    "source_file, extracted FROM invoices WHERE id = %s",
                    (invoice_id,),
                )
            ).fetchone()
            inv["notes"] = await (
                await q(
                    "SELECT agent, kind, content, provider, model, created_at "
                    "FROM agent_notes WHERE invoice_id = %s ORDER BY id",
                    (invoice_id,),
                )
            ).fetchall()
            inv["tool_calls"] = await (
                await q(
                    "SELECT agent, tool, allowed, error, duration_ms, created_at "
                    "FROM tool_calls WHERE invoice_id = %s ORDER BY id",
                    (invoice_id,),
                )
            ).fetchall()
            inv["llm_calls"] = await (
                await q(
                    "SELECT agent, provider, model, cached, ok, attempts, duration_ms, "
                    "prompt_tokens, output_tokens, error, created_at "
                    "FROM llm_calls WHERE invoice_id = %s ORDER BY id",
                    (invoice_id,),
                )
            ).fetchall()
            inv["payment"] = await (
                await q(
                    """SELECT b.batch_ref, b.status, b.csv_path, l.amount,
                              l.beneficiary_name, l.beneficiary_account, l.ifsc
                       FROM payment_lines l JOIN payment_batches b ON b.id = l.batch_id
                       WHERE l.invoice_id = %s""",
                    (invoice_id,),
                )
            ).fetchone()
            inv["decisions"] = await (
                await q(
                    "SELECT decided_by, decider_kind, decision, approval_step, reason, "
                    "rule_version, created_at FROM decisions WHERE invoice_id = %s "
                    "ORDER BY id",
                    (invoice_id,),
                )
            ).fetchall()
        return _json(inv)

    @app.get("/api/audit")
    async def audit(limit: int = 200) -> list[dict[str, Any]]:
        return _json(
            await _all(
                pool,
                """SELECT a.id, a.invoice_id, i.invoice_no, a.actor, a.actor_kind, a.action,
                          a.from_state, a.to_state, a.reason, a.created_at
                   FROM audit_log a LEFT JOIN invoices i ON i.id = a.invoice_id
                   ORDER BY a.id DESC LIMIT %s""",
                min(max(limit, 1), 1000),
            )
        )

    @app.get("/api/rules")
    async def rules() -> dict[str, Any]:
        versions = await _all(
            pool,
            "SELECT version, active, policy, change_summary, proposed_by, approved_by, "
            "created_at FROM rules ORDER BY version DESC",
        )
        async with pool.connection() as conn:
            ids = [
                r["id"]
                for r in await (
                    await conn.execute("SELECT id FROM rule_proposals ORDER BY id DESC")
                ).fetchall()
            ]
            proposals = [await steward.describe(conn, i) for i in ids]
        return _json({"versions": versions, "proposals": proposals})

    return app


def _latest_eval() -> dict[str, Any] | None:
    """Headline numbers from the newest full eval run, if there is one."""
    runs = sorted((ROOT / "evals" / "runs").glob("*.json"))
    for path in reversed(runs):
        try:
            run = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        m = run.get("metrics") or {}
        if run.get("mode") != "live" or run.get("ids"):
            continue  # only full live runs count
        return {
            "run": path.stem,
            "n": m.get("n"),
            "fraud_recall": (m.get("fraud_recall") or {}).get("pct"),
            "duplicate_recall": (m.get("duplicate_recall") or {}).get("pct"),
            "injection_resistance": (m.get("injection_resistance") or {}).get("pct"),
            "decision_accuracy": m.get("decision_accuracy"),
        }
    return None
