"""Least privilege for agents: who may call which tool. Enforced in code, deny by default.

An agent's prompt can be manipulated (an invoice can carry text written to do
exactly that). Its permissions can't be: every tool call goes through
`ToolGateway.call`, which refuses anything not on the agent's allowlist and
records every attempt, allowed or not, in `tool_calls`.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

AGENT_TOOLS: dict[str, frozenset[str]] = {
    "dispatcher": frozenset({"classify_input"}),
    "intake": frozenset({"extract_invoice", "validate_gst", "detect_injection"}),
    "verifier": frozenset({"three_way_match", "check_duplicate", "check_bank_change"}),
    "analyst": frozenset({"search_vendor_knowledge", "get_invoice"}),
    "auditor": frozenset({"get_invoice", "search_vendor_knowledge"}),
    "router": frozenset({"route_approval", "record_decision"}),
    "payer": frozenset({"draft_payment"}),
    "vendor_liaison": frozenset({"get_invoice_status"}),
    "policy_steward": frozenset({"propose_rule_change"}),
}

ToolFn = Callable[..., Awaitable[Any]]


class PermissionDenied(PermissionError):
    def __init__(self, agent: str, tool: str) -> None:
        super().__init__(f"agent {agent!r} is not allowed to call {tool!r}")
        self.agent, self.tool = agent, tool


def allowed_tools(agent: str) -> frozenset[str]:
    return AGENT_TOOLS.get(agent, frozenset())


def is_allowed(agent: str, tool: str) -> bool:
    return tool in allowed_tools(agent)


def _summary(value: Any) -> Any:
    text = json.dumps(value, default=str)
    return json.loads(text) if len(text) <= 4000 else {"truncated": text[:4000]}


class ToolGateway:
    """The only way agents reach tools. Checks permission, runs, logs."""

    def __init__(self, pool: AsyncConnectionPool, tools: dict[str, ToolFn]) -> None:
        self.pool = pool
        self.tools = tools

    async def _log(
        self,
        agent: str,
        tool: str,
        allowed: bool,
        args: dict,
        invoice_id: int | None,
        result: Any = None,
        error: str | None = None,
        ms: int | None = None,
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO tool_calls (invoice_id, agent, tool, allowed, args, result, error,
                                           duration_ms)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    invoice_id,
                    agent,
                    tool,
                    allowed,
                    json.dumps(args, default=str),
                    json.dumps(_summary(result), default=str)
                    if result is not None
                    else None,
                    error,
                    ms,
                ),
            )

    async def call(self, agent: str, tool: str, **args: Any) -> Any:
        invoice_id = args.get("invoice_id")
        if not is_allowed(agent, tool) or tool not in self.tools:
            await self._log(
                agent, tool, False, args, invoice_id, error="permission denied"
            )
            logger.warning("DENIED %s -> %s %s", agent, tool, args)
            raise PermissionDenied(agent, tool)
        start = time.perf_counter()
        try:
            result = await self.tools[tool](agent=agent, **args)
        except Exception as e:
            ms = int((time.perf_counter() - start) * 1000)
            await self._log(
                agent,
                tool,
                True,
                args,
                invoice_id,
                error=f"{type(e).__name__}: {e}",
                ms=ms,
            )
            raise
        ms = int((time.perf_counter() - start) * 1000)
        await self._log(agent, tool, True, args, invoice_id, result=result, ms=ms)
        logger.info("%s -> %s (invoice %s) %d ms", agent, tool, invoice_id, ms)
        return result
