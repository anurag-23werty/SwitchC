"""A client for the Switch Agent Protocol.

Implements the wire contract in Switch's docs (internals/agent-protocol.md),
verified against switch-core 0.31.0:

* Registration: ``POST /agents`` with the registration token as bearer. A custom
  agent registers with its own ``connector_type`` and an ``always_on``
  integration profile; the API key comes back once, so it is cached locally.
* Down: one SSE stream per connection, ``GET /agents/{id}/events``. Rooms are
  declared on the open URL, never after, so catch-up covers them.
* Up: ``POST /agents/{id}/ops/{operation}`` with the arguments unwrapped, bound
  to a room by the ``X-Switch-Connection-Id`` header.
* Liveness: ``POST /agents/{id}/connection/beat`` every heartbeat interval (2 s,
  TTL 6 s). A 404 or 409 on a beat means reopen the stream, not back off.
* The connection id is generated once and reused on reconnect, with
  ``Last-Event-ID``, so a dropped socket resumes rather than loses events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

CLIENT_NAME = "khata"
CLIENT_VERSION = "0.1.0"
CONNECTOR_TYPE = "khata"

# What a Khata agent can do: an always-connected service that exchanges
# messages. It is not a CLI session, so it supports no reset/compact commands.
ALWAYS_ON_PROFILE: dict[str, Any] = {
    "connection_model": "always_on",
    "message_exchange": True,
    "pre_invocation_mediation": [],
    "post_invocation_mediation": [],
    "event_reporting": [],
}


class SwitchError(RuntimeError):
    """Switch refused or failed a call. Carries the status and the body."""

    def __init__(self, what: str, response: httpx.Response) -> None:
        super().__init__(f"{what}: HTTP {response.status_code} {response.text[:500]}")
        self.status_code = response.status_code


class AgentIdentity(BaseModel):
    name: str
    id: str
    api_key: str


class SwitchEvent(BaseModel):
    """One frame from the event stream: a domain event or a control frame."""

    kind: str
    sequence: int | None = None
    room_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_addressed_message(self) -> bool:
        return self.kind == "message" and bool(self.payload.get("addressed"))


class KeyStore:
    """Agent API keys, cached on disk because Switch returns each one once."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, AgentIdentity]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text())
        return {name: AgentIdentity(**ident) for name, ident in data.items()}

    def save(self, identity: AgentIdentity) -> None:
        data = {n: i.model_dump() for n, i in self.load().items()}
        data[identity.name] = identity.model_dump()
        self.path.write_text(json.dumps(data, indent=2))
        self.path.chmod(0o600)


async def register_agent(
    *,
    api_url: str,
    registration_token: str,
    keystore: KeyStore,
    name: str,
    description: str,
    display_name: str,
    icon_url: str | None,
) -> AgentIdentity:
    """Register an agent, or reuse the key already cached for it.

    A name Switch already holds but we have no key for (the cache was deleted)
    is re-registered with ``overwrite``, which issues a fresh key.
    """
    cached = keystore.load().get(name)
    if cached is not None:
        async with httpx.AsyncClient(base_url=api_url, timeout=30) as http:
            r = await http.get(
                f"/agents/{cached.id}/ops",
                headers={"Authorization": f"Bearer {cached.api_key}"},
            )
        if r.status_code == 200:
            return cached
        logger.warning(
            "cached key for %s rejected (%s); re-registering", name, r.status_code
        )

    body = {
        "name": name,
        "description": description,
        "display_name": display_name,
        "icon_url": icon_url,
        "connector_type": CONNECTOR_TYPE,
        "integration_profile": ALWAYS_ON_PROFILE,
        "overwrite": False,
    }
    headers = {"Authorization": f"Bearer {registration_token}"}
    async with httpx.AsyncClient(base_url=api_url, timeout=30) as http:
        r = await http.post("/agents", json=body, headers=headers)
        if r.status_code == 409:
            body["overwrite"] = True
            r = await http.post("/agents", json=body, headers=headers)
    if r.status_code != 200:
        raise SwitchError(f"registering agent {name}", r)
    data = r.json()
    identity = AgentIdentity(name=name, id=data["id"], api_key=data["api_key"])
    keystore.save(identity)
    logger.info("registered agent %s (%s)", name, identity.id)
    return identity


