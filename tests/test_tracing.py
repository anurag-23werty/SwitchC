"""Langfuse tracing: off by default; when on, one trace per invoice run with each LLM
call as a generation, and no invoice text unless content capture is switched on.

Uses the Langfuse SDK with an in-memory OpenTelemetry exporter: no server.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from khata import tracing
from khata.runtime import Khata

from .conftest import GOLDEN, ROOT
from .llm_fakes import FakeProviders

BY_ID = {g["id"]: g for g in GOLDEN}


def test_tracing_is_off_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setattr(tracing, "_configured", False)
    monkeypatch.setattr(tracing, "_client", None)
    assert tracing.client() is None and tracing.run_config() == {}
    with tracing.invoice_run("t"):
        pass
    with tracing.generation("x", input={"user": "secret"}) as g:
        g.update(output="y")  # a no-op


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    lf = Langfuse(
        public_key=f"pk-lf-{uuid.uuid4().hex}",  # the SDK keeps one client per key
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:9",
        span_exporter=exporter,
        flush_at=1,
    )
    monkeypatch.setattr(tracing, "_configured", True)
    monkeypatch.setattr(tracing, "_client", lf)
    yield exporter, lf
    lf.shutdown()


async def _run(gid, test_db_url, tmp_path):
    async with Khata.open(
        database_url=test_db_url,
        llm=FakeProviders(GOLDEN, ROOT).llm(),
        guard=False,
        payments_dir=tmp_path,
        today=date(2026, 11, 6),
    ) as k:
        return await k.submit(BY_ID[gid]["file"])


@pytest.mark.asyncio
async def test_an_invoice_run_is_one_trace_with_a_generation_per_llm_call(
    spans, test_db_url, tmp_path, monkeypatch
):
    monkeypatch.delenv("LANGFUSE_CAPTURE_CONTENT", raising=False)
    exporter, lf = spans
    out = await _run("D02", test_db_url, tmp_path)
    lf.flush()
    got = exporter.get_finished_spans()
    gens = [
        s for s in got if s.attributes.get("langfuse.observation.type") == "generation"
    ]
    names = {s.name for s in gens}
    assert {"intake-a", "intake-b", "analyst", "auditor"} <= names
    assert len({s.context.trace_id for s in got}) == 1  # the whole run, one trace
    root = next(s for s in got if s.name == "invoice-run")
    assert root.attributes.get("session.id") or any(
        "session" in k for k in root.attributes
    )
    assert any(s.name == "route" for s in got)  # LangGraph nodes are spans too
    a = next(s for s in gens if s.name == "intake-a")
    attrs = " ".join(str(v) for v in a.attributes.values())
    assert "fake-groq-a" in attrs and str(out.invoice_id) in attrs
    assert "Deccan" not in attrs  # no document text unless capture is on


@pytest.mark.asyncio
async def test_content_is_sent_only_when_asked(
    spans, test_db_url, tmp_path, monkeypatch
):
    monkeypatch.setenv("LANGFUSE_CAPTURE_CONTENT", "1")
    exporter, lf = spans
    await _run("D01", test_db_url, tmp_path)  # a new file: the same one is not re-read
    lf.flush()
    a = next(s for s in exporter.get_finished_spans() if s.name == "intake-a")
    assert "Sharma Office Supplies" in " ".join(str(v) for v in a.attributes.values())
