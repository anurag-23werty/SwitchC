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
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from khata.avatars import avatar_png
from khata.config import ROOT, Settings
from khata.db import make_pool
from khata.mattermost import MattermostAdmin
from khata.repo import get_policy
from khata.switch_admin import GatewayAdmin, room_members_rule
from khata.switch_client import (
    AgentIdentity,
    KeyStore,
    SwitchAgent,
    SwitchError,
    register_agent,
)
from khata.switch_docs import policy_text

log = logging.getLogger("khata.setup")

TEMPLATE = ROOT / "switch" / "khata.template.yaml"
INBOX_TEMPLATE = ROOT / "switch" / "khata-inbox.template.yaml"
STATE = Path(os.environ.get("KHATA_SWITCH_STATE", ROOT / ".khata-switch.json"))
STACK_ENV = Path(os.environ.get("KHATA_STACK_ENV", ROOT / "switch" / "stack" / ".env"))
MATTERMOST_URL = os.environ.get("KHATA_MATTERMOST_URL", "http://localhost:8065")
DESK, AUDIT, INBOX = "ap-desk", "ap-audit", "ap-inbox"

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
    "khata-steward": (
        "Khata Policy Steward",
        "Spots exceptions people keep approving and proposes a rule; the owner decides.",
    ),
    "khata-liaison": (
        "Khata Vendor Liaison",
        "Answers vendors in the vendor inbox about their own invoices. Never changes bank details.",
    ),
}
# Which agents belong in which room. The template creates rooms with these;
# setup also invites any that are missing, so a new agent joins existing rooms.
ROOM_AGENTS = {
    "ap-desk": list(AGENTS),
    "ap-audit": ["khata-router", "khata-auditor", "khata-steward"],
}  # #ap-inbox holds only the Liaison, from its own template


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


def demo_password() -> str | None:
    """The demo team's shared Mattermost password (KHATA_DEMO_PASSWORD), if this is
    the demo. A real team gets a separate one-time password per new account."""
    return os.environ.get("KHATA_DEMO_PASSWORD") or dotenv_values(ROOT / ".env").get(
        "KHATA_DEMO_PASSWORD"
    )


def ensure_demo_password() -> str:
    """Demo only: create KHATA_DEMO_PASSWORD in .env so the scripted demos can sign in."""
    current = demo_password()
    if current:
        return current
    pw = "Khata-" + secrets.token_urlsafe(9)
    with (ROOT / ".env").open("a") as f:
        f.write(
            f"\n# Shared Mattermost password for the DEMO team only\nKHATA_DEMO_PASSWORD={pw}\n"
        )
    os.environ["KHATA_DEMO_PASSWORD"] = pw
    return pw


@dataclass
class Team:
    """Who belongs in the rooms, read from Khata's database (not from the demo data)."""

    people: list[dict[str, Any]]  # username, display_name, role, email
    contacts: list[dict[str, Any]]  # vendor-side: username, display_name
    approvers: dict[str, str]
    policy: str


async def load_team(url: str) -> Team:
    pool = make_pool(url, max_size=1)
    await pool.open()
    try:
        async with pool.connection() as conn:
            people = await (
                await conn.execute(
                    "SELECT username, display_name, role, email FROM people "
                    "WHERE active ORDER BY username"
                )
            ).fetchall()
            contacts = await (
                await conn.execute(
                    "SELECT username, display_name FROM vendor_contacts ORDER BY username"
                )
            ).fetchall()
            pol = await get_policy(conn)
    finally:
        await pool.close()
    if not people:
        raise RuntimeError(
            "no people in Khata yet: run `khata init khata.yaml --as <owner>`"
        )
    return Team(people, contacts, pol.approvers, policy_text(pol))


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


async def ensure_people(
    mm: MattermostAdmin, team_name: str, team: Team
) -> dict[str, str]:
    """Make sure everyone has a Mattermost account in the team. Returns the accounts
    created, with the password to hand over: the demo password, or a fresh random one
    per person (shown once; they should change it at first sign-in)."""
    shared = demo_password()
    created: dict[str, str] = {}
    for p in [*team.people, *team.contacts]:
        first, _, last = p["display_name"].partition(" ")
        pw = shared or "Kh-" + secrets.token_urlsafe(12)
        user, new = await mm.ensure_user(
            p["username"],
            email=p.get("email") or f"{p['username']}@khata.local",
            password=pw,
            first=first,
            last=last,
        )
        await mm.add_to_team(team_name, user["id"])
        if new:
            created[p["username"]] = "(KHATA_DEMO_PASSWORD)" if shared else pw
    return created