class SwitchAgent:
    """HTTP-side calls for one registered agent."""

    def __init__(self, identity: AgentIdentity, api_url: str) -> None:
        self.identity = identity
        self.api_url = api_url.rstrip("/")
        self.http = httpx.AsyncClient(
            base_url=self.api_url,
            headers={"Authorization": f"Bearer {identity.api_key}"},
            timeout=httpx.Timeout(30.0),
        )

    @property
    def name(self) -> str:
        return self.identity.name

    async def aclose(self) -> None:
        await self.http.aclose()

    async def op(
        self, operation: str, connection_id: str | None = None, **args: Any
    ) -> Any:
        """Invoke an operation. Arguments go in the body unwrapped."""
        headers = {"X-Switch-Connection-Id": connection_id} if connection_id else {}
        r = await self.http.post(
            f"/agents/{self.identity.id}/ops/{operation}", json=args, headers=headers
        )
        if r.status_code != 200:
            raise SwitchError(f"{self.name} op {operation}", r)
        return r.json()["result"]

    async def send_message(self, room_id: str, content: str) -> str:
        """Post to a room without a connection (top-level, no thread)."""
        r = await self.http.post(
            f"/agents/{self.identity.id}/message",
            json={"room_id": room_id, "content": content},
        )
        if r.status_code != 200:
            raise SwitchError(f"{self.name} message", r)
        return r.json()["event_id"]

    async def download_media(self, room_id: str, mxc: str) -> bytes:
        """Fetch an attachment's bytes. An attachment in an event is a pointer."""
        r = await self.http.get(
            f"/agents/{self.identity.id}/rooms/{room_id}/media", params={"mxc": mxc}
        )
        if r.status_code != 200:
            raise SwitchError(f"{self.name} media download", r)
        return r.content

    async def upload_media(
        self,
        room_id: str,
        files: list[tuple[str, bytes, str]],
        caption: str | None,
        thread_id: str | None,
    ) -> dict[str, Any]:
        """Post files to a room as this agent. Each file is (name, bytes, mimetype)."""
        data = {
            k: v for k, v in {"caption": caption, "thread_id": thread_id}.items() if v
        }
        r = await self.http.post(
            f"/agents/{self.identity.id}/rooms/{room_id}/media",
            files=[("files", f) for f in files],
            data=data,
        )
        if r.status_code != 200:
            raise SwitchError(f"{self.name} media upload", r)
        return r.json()


class _Reopen(Exception):
    """The connection must be reopened (beat refused, or evicted)."""


