"""Optional Langfuse traces: one trace per invoice run, one generation per LLM call.

Off unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set (plus
LANGFUSE_HOST for a self-hosted Langfuse). Postgres stays the record either way
(`llm_calls`, `tool_calls`, `audit_log`); Langfuse is a viewer for engineers.

Invoices are financial data, so prompts and model outputs are only sent when
LANGFUSE_CAPTURE_CONTENT=1. By default a trace carries timings, models,
providers, cache hits, attempts and the invoice id, and no document text.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)

_client: Any = None
_configured = False


def configure(client: Any) -> None:
    """Use this Langfuse client (tests pass one with an in-memory exporter)."""
    global _client, _configured
    _client, _configured = client, True


def client() -> Any:
    """The Langfuse client, or None when tracing is off."""
    global _client, _configured
    if not _configured:
        _configured = True
        if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get(
            "LANGFUSE_SECRET_KEY"
        ):
            try:
                from langfuse import Langfuse

                _client = Langfuse(environment=os.environ.get("KHATA_ENV", "dev"))
                log.info("Langfuse tracing on (%s)", os.environ.get("LANGFUSE_HOST"))
            except Exception as e:  # tracing must never stop invoices
                log.warning("Langfuse tracing disabled: %s", e)
                _client = None
    return _client


def capture_content() -> bool:
    return os.environ.get("LANGFUSE_CAPTURE_CONTENT", "") in ("1", "true", "yes")


class _Off:
    def update(self, **_: Any) -> None:
        pass


@contextmanager
def generation(name: str, **fields: Any) -> Iterator[Any]:
    """A Langfuse generation around one LLM call, or a no-op when tracing is off."""
    lf = client()
    if lf is None:
        yield _Off()
        return
    if not capture_content():
        fields.pop("input", None)
    with lf.start_as_current_observation(
        as_type="generation", name=name, **fields
    ) as g:
        yield g


def run_config() -> dict[str, Any]:
    """Extra LangGraph config for a run: Langfuse's callback handler (node spans)."""
    if client() is None:
        return {}
    from langfuse.langchain import CallbackHandler

    return {"callbacks": [CallbackHandler()]}


@contextmanager
def invoice_run(graph_thread: str) -> Iterator[None]:
    """The root span for one step of an invoice's run. LangGraph runs nodes in their
    own tasks; they inherit this span through contextvars, so the node spans and
    every LLM generation land in one trace, grouped by the run's thread (session)."""
    lf = client()
    if lf is None:
        yield
        return
    from langfuse import propagate_attributes

    with (
        lf.start_as_current_observation(as_type="chain", name="invoice-run"),
        propagate_attributes(
            session_id=graph_thread, tags=["khata", "invoice"], trace_name="invoice"
        ),
    ):
        yield


def flush() -> None:
    lf = client()
    if lf is not None:
        lf.flush()
