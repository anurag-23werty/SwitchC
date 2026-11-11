"""Act as a person in Mattermost, for end-to-end runs without clicking.

Posts exactly what a person would through Mattermost's own API (the same path
as the web app), so Switch and Khata see an ordinary human post.

    uv run python scripts/mm_person.py post --as ravi data/invoices/D01_SOS_SOS-26-27-0412.pdf
    uv run python scripts/mm_person.py reply --as priya <root post id> approve Matches PO
    uv run python scripts/mm_person.py thread <root post id>      # print the thread
    uv run python scripts/mm_person.py latest                    # recent root posts in #ap-desk
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
MM = "http://localhost:8065/api/v4"


def password_for(user: str) -> str:
    if user == "user":
        return dotenv_values(ROOT / "switch/stack/.env")["MATTERMOST_USER_PASSWORD"]
    return dotenv_values(ROOT / ".env")["KHATA_DEMO_PASSWORD"]


async def session(user: str) -> httpx.AsyncClient:
    c = httpx.AsyncClient(base_url=MM, timeout=60)
    r = await c.post(
        "/users/login", json={"login_id": user, "password": password_for(user)}
    )
    r.raise_for_status()
    c.headers["Authorization"] = f"Bearer {r.headers['Token']}"
    return c


async def channel_id(c: httpx.AsyncClient, name: str) -> str:
    team = dotenv_values(ROOT / "switch/stack/.env")["MATTERMOST_TEAM_NAME"]
    t = (await c.get(f"/teams/name/{team}")).json()
    r = await c.get(f"/teams/{t['id']}/channels/name/{name}")
    r.raise_for_status()
    return r.json()["id"]


async def post_file(
    user: str, path: Path, caption: str, channel: str = "ap-desk"
) -> str:
    c = await session(user)
    ch = await channel_id(c, channel)
    up = await c.post(
        "/files",
        data={"channel_id": ch},
        files={"files": (path.name, path.read_bytes(), "application/pdf")},
    )
    up.raise_for_status()
    fid = up.json()["file_infos"][0]["id"]
    r = await c.post(
        "/posts", json={"channel_id": ch, "message": caption, "file_ids": [fid]}
    )
    r.raise_for_status()
    await c.aclose()
    return r.json()["id"]


async def post(user: str, channel: str, text: str) -> str:
    """A new top-level post, as `user`."""
    c = await session(user)
    ch = await channel_id(c, channel)
    r = await c.post("/posts", json={"channel_id": ch, "message": text})
    r.raise_for_status()
    await c.aclose()
    return r.json()["id"]


async def reply(user: str, root: str, text: str, channel: str = "ap-desk") -> str:
    c = await session(user)
    ch = await channel_id(c, channel)
    r = await c.post(
        "/posts", json={"channel_id": ch, "message": text, "root_id": root}
    )
    r.raise_for_status()
    await c.aclose()
    return r.json()["id"]


async def thread(root: str, user: str = "user") -> list[dict]:
    c = await session(user)
    r = await c.get(f"/posts/{root}/thread")
    r.raise_for_status()
    data = r.json()
    users: dict[str, str] = {}
    out = []
    for pid in sorted(data["order"], key=lambda i: data["posts"][i]["create_at"]):
        p = data["posts"][pid]
        uid = p["user_id"]
        if uid not in users:
            users[uid] = (await c.get(f"/users/{uid}")).json()["username"]
        files = [f["name"] for f in (p.get("metadata") or {}).get("files") or []]
        out.append(
            {"id": pid, "user": users[uid], "message": p["message"], "files": files}
        )
    await c.aclose()
    return out


async def latest(
    user: str = "user", n: int = 15, channel: str = "ap-desk"
) -> list[dict]:
    c = await session(user)
    ch = await channel_id(c, channel)
    data = (await c.get(f"/channels/{ch}/posts", params={"per_page": 100})).json()
    roots = [data["posts"][i] for i in data["order"] if not data["posts"][i]["root_id"]]
    await c.aclose()
    return [{"id": p["id"], "message": p["message"][:80]} for p in roots[:n]]


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("post")
    p.add_argument("--as", dest="user", default="ravi")
    p.add_argument("--caption", default="New invoice")
    p.add_argument("files", nargs="+")
    r = sub.add_parser("reply")
    r.add_argument("--as", dest="user", required=True)
    r.add_argument("root")
    r.add_argument("text", nargs="+")
    t = sub.add_parser("thread")
    t.add_argument("root")
    sub.add_parser("latest")
    a = ap.parse_args()
    if a.cmd == "post":
        for f in a.files:
            print(asyncio.run(post_file(a.user, Path(f), a.caption)), f)
    elif a.cmd == "reply":
        print(asyncio.run(reply(a.user, a.root, " ".join(a.text))))
    elif a.cmd == "thread":
        for m in asyncio.run(thread(a.root)):
            files = f" [files: {', '.join(m['files'])}]" if m["files"] else ""
            print(f"<{m['user']}> {m['message']}{files}\n")
    else:
        for m in asyncio.run(latest()):
            print(m["id"], m["message"])
    sys.exit(0)


if __name__ == "__main__":
    main()
