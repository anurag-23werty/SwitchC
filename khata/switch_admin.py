"""Operator-side Switch calls, made through the Gateway as the admin.

Addressing policies are not agent-facing (an agent cannot widen its own), so
Khata's setup signs in to the Gateway once and sets them there.
"""

from __future__ import annotations

from typing import Any

import httpx

from khata.switch_client import SwitchError


class GatewayAdmin:
    def __init__(self, gateway_url: str) -> None:
        self.http = httpx.AsyncClient(
            base_url=gateway_url.rstrip("/") + "/gateway", timeout=30
        )

    async def __aenter__(self) -> GatewayAdmin:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.http.aclose()

    async def login(self, email: str, password: str) -> None:
        if not password:
            raise RuntimeError("SWITCH_GATEWAY_PASSWORD is empty; run `make env`")
        r = await self.http.post(
            "/auth/login", json={"email": email, "password": password}
        )
        if r.status_code != 200:
            raise SwitchError("gateway login", r)

    async def set_addressing_policy(
        self, agent_id: str, rules: list[dict[str, Any]]
    ) -> None:
        r = await self.http.put(
            f"/agents/{agent_id}/addressing-policy", json={"policy": {"rules": rules}}
        )
        if r.status_code != 200:
            raise SwitchError(f"addressing policy for {agent_id}", r)

    async def rooms_from_yaml(
        self, text: str, inputs: dict[str, Any], template_name: str
    ) -> dict[str, Any]:
        """Provision a room (or a room group) from a Switch template, as the admin."""
        r = await self.http.post(
            "/rooms/from-yaml",
            json={"yaml": text, "inputs": inputs, "template_name": template_name},
        )
        if r.status_code not in (200, 201):
            raise SwitchError("rooms from yaml", r)
        return r.json()


def room_members_rule(room_ids: list[str]) -> dict[str, Any]:
    """Anyone in these rooms, person or agent, may address the agent.

    ``users: "*"`` also admits people whose messaging account is not linked to
    a Switch user, which is what a finance team on Mattermost looks like. The
    scope is the rooms, so the agent still ignores every other room.
    """
    return {"rooms": room_ids, "room_groups": "*", "users": "*", "agents": "*"}
