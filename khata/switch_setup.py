"""One-time (and re-runnable) Switch setup for Khata: `uv run khata setup-switch`.

1. Register Khata's seven agents over the Agent Protocol (keys cached locally).
2. Create the finance team's Mattermost accounts (priya, owner, ravi).
3. Deploy `switch/khata.template.yaml` through the Gateway: the "Khata AP"
   group with #ap-desk and #ap-audit, members, instructions and kickoff (the
   kickoff states the policy read from Khata's `rules` table).
4. Open each agent to everyone in those two rooms, and only those.
5. Give each agent's Mattermost bot its avatar.

Every step checks what exists first, so running it again is safe.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from data import fixtures as fx
from khata.avatars import avatar_png
from khata.config import ROOT, Settings
from khata.db import make_pool
from khata.mattermost import MattermostAdmin
from khata.repo import get_policy
from khata.switch_admin import GatewayAdmin, room_members_rule
from khata.switch_client import AgentIdentity, KeyStore, SwitchAgent, register_agent
from khata.switch_docs import policy_text

log = logging.getLogger("khata.setup")

TEMPLATE = ROOT / "switch" / "khata.template.yaml"
STATE = ROOT / ".khata-switch.json"
STACK_ENV = ROOT / "switch" / "stack" / ".env"
MATTERMOST_URL = "http://localhost:8065"
DESK, AUDIT = "ap-desk", "ap-audit"

AGENTS: dict[str, tuple[str, str]] = {
    "khata-dispatcher": (
        "Khata Dispatcher",
        "Front door of #ap-desk: classifies every post and routes it.",
    ),
    "khata-intake": (
        "Khata Intake",
        "Reads invoices with two independent models; checks GST and injection.",
    ),
    "khata-verifier": (
        "Khata Verifier",
        "Three-way match against PO and GRN; duplicate and bank checks.",
    ),
    "khata-analyst": (
        "Khata Analyst",
        "Explains exceptions for the approver, citing past decisions.",
    ),
    "khata-auditor": (
        "Khata Auditor",
        "Independent second opinion on the Analyst's note, on another provider.",
    ),
    "khata-router": (
        "Khata Router",
        "Applies the approval policy, tags the approver, records decisions.",
    ),
    "khata-payer": (
        "Khata Payer",
        "Drafts NEFT bulk-upload files and journals. Never pays.",
    ),
}


@dataclass
class SwitchState:
    rooms: dict[str, str]  # room name -> Switch room id

    @classmethod
    def load(cls, path: Path = STATE) -> SwitchState:
        if not path.exists():
            raise RuntimeError(
                "Switch isn't set up for Khata yet: run `uv run khata setup-switch`"
            )
        return cls(**json.loads(path.read_text()))

    def save(self, path: Path = STATE) -> None:
        path.write_text(json.dumps({"rooms": self.rooms}, indent=2))


def people_password() -> str:
    """The demo team's Mattermost password: KHATA_DEMO_PASSWORD in .env, created once."""
    env_file = ROOT / ".env"
    current = dotenv_values(env_file).get("KHATA_DEMO_PASSWORD")
    if current:
        return current
    pw = "Khata-" + secrets.token_urlsafe(9)
    with env_file.open("a") as f:
        f.write(
            f"\n# Mattermost password for the demo team (priya, owner, ravi)\nKHATA_DEMO_PASSWORD={pw}\n"
        )
    return pw


async def register_all(s: Settings) -> dict[str, AgentIdentity]:
    keys = KeyStore(s.agent_keys_path)
    out = {}
    for name, (display, description) in AGENTS.items():
        out[name] = await register_agent(
            api_url=s.switch_api_url,
            registration_token=s.switch_registration_token,
            keystore=keys,
            name=name,
            description=description,
            display_name=display,
            icon_url=None,
        )
    return out


async def ensure_people(mm: MattermostAdmin, team: str) -> list[str]:
    pw = people_password()
    created = []
    for p in fx.PEOPLE:
        first, _, last = p["display_name"].partition(" ")
        user, new = await mm.ensure_user(
            p["username"],
            email=f"{p['username']}@khata.example",
            password=pw,
            first=first,
            last=last,
        )
        await mm.add_to_team(team, user["id"])
        if new:
            created.append(p["username"])
    return created


async def find_rooms(agent: SwitchAgent) -> dict[str, str]:
    rooms = await agent.op("list_rooms")
    return {r["name"]: r["room_id"] for r in rooms if r["name"] in (DESK, AUDIT)}


def _room_ids(result: dict[str, Any]) -> dict[str, str]:
    return {r["room_name"]: r["room_id"] for r in result.get("rooms", [])}


async def setup(s: Settings) -> dict[str, Any]:
    stack = dotenv_values(STACK_ENV)
    report: dict[str, Any] = {}

    agents = await register_all(s)
    report["agents"] = sorted(agents)

    async with MattermostAdmin(MATTERMOST_URL) as mm:
        await mm.login(
            stack["MATTERMOST_ADMIN_USER"], stack["MATTERMOST_ADMIN_PASSWORD"]
        )
        report["people_created"] = await ensure_people(
            mm, stack["MATTERMOST_TEAM_NAME"]
        )

        router = SwitchAgent(agents["khata-router"], s.switch_api_url)
        try:
            rooms = await find_rooms(router)
            if len(rooms) < 2:
                pool = make_pool(s.khata_database_url, max_size=1)
                await pool.open()
                try:
                    async with pool.connection() as conn:
                        policy = policy_text(await get_policy(conn))
                finally:
                    await pool.close()
                async with GatewayAdmin(s.switch_gateway_url) as gw:
                    await gw.login(s.switch_gateway_email, s.switch_gateway_password)
                    result = await gw.rooms_from_yaml(
                        TEMPLATE.read_text(), {"policy": policy}, "Khata AP"
                    )
                errors = result.get("errors") or []
                if errors:
                    raise RuntimeError(f"template errors: {errors}")
                rooms = _room_ids(result) or await find_rooms(router)
                report["rooms_created"] = True
            missing = {DESK, AUDIT} - set(rooms)
            if missing:
                raise RuntimeError(f"rooms not found after provisioning: {missing}")
        finally:
            await router.aclose()

        async with GatewayAdmin(s.switch_gateway_url) as gw:
            await gw.login(s.switch_gateway_email, s.switch_gateway_password)
            for ident in agents.values():
                await gw.set_addressing_policy(
                    ident.id, [room_members_rule([rooms[DESK], rooms[AUDIT]])]
                )
        report["addressing"] = "anyone in #ap-desk and #ap-audit"

        bots = {b["username"]: b["user_id"] for b in await mm.bots()}
        pictured = []
        for name in agents:
            if name in bots:
                await mm.set_picture(bots[name], avatar_png(name))
                pictured.append(name)
        report["avatars"] = pictured

    SwitchState(rooms=rooms).save()
    report["rooms"] = rooms
    return report