async def find_rooms(agent: SwitchAgent) -> dict[str, str]:
    rooms = await agent.op("list_rooms")
    return {r["name"]: r["room_id"] for r in rooms if r["name"] in (DESK, AUDIT, INBOX)}


def _room_ids(result: dict[str, Any]) -> dict[str, str]:
    return {r["room_name"]: r["room_id"] for r in result.get("rooms", [])}


async def setup(s: Settings) -> dict[str, Any]:
    stack = dotenv_values(STACK_ENV)
    report: dict[str, Any] = {}

    team = await load_team(s.khata_database_url)
    agents = await register_all(s)
    report["agents"] = sorted(agents)

    async with MattermostAdmin(MATTERMOST_URL) as mm:
        await mm.login(
            stack["MATTERMOST_ADMIN_USER"], stack["MATTERMOST_ADMIN_PASSWORD"]
        )
        report["accounts_created"] = await ensure_people(
            mm, stack["MATTERMOST_TEAM_NAME"], team
        )

        router = SwitchAgent(agents["khata-router"], s.switch_api_url)
        liaison = SwitchAgent(agents["khata-liaison"], s.switch_api_url)
        try:
            # The inbox has only the Liaison in it, so ask both who sees what.
            rooms = await find_rooms(liaison) | await find_rooms(router)
            if {DESK, AUDIT} - set(rooms):
                async with GatewayAdmin(s.switch_gateway_url) as gw:
                    await gw.login(s.switch_gateway_email, s.switch_gateway_password)
                    result = await gw.rooms_from_yaml(
                        TEMPLATE.read_text(),
                        {
                            "policy": team.policy,
                            "finance_manager": team.approvers["finance_manager"],
                            "owner": team.approvers["owner"],
                        },
                        "Khata AP",
                    )
                errors = result.get("errors") or []
                if errors:
                    raise RuntimeError(f"template errors: {errors}")
                rooms = _room_ids(result) or await find_rooms(router)
                report["rooms_created"] = True
            if INBOX not in rooms:
                async with GatewayAdmin(s.switch_gateway_url) as gw:
                    await gw.login(s.switch_gateway_email, s.switch_gateway_password)
                    result = await gw.rooms_from_yaml(
                        INBOX_TEMPLATE.read_text(), {}, "Khata vendor inbox"
                    )
                if result.get("errors"):
                    raise RuntimeError(f"inbox template errors: {result['errors']}")
                rooms |= await find_rooms(liaison)
                report["inbox_created"] = True
            missing = {DESK, AUDIT, INBOX} - set(rooms)
            if missing:
                raise RuntimeError(f"rooms not found after provisioning: {missing}")
        finally:
            await router.aclose()
            await liaison.aclose()

        async with GatewayAdmin(s.switch_gateway_url) as gw:
            await gw.login(s.switch_gateway_email, s.switch_gateway_password)
            for ident in agents.values():
                await gw.set_addressing_policy(
                    ident.id,
                    [room_members_rule([rooms[DESK], rooms[AUDIT], rooms[INBOX]])],
                )
        report["addressing"] = "anyone in #ap-desk, #ap-audit and #ap-inbox"

        router = SwitchAgent(agents["khata-router"], s.switch_api_url)
        liaison_agent = SwitchAgent(agents["khata-liaison"], s.switch_api_url)
        try:
            invited = []
            for room, names in ROOM_AGENTS.items():
                for name in names:
                    if name == "khata-router":
                        continue
                    try:
                        await router.op(
                            "invite_agent_to_room", room_id=rooms[room], agent_name=name
                        )
                        invited.append(f"{name}->{room}")
                    except SwitchError as e:
                        log.warning("invite %s to %s: %s", name, room, e)
            report["room_members"] = invited
            # People join the rooms from the database: everyone in #ap-desk, the
            # approvers in #ap-audit, vendor contacts in #ap-inbox.
            approvers = [p["username"] for p in team.people if p["role"] != "clerk"]
            joins = {
                DESK: (router, [p["username"] for p in team.people]),
                AUDIT: (router, approvers),
                INBOX: (liaison_agent, [c["username"] for c in team.contacts]),
            }
            unresolved: list[str] = []
            for room, (agent, names) in joins.items():
                if not names:
                    continue
                try:
                    res = await agent.op(
                        "add_users_to_room", room_id=rooms[room], user_names=names
                    )
                    unresolved += [f"{u} ({room})" for u in res.get("unresolved") or []]
                except SwitchError as e:
                    log.warning("add people to %s: %s", room, e)
                    unresolved += [f"{u} ({room})" for u in names]
            report["people_not_added"] = unresolved or "none"
        finally:
            await router.aclose()
            await liaison_agent.aclose()

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
