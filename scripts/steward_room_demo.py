"""Phase 6 acceptance for the Policy Steward, live in Mattermost through Switch.
Needs a freshly seeded database (`make seed`) and `khata serve` running.

    uv run python scripts/steward_room_demo.py

1. Ravi posts D03 and D04; Priya approves both (the labelled replies).
2. The Steward posts rule proposal #1 in #ap-desk, tagging the owner.
3. Priya tries to approve it (refused: only the owner), then the owner approves.
4. Ravi posts S01 (same 4% freight, small order): auto-approved under policy v2.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import psycopg
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mm_person import latest, reply, thread  # noqa: E402
from room_demo import ROOT, db_state, run_one  # noqa: E402

from khata.config import get_settings  # noqa: E402


async def wait_for(root: str, text: str, timeout: float = 120) -> list[dict]:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        posts = await thread(root)
        if any(text in p["message"] for p in posts):
            return posts
        await asyncio.sleep(2)
    raise TimeoutError(f"{text!r} never appeared in thread {root}")


async def find_proposal(timeout: float = 60) -> str:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        for p in await latest():
            if p["message"].startswith("**Rule proposal #"):
                return p["id"]
        await asyncio.sleep(2)
    raise TimeoutError("no rule proposal was posted")


def show(posts: list[dict]) -> None:
    for p in posts:
        files = f"  [file: {', '.join(p['files'])}]" if p["files"] else ""
        print(f"<{p['user']}> {p['message']}{files}")


async def main() -> int:
    golden = {
        g["id"]: g
        for g in yaml.safe_load((ROOT / "evals/golden.yaml").read_text())["invoices"]
    }
    results = [await run_one(golden[i]) for i in ("D03", "D04")]

    print("\n===== The Steward's proposal")
    root = await find_proposal()
    await reply("priya", root, "approve Fine by me")
    await wait_for(root, "only the owner")
    await reply("owner", root, "approve Freight up to 4% is agreed with Mumbai Steel")
    show(await wait_for(root, "is now active"))

    s01 = {
        "id": "S01",
        "file": "data/invoices/S01_MST_MST-INV-7840.pdf",
        "expected": {
            "human": [],
            "final_state": "PAYMENT_DRAFTED",
            "flags": ["PRICE_VARIANCE"],
        },
    }
    results.append(await run_one(s01))

    with psycopg.connect(get_settings().khata_database_url) as c:
        version = c.execute("SELECT version FROM khata.rules WHERE active").fetchone()[
            0
        ]
        prop = c.execute(
            "SELECT status, decided_by, switch_thread_id IS NOT NULL, "
            "switch_message_id IS NOT NULL FROM khata.rule_proposals ORDER BY id DESC"
        ).fetchone()
        route = c.execute(
            "SELECT route FROM khata.invoices WHERE source_file LIKE '%S01_%' "
            "ORDER BY id DESC"
        ).fetchone()[0]
    print("\nid    state             expected")
    for gid, state, want, _flags, ok in results:
        print(f"{gid:<5} {state:<17} {want:<17} {'ok' if ok else 'MISMATCH'}")
    checks = {
        "policy v2 active": version == 2,
        "proposal approved by the owner, from the room": prop
        == ("approved", "owner", True, True),
        "S01 auto-approved": route == "auto"
        and db_state("S01_MST_MST-INV-7840.pdf")[0] == "PAYMENT_DRAFTED",
    }
    for k, v in checks.items():
        print(f"{'ok ' if v else 'FAIL'} {k}")
    return 0 if all(r[4] for r in results) and all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
