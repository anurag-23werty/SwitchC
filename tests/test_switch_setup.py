"""setup-switch builds the team from Khata's database, with a password per new account."""

from __future__ import annotations

import pytest

from khata import switch_setup as ss


class FakeMM:
    def __init__(self, existing: set[str]) -> None:
        self.existing, self.passwords, self.teams = existing, {}, []

    async def ensure_user(self, username, *, email, password, first, last):
        if username in self.existing:
            return {"id": username}, False
        self.passwords[username] = password
        return {"id": username}, True

    async def add_to_team(self, team, user_id):
        self.teams.append(user_id)


TEAM = ss.Team(
    people=[
        {
            "username": "meera",
            "display_name": "Meera Iyer",
            "role": "owner",
            "email": None,
        },
        {
            "username": "arun",
            "display_name": "Arun Nair",
            "role": "finance_manager",
            "email": "a@x.in",
        },
    ],
    contacts=[{"username": "ravi-nova", "display_name": "Ravi Shetty"}],
    approvers={"finance_manager": "arun", "owner": "meera"},
    policy="",
)


@pytest.mark.asyncio
async def test_a_real_team_gets_a_different_one_time_password_each(monkeypatch):
    monkeypatch.setattr(ss, "demo_password", lambda: None)
    mm = FakeMM(existing={"meera"})
    created = await ss.ensure_people(mm, "t", TEAM)
    assert set(created) == {"arun", "ravi-nova"}  # meera already had an account
    assert created["arun"] != created["ravi-nova"] and len(created["arun"]) >= 15
    assert sorted(mm.teams) == ["arun", "meera", "ravi-nova"]


@pytest.mark.asyncio
async def test_the_demo_uses_its_shared_password(monkeypatch):
    monkeypatch.setattr(ss, "demo_password", lambda: "Khata-demo")
    mm = FakeMM(existing=set())
    created = await ss.ensure_people(mm, "t", TEAM)
    assert set(mm.passwords.values()) == {"Khata-demo"}
    assert set(created.values()) == {"(KHATA_DEMO_PASSWORD)"}
