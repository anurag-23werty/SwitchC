"""Phase 0 smoke test: one custom agent answering in Mattermost through Switch.

Registers `khata-hello` over the Agent Protocol, creates (or reuses) a bridged
room `khata-hello` with the seeded Mattermost user in it, announces itself, and
then replies in-thread to every message that addresses it.

    uv run python scripts/hello_agent.py
"""

from __future__ import annotations

import asyncio
import logging

from khata.config import get_settings
from khata.switch_admin import GatewayAdmin, room_members_rule
from khata.switch_client import KeyStore, RoomConnection, SwitchAgent, register_agent

AGENT = "khata-hello"
ROOM = "khata-hello"
MATTERMOST_USER = "user"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("hello")


async def find_or_create_room(agent: SwitchAgent) -> str:
    for room in await agent.op("list_rooms"):
        if room["name"] == ROOM:
            return room["room_id"]
    created = await agent.op(
        "create_room",
        name=ROOM,
        description="Khata smoke test: one agent, one person",
        agent_names=[AGENT],
        user_names=[MATTERMOST_USER],
        channel_type="channel_public",
        instructions="Smoke-test room for the Khata hello agent.",
    )
    log.info("created room %s", created["id"])
    return created["id"]


async def main() -> None:
    settings = get_settings()
    identity = await register_agent(
        api_url=settings.switch_api_url,
        registration_token=settings.switch_registration_token,
        keystore=KeyStore(settings.agent_keys_path),
        name=AGENT,
        description="Khata smoke-test agent: echoes what you ask it",
        display_name="Khata Hello",
        icon_url=None,
    )
    agent = SwitchAgent(identity, settings.switch_api_url)
    room_id = await find_or_create_room(agent)
    log.info("room %s ready", room_id)

    # New agents answer only their owner. Open this one to everyone in its room.
    async with GatewayAdmin(settings.switch_gateway_url) as admin:
        await admin.login(
            settings.switch_gateway_email, settings.switch_gateway_password
        )
        await admin.set_addressing_policy(identity.id, [room_members_rule([room_id])])
    log.info("addressing policy: anyone in %s", ROOM)

    conn = RoomConnection(agent, room_id, event_filter="addressed")
    announced = False
    async for event in conn.events():
        log.info("frame %s seq=%s", event.kind, event.sequence)
        if event.kind == "connection_state" and not announced:
            await conn.post(
                f"Khata hello agent online. Address me with @{AGENT}.", thread_id=None
            )
            announced = True
        elif event.is_addressed_message:
            p = event.payload
            thread = p.get("thread_id") or p.get("message_id")
            reply = (
                f"Hi {p.get('sender_name')}, Khata here via the Switch Agent Protocol. "
                f'You said: "{p.get("body", "").strip()}"'
            )
            for att in p.get("attachments") or []:
                data = await agent.download_media(room_id, att["mxc"])
                log.info("attachment %s", att)
                reply += (
                    f"\nGot `{att.get('filename')}` ({att.get('mimetype')}): "
                    f"downloaded {len(data)} bytes, starts with {data[:5]!r}"
                )
            await conn.post(reply, thread_id=thread)


if __name__ == "__main__":
    asyncio.run(main())
