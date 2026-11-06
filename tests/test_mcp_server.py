"""The MCP server exposes only an agent's own tools, and calls go through the gateway."""

from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError

from khata.policy import AGENT_TOOLS, ToolGateway
from khata.tools import Toolbox
from mcp_server.server import build_server

from .conftest import ROOT

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def gateway(pool, tmp_path_factory):
    box = Toolbox(
        pool,
        files_root=ROOT / "data",
        payments_dir=tmp_path_factory.mktemp("p"),
        today=date(2026, 11, 6),
    )
    return ToolGateway(pool, box.as_dict())


@pytest.mark.parametrize("agent", ["intake", "verifier", "analyst", "router", "payer"])
async def test_server_lists_only_the_agents_tools(gateway, agent):
    async with Client(build_server(agent, gateway)) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == set(AGENT_TOOLS[agent])


async def test_tool_schema_hides_the_agent_parameter(gateway):
    async with Client(build_server("analyst", gateway)) as client:
        tools = {t.name: t for t in await client.list_tools()}
    props = tools["search_vendor_knowledge"].input_schema["properties"]
    assert "agent" not in props and "vendor_code" in props


async def test_analyst_reads_vendor_knowledge_over_mcp(gateway):
    async with Client(build_server("analyst", gateway)) as client:
        res = await client.call_tool(
            "search_vendor_knowledge", {"vendor_code": "MST", "query": "payment terms"}
        )
    assert res.data["vendor"] == "Mumbai Steel Traders"
    assert any("45 days" in item["text"] for item in res.data["items"])


async def test_calling_an_unlisted_tool_by_name_fails(gateway):
    async with Client(build_server("analyst", gateway)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("draft_payment", {"invoice_ids": [1]})
