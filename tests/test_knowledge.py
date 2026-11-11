"""Vendor knowledge in pgvector: decisions are mirrored once, embedded locally, and
searched by cosine similarity; if the model can't run, the fallback says so."""

from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio

from khata.embeddings import FastEmbedder, HashEmbedder
from khata.tools import Toolbox

from .conftest import ROOT

on_loop = pytest.mark.asyncio(loop_scope="module")
FREIGHT_REASON = "Freight charges were agreed on a call; OK to pay them"


class Broken:
    name = "broken"

    async def embed(self, texts):
        raise RuntimeError("model not available")


class OtherModel(HashEmbedder):
    name = "hash-v2"


def box(pool, embedder, tmp):
    return Toolbox(
        pool,
        files_root=ROOT / "data",
        payments_dir=tmp,
        today=date(2026, 11, 6),
        embedder=embedder,
    )


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def mst(pool):
    """Give one of Mumbai Steel's past approvals a freight reason."""
    async with pool.connection() as conn:
        await conn.execute(
            """UPDATE decisions SET reason = %s WHERE id = (
                 SELECT d.id FROM decisions d JOIN invoices i ON i.id = d.invoice_id
                 WHERE i.invoice_no = 'MST/INV/7588')""",
            (FREIGHT_REASON,),
        )
        await conn.commit()
    return "MST"


async def _rows(pool, sql, *args):
    async with pool.connection() as conn:
        return await (await conn.execute(sql, args)).fetchall()


@on_loop
async def test_decisions_are_mirrored_once_and_every_row_embedded(pool, mst, tmp_path):
    b = box(pool, HashEmbedder(), tmp_path)
    for _ in range(2):
        res = await b.search_vendor_knowledge(
            agent="analyst", vendor_code=mst, query="price variance"
        )
    assert res["retrieval"] == "pgvector cosine over hash"
    human = await _rows(
        pool,
        """SELECT count(*) AS n FROM decisions d JOIN invoices i ON i.id = d.invoice_id
           JOIN vendors v ON v.id = i.vendor_id
           WHERE v.code = 'MST' AND d.decider_kind = 'human'""",
    )
    mirrored = await _rows(
        pool,
        """SELECT count(*) AS n, count(embedding) AS embedded,
                  bool_and(embedding_model = 'hash') AS same_model
           FROM vendor_knowledge vk JOIN vendors v ON v.id = vk.vendor_id
           WHERE v.code = 'MST' AND vk.kind = 'decision'""",
    )
    assert mirrored[0]["n"] == human[0]["n"] > 0
    assert mirrored[0]["embedded"] == mirrored[0]["n"] and mirrored[0]["same_model"]


@on_loop
async def test_nearest_knowledge_comes_first_with_its_invoice(pool, mst, tmp_path):
    res = await box(pool, HashEmbedder(), tmp_path).search_vendor_knowledge(
        agent="analyst", vendor_code=mst, query="freight charges agreed"
    )
    top = res["items"][0]
    assert top["kind"] == "decision" and FREIGHT_REASON in top["text"]
    assert top["invoice_no"] == "MST/INV/7588" and top["score"] > 0
    scores = [i["score"] for i in res["items"]]
    assert scores == sorted(scores, reverse=True)


@on_loop
async def test_a_new_embedding_model_re_embeds_everything(pool, mst, tmp_path):
    await box(pool, OtherModel(), tmp_path).search_vendor_knowledge(
        agent="analyst", vendor_code=mst, query="anything"
    )
    models = await _rows(
        pool,
        """SELECT DISTINCT embedding_model FROM vendor_knowledge vk
           JOIN vendors v ON v.id = vk.vendor_id WHERE v.code = 'MST'""",
    )
    assert [m["embedding_model"] for m in models] == ["hash-v2"]


@on_loop
async def test_without_the_model_it_falls_back_to_keywords_and_says_so(
    pool, mst, tmp_path
):
    res = await box(pool, Broken(), tmp_path).search_vendor_knowledge(
        agent="analyst", vendor_code=mst, query="freight charges agreed"
    )
    assert res["retrieval"].startswith("keyword overlap (embeddings unavailable")
    assert FREIGHT_REASON in res["items"][0]["text"]


@on_loop
async def test_real_model_matches_meaning_not_just_words(pool, mst, tmp_path):
    """bge-small (local) links 'transport costs' to a freight approval."""
    emb = FastEmbedder()
    try:
        await emb.embed(["warm up"])
    except Exception as e:  # no model cached and no network
        pytest.skip(f"fastembed model unavailable: {e}")
    res = await box(pool, emb, tmp_path).search_vendor_knowledge(
        agent="analyst",
        vendor_code=mst,
        query="the vendor added transport costs to the bill",
    )
    assert res["retrieval"].startswith("pgvector cosine over fastembed")
    assert FREIGHT_REASON in res["items"][0]["text"]
