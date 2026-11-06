"""Phase 4 acceptance: drop D01-D09 into #ap-desk as a person, answer as the labelled
approvers in each thread, and check every outcome. Needs `khata serve` running.

    uv run python scripts/room_demo.py            # D01-D09
    uv run python scripts/room_demo.py D03 D04    # some of them

Each invoice is posted by Ravi (the clerk), like a real AP desk. When the Router
tags an approver, the labelled reply is posted by that person in the thread.
The final state is read from Khata's database, and the thread is printed.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import psycopg
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mm_person import post_file, reply, thread  # noqa: E402

from khata.config import get_settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DONE = (
    "Reply `approve",
    "Khata never pays",
    "must reissue",
    "rejected the",
    "couldn't finish",
)


async def settle(root: str, seen: int, timeout: float = 420) -> list[dict]:
    """Wait for the thread to reach a resting point after `seen` posts."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        posts = await thread(root)
        new = posts[seen:]
        if any(m in p["message"] for p in new for m in DONE):
            await asyncio.sleep(3)  # let trailing posts (the CSV, the feed) land
            return await thread(root)
        await asyncio.sleep(3)
    raise TimeoutError(f"thread {root} did not settle")


def db_state(file_name: str) -> tuple[str, list[str]]:
    """The newest invoice from this file. (Switch's thread ids aren't Mattermost's
    post ids, so the file is the link between the two.)"""
    with psycopg.connect(get_settings().khata_database_url) as c:
        row = c.execute(
            "SELECT state, flags FROM khata.invoices WHERE source_file LIKE %s "
            "ORDER BY id DESC LIMIT 1",
            (f"%{file_name}",),
        ).fetchone()
    return (row[0], list(row[1])) if row else ("-", [])


async def run_one(g: dict) -> tuple[str, str, str, list[str], bool]:
    print(f"\n===== {g['id']}: {Path(g['file']).name}")
    root = await post_file("ravi", ROOT / g["file"], "New invoice")
    posts = await settle(root, 1)
    for h in g["expected"]["human"]:
        await reply(h["by"], root, f"{h['decision']} {h['reason']}")
        posts = await settle(root, len(posts) + 1)
    for p in posts:
        files = f"  [file: {', '.join(p['files'])}]" if p["files"] else ""
        print(f"<{p['user']}> {p['message']}{files}")
    state, flags = db_state(Path(g["file"]).name)
    e = g["expected"]
    ok = state == e["final_state"] and sorted(flags) == sorted(e["flags"])
    return g["id"], state, e["final_state"], flags, ok


async def main(ids: list[str]) -> int:
    golden = yaml.safe_load((ROOT / "evals/golden.yaml").read_text())["invoices"]
    chosen = (
        [g for g in golden if g["id"] in ids]
        if ids
        else [g for g in golden if g["id"].startswith("D")]
    )
    rows = [await run_one(g) for g in chosen]
    print("\nid    state             expected          flags")
    for gid, state, want, flags, ok in rows:
        print(
            f"{gid:<5} {state:<17} {want:<17} {','.join(flags) or '-':<22} {'ok' if ok else 'MISMATCH'}"
        )
    good = sum(r[4] for r in rows)
    print(f"\n{good}/{len(rows)} as labelled, end to end through Mattermost and Switch")
    return 0 if good == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