class RoomConnection:
    """One agent's connection to one room: an SSE stream plus its heartbeat.

    ``single`` scope with the room declared on the open URL, so the claim and
    catch-up happen on open and every op made through it is bound to the room.
    """

    def __init__(
        self,
        agent: SwitchAgent,
        room_id: str,
        event_filter: str,
        cursor: int | None = None,
    ) -> None:
        """`cursor`: resume after this event sequence (a restart); None starts at head."""
        self.agent = agent
        self.room_id = room_id
        self.event_filter = event_filter
        self.connection_id = str(uuid.uuid4())
        self.cursor: int | None = cursor
        self.heartbeat_interval = 2.0
        self._reopen = asyncio.Event()

    async def op(self, operation: str, **args: Any) -> Any:
        return await self.agent.op(operation, connection_id=self.connection_id, **args)

    async def post(self, body: str, thread_id: str | None) -> dict[str, Any]:
        args: dict[str, Any] = {"body": body}
        if thread_id:
            args["thread_id"] = thread_id
        return await self.op("post_message", **args)

    async def address(
        self, body: str, target_names: list[str], thread_id: str | None
    ) -> dict[str, Any]:
        """Post a message that addresses named participants, so they act on it."""
        args: dict[str, Any] = {"body": body, "target_names": target_names}
        if thread_id:
            args["thread_id"] = thread_id
        return await self.op("send_targeted_message", **args)

    async def _beat_forever(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            body: dict[str, Any] = {"connection_id": self.connection_id}
            if self.cursor is not None:
                body["cursor"] = self.cursor
            try:
                r = await self.agent.http.post(
                    f"/agents/{self.agent.identity.id}/connection/beat", json=body
                )
            except httpx.HTTPError as e:
                logger.warning("%s beat failed: %s", self.agent.name, e)
                continue
            if r.status_code in (404, 409):
                logger.warning("%s beat %s; reopening", self.agent.name, r.status_code)
                self._reopen.set()
                return
            if r.status_code != 200:
                logger.error(
                    "%s beat HTTP %s: %s", self.agent.name, r.status_code, r.text[:200]
                )

    def _open_params(self) -> dict[str, str]:
        params = {
            "connection_id": self.connection_id,
            "scope": "single",
            "filter": self.event_filter,
            "rooms": self.room_id,
            "client": CLIENT_NAME,
            "client_version": CLIENT_VERSION,
        }
        if self.cursor is None:
            params["start_from"] = "head"
        return params

    async def _stream_once(self) -> AsyncIterator[SwitchEvent]:
        headers = {"Accept": "text/event-stream"}
        if self.cursor is not None:
            headers["Last-Event-ID"] = str(self.cursor)
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
        async with self.agent.http.stream(
            "GET",
            f"/agents/{self.agent.identity.id}/events",
            params=self._open_params(),
            headers=headers,
            timeout=timeout,
        ) as resp:
            if resp.status_code != 200:
                await resp.aread()
                raise SwitchError(f"{self.agent.name} open stream", resp)
            async for event in _parse_sse(resp.aiter_lines()):
                if self._reopen.is_set():
                    raise _Reopen()
                if event.kind == "connection_state":
                    self.heartbeat_interval = float(
                        event.raw.get(
                            "heartbeat_interval_seconds", self.heartbeat_interval
                        )
                    )
                    if self.cursor is None and event.raw.get("cursor") is not None:
                        self.cursor = int(event.raw["cursor"])
                elif event.kind == "evicted":
                    raise _Reopen()
                if event.sequence is not None:
                    self.cursor = event.sequence
                yield event

    async def events(self) -> AsyncIterator[SwitchEvent]:
        """Every frame, forever, reconnecting with the same connection id."""
        backoff = 1.0
        while True:
            self._reopen.clear()
            beat = asyncio.create_task(self._beat_forever())
            try:
                async for event in self._stream_once():
                    backoff = 1.0
                    yield event
            except _Reopen:
                logger.info("%s reopening stream", self.agent.name)
            except (httpx.HTTPError, SwitchError) as e:
                logger.warning(
                    "%s stream dropped: %s (retry in %.0fs)",
                    self.agent.name,
                    e,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                beat.cancel()


async def _parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[SwitchEvent]:
    """Turn SSE lines into events. Comment lines are keepalives and are skipped."""
    sse_id: str | None = None
    sse_event: str | None = None
    data: list[str] = []
    async for line in lines:
        if line == "":
            if data:
                yield _to_event(sse_id, sse_event, "\n".join(data))
            sse_id, sse_event, data = None, None, []
        elif line.startswith(":"):
            continue
        else:
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "id":
                sse_id = value
            elif field == "event":
                sse_event = value
            elif field == "data":
                data.append(value)


def _to_event(sse_id: str | None, sse_event: str | None, data: str) -> SwitchEvent:
    obj = json.loads(data)
    kind = obj.get("type") or sse_event or "unknown"
    sequence = obj.get("sequence")
    if sequence is None and sse_id is not None:
        sequence = int(sse_id)
    return SwitchEvent(
        kind=kind,
        sequence=sequence,
        room_id=obj.get("room_id"),
        payload=obj.get("payload") or {},
        raw=obj,
    )
