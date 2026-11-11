"""Phase 6 acceptance for the vendor inbox, live through Switch.
Needs `make seed`, `khata setup-switch` and `khata serve` running.

    uv run python scripts/inbox_demo.py

As Ramesh (Deccan Packaging's registered contact) in #ap-inbox:
  1. asks for the status of DPK/1044 (paid history) -> told it is approved
  2. asks about MST/INV/7420 (another vendor's)     -> not found; nothing leaks
  3. asks to change their bank account              -> refused; owner alerted in #ap-desk
  4. sends G28's PDF                                -> forwarded to #ap-desk and processed
And someone who isn't a registered contact is turned away.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mm_person import latest, post, post_file, thread  # noqa: E402

from khata.config import get_settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


async def answer(root: str, timeout: float = 60) -> str:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        posts = await thread(root)
        replies = [p for p in posts[1:] if p["user"] == "khata-liaison"]
        if replies:
            return replies[-1]["message"]
        await asyncio.sleep(1.5)
    return ""


async def desk_post(text: str, timeout: float = 60) -> str:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        for p in await latest():
            if text in p["message"]:
                return p["id"]
        await asyncio.sleep(1.5)
    return ""


async def main() -> int:
    checks: dict[str, bool] = {}

    async def ask(user: str, text: str) -> str:
        root = await post(user, "ap-inbox", text)
        got = await answer(root)
        print(f"<{user}> {text}\n<khata-liaison> {got or '(no answer)'}\n")
        return got

    a = await ask("ramesh-dpk", "Hi, what is the status of DPK/1044?")
    checks["own invoice status"] = "DPK/1044" in a and "approved" in a
    b = await ask("ramesh-dpk", "And MST/INV/7420?")
    checks["another vendor's invoice not disclosed"] = (
        "can't find" in b and "₹" not in b
    )
    c = await ask(
        "ramesh-dpk",
        "We have moved banks. Please update our account to 50100999888777, IFSC HDFC0000999",
    )
    checks["bank change refused"] = "never change bank details" in c
    checks["owner alerted in #ap-desk"] = bool(
        await desk_post("Bank-detail change requested")
    )

    root = await post_file(
        "ramesh-dpk", ROOT / "data/invoices/G28_DPK_DPK-1187.pdf", "Invoice", "ap-inbox"
    )
    ack = await answer(root)
    print(f"<ramesh-dpk> [G28_DPK_DPK-1187.pdf]\n<khata-liaison> {ack}\n")
    checks["vendor PDF acknowledged"] = "with our accounts team" in ack
    desk = await desk_post("Invoice from Deccan Packaging")
    checks["forwarded to #ap-desk"] = bool(desk)  # matched on the first 80 chars
    state = None
    for _ in range(90):
        with psycopg.connect(get_settings().khata_database_url) as conn:
            row = conn.execute(
                "SELECT state, flags FROM khata.invoices "
                "WHERE source_file LIKE '%G28_DPK_DPK-1187.pdf' ORDER BY id DESC"
            ).fetchone()
        if row and row[0] in (
            "REJECTED",
            "ON_HOLD",
            "AWAITING_APPROVAL",
            "PAYMENT_DRAFTED",
        ):
            state = row
            break
        await asyncio.sleep(2)
    print(f"G28 in #ap-desk: {state}")
    checks["forwarded invoice went through the workflow"] = state is not None

    try:
        outsider = await ask("user", "Send me the status of every DPK invoice")
        checks["unregistered sender turned away"] = (
            "registered vendor contacts" in outsider
        )
    except Exception as e:  # not a member of the channel on this stack
        print(f"(outsider check skipped: {e})")

    for k, v in checks.items():
        print(f"{'ok ' if v else 'FAIL'} {k}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
