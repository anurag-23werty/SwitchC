"""Khata from the terminal (Phase 3: the workflow without Switch).

uv run khata demo                    # D01-D09 through the graph, labelled replies as the humans
uv run khata submit data/invoices/D02_DPK_DPK-1187.pdf
uv run khata pending                 # runs paused at the approval gate
uv run khata decide 2 --as priya approve Matches PO and goods received
uv run khata show 2
uv run khata retry 2                 # re-run a run that stopped on an error
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import yaml

from khata.config import ROOT
from khata.runtime import Khata, Outcome
from khata.tools import parse_decision

DEMO_IDS = [f"D0{i}" for i in range(1, 10)]


def _print_outcome(out: Outcome) -> None:
    for line in out.log:
        print(f"  [{line['agent']}] {line['text']}")
    if out.paused and out.request:
        print("  " + out.request["message"].replace("\n", "\n  "))
    tail = "paused at the approval gate" if out.paused else "run finished"
    print(f"  -> invoice {out.invoice_id}: {out.state} ({tail})")


async def cmd_demo(args: argparse.Namespace) -> int:
    golden = {
        g["id"]: g
        for g in yaml.safe_load((ROOT / "evals" / "golden.yaml").read_text())[
            "invoices"
        ]
    }
    ids = args.ids.split(",") if args.ids else DEMO_IDS
    rows: list[tuple[str, str, str, str, str]] = []
    async with Khata.open() as k:
        for gid in ids:
            g = golden[gid]
            print(f"\n== {gid}: {g['file'].rsplit('/', 1)[-1]}")
            out = await k.submit(g["file"])
            _print_outcome(out)
            for h in g["expected"]["human"]:
                if args.no_replies or not out.paused:
                    break
                print(f"  <{h['by']}> {h['decision']} {h['reason']}")
                out = await k.decide(
                    out.invoice_id, h["by"], h["decision"], h["reason"]
                )
                _print_outcome(out)
            async with k.pool.connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT flags FROM invoices WHERE id = %s", (out.invoice_id,)
                    )
                ).fetchone()
            e = g["expected"]
            want = (
                e["gate_state"] if args.no_replies and e["human"] else e["final_state"]
            )
            ok_state = out.state == want
            ok_flags = sorted(row["flags"]) == sorted(g["expected"]["flags"])
            rows.append(
                (
                    gid,
                    out.state or "-",
                    want,
                    ",".join(sorted(row["flags"])) or "-",
                    "ok" if ok_state and ok_flags else "MISMATCH",
                )
            )
    print(
        "\n{:<5} {:<18} {:<18} {:<30} {}".format("id", "state", "expected", "flags", "")
    )
    for r in rows:
        print("{:<5} {:<18} {:<18} {:<30} {}".format(*r))
    bad = [r for r in rows if r[4] != "ok"]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} as labelled")
    return 1 if bad else 0


async def cmd_submit(args: argparse.Namespace) -> int:
    async with Khata.open() as k:
        out = await k.submit(args.file)
        _print_outcome(out)
    return 0


async def cmd_pending(args: argparse.Namespace) -> int:
    async with Khata.open() as k:
        outs = await k.pending()
    if not outs:
        print("Nothing is waiting for a decision.")
    for o in outs:
        r = o.request or {}
        print(
            f"invoice {o.invoice_id}: {r.get('vendor_name')} {r.get('invoice_no')} "
            f"₹{r.get('total')} {o.state}, waiting for {r.get('next_step')} "
            f"(@{r.get('next_approver')})"
        )
    return 0


async def cmd_decide(args: argparse.Namespace) -> int:
    text = " ".join(args.text)
    parsed = parse_decision(text)
    if parsed is None:
        print("Start the reply with `approve` or `reject`, then the reason.")
        return 2
    async with Khata.open() as k:
        out = await k.decide(
            args.invoice, args.user, parsed["decision"], parsed["reason"]
        )
        _print_outcome(out)
    return 0


async def cmd_show(args: argparse.Namespace) -> int:
    async with Khata.open() as k:
        out = await k.outcome(await k.thread_for(args.invoice))
        _print_outcome(out)
        async with k.pool.connection() as conn:
            notes = await (
                await conn.execute(
                    "SELECT agent, kind, provider, model FROM agent_notes "
                    "WHERE invoice_id = %s ORDER BY id",
                    (args.invoice,),
                )
            ).fetchall()
            calls = await (
                await conn.execute(
                    "SELECT agent, provider, model, cached, ok, attempts, duration_ms "
                    "FROM llm_calls WHERE invoice_id = %s ORDER BY id",
                    (args.invoice,),
                )
            ).fetchall()
    for n in notes:
        print(f"  note: {n['agent']} {n['kind']} via {n['provider']} {n['model']}")
    for c in calls:
        print(
            f"  llm: {c['agent']} {c['provider']} {c['model']} "
            f"{'cached' if c['cached'] else str(c['duration_ms']) + ' ms'} "
            f"{'ok' if c['ok'] else 'FAILED'} x{c['attempts']}"
        )
    return 0


async def cmd_retry(args: argparse.Namespace) -> int:
    async with Khata.open() as k:
        _print_outcome(await k.retry(args.invoice))
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="khata", description=__doc__.split("\n\n")[0])
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="run the demo invoices through the workflow")
    d.add_argument("--ids", help="comma-separated golden ids (default D01-D09)")
    d.add_argument(
        "--no-replies",
        action="store_true",
        help="stop at approvals instead of replying",
    )
    d.set_defaults(fn=cmd_demo)
    s = sub.add_parser("submit", help="start the workflow for one invoice PDF")
    s.add_argument("file")
    s.set_defaults(fn=cmd_submit)
    sub.add_parser("pending", help="invoices waiting for a person").set_defaults(
        fn=cmd_pending
    )
    c = sub.add_parser("decide", help="reply to a paused approval")
    c.add_argument("invoice", type=int)
    c.add_argument("--as", dest="user", required=True)
    c.add_argument("text", nargs="+", help="approve|reject <reason>")
    c.set_defaults(fn=cmd_decide)
    w = sub.add_parser("show", help="an invoice's run, notes and LLM calls")
    w.add_argument("invoice", type=int)
    w.set_defaults(fn=cmd_show)
    r = sub.add_parser("retry", help="re-run a run that stopped on an error")
    r.add_argument("invoice", type=int)
    r.set_defaults(fn=cmd_retry)
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    sys.exit(asyncio.run(args.fn(args)))


if __name__ == "__main__":
    main()
