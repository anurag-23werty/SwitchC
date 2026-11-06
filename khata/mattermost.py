"""Mattermost admin calls for demo setup: the finance team's accounts, and agent avatars.

Switch owns everything else on Mattermost (rooms, bots, messages). These are the
two things it leaves to the workspace: people's accounts, and bot pictures when
the icon URL is not reachable from the Switch server (a laptop demo).
"""

from __future__ import annotations

from typing import Any

import httpx


class MattermostError(RuntimeError):
    pass


class MattermostAdmin:
    def __init__(self, url: str) -> None:
        self.http = httpx.AsyncClient(base_url=url.rstrip("/") + "/api/v4", timeout=30)

    async def __aenter__(self) -> MattermostAdmin:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.http.aclose()

    def _check(self, r: httpx.Response, what: str) -> Any:
        if r.status_code >= 400:
            raise MattermostError(f"{what}: HTTP {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    async def login(self, username: str, password: str) -> None:
        r = await self.http.post(
            "/users/login", json={"login_id": username, "password": password}
        )
        self._check(r, "mattermost login")
        self.http.headers["Authorization"] = f"Bearer {r.headers['Token']}"

    async def user(self, username: str) -> dict[str, Any] | None:
        r = await self.http.get(f"/users/username/{username}")
        return None if r.status_code == 404 else self._check(r, f"user {username}")

    async def ensure_user(
        self, username: str, *, email: str, password: str, first: str, last: str
    ) -> tuple[dict[str, Any], bool]:
        """The user, created if missing. Returns (user, created)."""
        existing = await self.user(username)
        if existing:
            return existing, False
        r = await self.http.post(
            "/users",
            json={
                "username": username,
                "email": email,
                "password": password,
                "first_name": first,
                "last_name": last,
            },
        )
        return self._check(r, f"create user {username}"), True

    async def add_to_team(self, team_name: str, user_id: str) -> None:
        team = self._check(await self.http.get(f"/teams/name/{team_name}"), "team")
        r = await self.http.post(
            f"/teams/{team['id']}/members",
            json={"team_id": team["id"], "user_id": user_id},
        )
        self._check(r, f"add {user_id} to team")

    async def bots(self) -> list[dict[str, Any]]:
        r = await self.http.get(
            "/bots", params={"per_page": 200, "include_deleted": "false"}
        )
        return self._check(r, "bots")

    async def set_picture(self, user_id: str, png: bytes) -> None:
        r = await self.http.post(
            f"/users/{user_id}/image", files={"image": ("avatar.png", png, "image/png")}
        )
        self._check(r, "set picture")
