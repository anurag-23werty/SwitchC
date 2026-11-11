"""Khata from the terminal (Phase 3: the workflow without Switch).

uv run khata demo                    # D01-D09 through the graph, labelled replies as the humans
uv run khata submit data/invoices/D02_DPK_DPK-1187.pdf
uv run khata pending                 # runs paused at the approval gate
uv run khata decide 2 --as priya approve Matches PO and goods received
uv run khata show 2
uv run khata retry 2                 # re-run a run that stopped on an error
uv run khata rules                   # policy versions and the Policy Steward's proposals
uv run khata rule 1 --as owner approve Freight up to 4% is agreed with them
uv run khata steward-demo            # D03, D04 -> proposal -> owner approves -> S01 auto
uv run khata dashboard               # http://127.0.0.1:8088, read-only
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


async def cmd_rules(args: argparse.Namespace) -> int:
    async with Khata.open() as k:
        async with k.pool.connection() as conn:
            versions = await (
                await conn.execute(
                    "SELECT version, active, change_summary, proposed_by, approved_by "
                    "FROM rules ORDER BY version"
                )
            ).fetchall()
        proposals = await k.proposals()
    for v in versions:
        print(
            f"v{v['version']}{' (active)' if v['active'] else ''}: {v['change_summary']} "
            f"[proposed by {v['proposed_by']}, approved by {v['approved_by']}]"
        )
    for p in proposals:
        print(
            f"proposal #{p['id']} {p['status']}: {p['summary']}"
            + (f" -> v{p['new_version']}" if p["new_version"] else "")
        )
    return 0


async def cmd_rule(args: argparse.Namespace) -> int:
    from khata.steward import ProposalRefused, decision_message

    parsed = parse_decision(" ".join(args.text))
    if parsed is None:
        print("Start the reply with `approve` or `reject`, then the reason.")
        return 2
    async with Khata.open() as k:
        try:
            res = await k.decide_rule(
                args.proposal, args.user, parsed["decision"], parsed["reason"]
            )
        except ProposalRefused as e:
            print(f"Not recorded: {e}")
            return 1
    print(decision_message(res))
    return 0


async def cmd_steward_demo(args: argparse.Namespace) -> int:
    """D03 and D04 approved by Priya -> the Steward proposes -> the owner approves ->
    S01 (same pattern, small) is auto-approved under the new version."""
    golden = {
        g["id"]: g
        for g in yaml.safe_load((ROOT / "evals" / "golden.yaml").read_text())[
            "invoices"
        ]
    }
    async with Khata.open() as k:
        for gid in ("D03", "D04"):
            g = golden[gid]
            print(f"\n== {gid}")
            out = await k.submit(g["file"])
            _print_outcome(out)
            for h in g["expected"]["human"]:
                print(f"  <{h['by']}> {h['decision']} {h['reason']}")
                out = await k.decide(
                    out.invoice_id, h["by"], h["decision"], h["reason"]
                )
                _print_outcome(out)
        open_ = await k.proposals("proposed")
        if not open_:
            print("\nNo proposal was made.")
            return 1
        p = open_[-1]
        print("\n== The Steward's proposal\n  " + p["message"].replace("\n", "\n  "))
        reason = "Freight up to 4% is agreed with Mumbai Steel"
        print(f"  <owner> approve {reason}")
        from khata.steward import decision_message

        res = await k.decide_rule(p["id"], "owner", "approve", reason)
        print("  [khata-steward] " + decision_message(res))
        print("\n== S01 (same freight pattern, small order)")
        out = await k.submit("data/invoices/S01_MST_MST-INV-7840.pdf")
        _print_outcome(out)
    ok = out.state == "PAYMENT_DRAFTED" and not out.paused
    print(
        "\nS01 auto-approved under the new policy"
        if ok
        else "\nS01 was NOT auto-approved"
    )
    return 0 if ok else 1


async def cmd_dashboard(args: argparse.Namespace) -> int:
    import uvicorn

    from khata.dashboard import create_app

    config = uvicorn.Config(
        create_app(), host=args.host, port=args.port, log_level="info"
    )
    await uvicorn.Server(config).serve()
    return 0


def _admin_conn():
    import psycopg

    from khata.config import get_settings

    return psycopg.connect(get_settings().khata_database_url)


async def cmd_init(args: argparse.Namespace) -> int:
    from pathlib import Path

    from khata.config import get_settings
    from khata.migrate import migrate_url
    from khata.onboarding import OnboardingError, apply_config, load_config

    try:
        cfg = load_config(Path(args.config))
        new = migrate_url(get_settings().khata_database_url)
        if new:
            print("schema: applied " + ", ".join(new))
        with _admin_conn() as conn:
            for line in apply_config(conn, cfg, args.user):
                print(line)
    except OnboardingError as e:
        print(f"Not applied: {e}")
        return 1
    print(
        "Next: import vendors (and POs/GRNs if you use them), then `khata setup-switch`."
    )
    return 0


async def cmd_import(args: argparse.Namespace) -> int:
    from pathlib import Path

    from khata import onboarding as ob

    kinds = {
        "vendors": (ob.VENDOR_COLS, ob.import_vendors),
        "pos": (ob.PO_COLS, ob.import_pos),
        "grns": (ob.GRN_COLS, ob.import_grns),
        "contacts": (ob.CONTACT_COLS, ob.import_contacts),
    }
    cols, fn = kinds[args.kind]
    try:
        rows = ob.read_csv(Path(args.file), cols)
        with _admin_conn() as conn:
            if args.kind == "vendors":
                rep = fn(
                    conn, rows, args.user, allow_bank_change=args.allow_bank_change
                )
            else:
                rep = fn(conn, rows, args.user)
    except ob.OnboardingError as e:
        print(f"Not imported: {e}")
        return 1
    print(f"{args.kind}: " + "\n".join(rep.lines()))
    return 1 if rep.refused else 0


async def cmd_vendors(args: argparse.Namespace) -> int:
    with _admin_conn() as conn:
        rows = conn.execute(
            "SELECT code, name, gstin, phone, bank_account, ifsc, active, bank_verified_by "
            "FROM khata.vendors ORDER BY code"
        ).fetchall()
    if not rows:
        print("No vendors yet: `khata import vendors vendors.csv --as <you>`.")
    for code, name, gstin, phone, acct, ifsc, active, verified in rows:
        print(
            f"{code:<8} {name[:32]:<32} {gstin}  {phone:<18} xxxx{acct[-4:]} {ifsc}"
            f"{'' if active else '  (inactive)'}{f'  bank verified by {verified}' if verified else ''}"
        )
    return 0


async def cmd_policy(args: argparse.Namespace) -> int:
    import json

    from khata.onboarding import OnboardingError, set_policy

    if args.action == "show":
        with _admin_conn() as conn:
            row = conn.execute(
                "SELECT version, policy FROM khata.rules WHERE active"
            ).fetchone()
        if row is None:
            print("No policy yet: run `khata init`.")
            return 1
        print(f"policy v{row[0]}")
        print(json.dumps(row[1], indent=2))
        return 0
    try:
        changes = dict(kv.split("=", 1) for kv in args.changes)
    except ValueError:
        print("Use key=value, e.g. auto_approve_below=5000")
        return 2
    try:
        with _admin_conn() as conn:
            v = set_policy(conn, changes, args.user, args.reason or "")
    except OnboardingError as e:
        print(f"Not changed: {e}")
        return 1
    print(f"Policy v{v} is active. The previous version is kept.")
    return 0


async def cmd_db(args: argparse.Namespace) -> int:
    from khata.config import get_settings
    from khata.migrate import applied, available, migrate_url, pending_url

    url = get_settings().khata_database_url
    if args.action == "migrate":
        new = migrate_url(url)
        print("applied: " + (", ".join(new) if new else "nothing, already up to date"))
        return 0
    import psycopg

    with psycopg.connect(url) as conn:
        done = applied(conn)
    for v in available():
        print(f"{'applied' if v in done else 'PENDING'}  {v}")
    return 1 if pending_url(url) else 0


async def cmd_setup_switch(args: argparse.Namespace) -> int:
    from khata.config import get_settings
    from khata.switch_setup import ensure_demo_password, setup

    if args.demo:
        ensure_demo_password()
    report = await setup(get_settings())
    created = report.pop("accounts_created", {})
    for k, v in report.items():
        print(f"{k}: {v}")
    if created:
        print(
            "\nNew Mattermost accounts (hand each password over privately; shown once):"
        )
        for user, pw in created.items():
            print(f"  {user:<20} {pw}")
    return 0


async def cmd_serve(args: argparse.Namespace) -> int:
    from khata.config import get_settings
    from khata.switch_service import serve

    await serve(get_settings())
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
    ini = sub.add_parser(
        "init", help="set Khata up for your business from a config file"
    )
    ini.add_argument("config", help="khata.yaml (see examples/khata.yaml)")
    ini.add_argument(
        "--as", dest="user", required=True, help="your username (an owner)"
    )
    ini.set_defaults(fn=cmd_init)
    imp = sub.add_parser(
        "import", help="import vendors, POs, GRNs or vendor contacts from CSV"
    )
    imp.add_argument("kind", choices=["vendors", "pos", "grns", "contacts"])
    imp.add_argument("file")
    imp.add_argument("--as", dest="user", required=True)
    imp.add_argument(
        "--allow-bank-change",
        action="store_true",
        help="owner only, after calling the vendor on the number on file",
    )
    imp.set_defaults(fn=cmd_import)
    sub.add_parser("vendors", help="the vendor master").set_defaults(fn=cmd_vendors)
    pol = sub.add_parser("policy", help="show or change the approval policy (owner)")
    pol.add_argument("action", choices=["show", "set"])
    pol.add_argument(
        "changes", nargs="*", help="key=value, e.g. auto_approve_below=5000"
    )
    pol.add_argument("--as", dest="user")
    pol.add_argument("--reason")
    pol.set_defaults(fn=cmd_policy)
    dbp = sub.add_parser("db", help="schema migrations")
    dbp.add_argument("action", choices=["migrate", "status"])
    dbp.set_defaults(fn=cmd_db)
    sub.add_parser("rules", help="policy versions and rule proposals").set_defaults(
        fn=cmd_rules
    )
    ru = sub.add_parser("rule", help="the owner's answer to a rule proposal")
    ru.add_argument("proposal", type=int)
    ru.add_argument("--as", dest="user", required=True)
    ru.add_argument("text", nargs="+", help="approve|reject <reason>")
    ru.set_defaults(fn=cmd_rule)
    sub.add_parser(
        "steward-demo", help="D03, D04, the Steward's proposal, the owner, then S01"
    ).set_defaults(fn=cmd_steward_demo)
    db = sub.add_parser("dashboard", help="read-only web view of the books")
    db.add_argument("--host", default="127.0.0.1")
    db.add_argument("--port", type=int, default=8088)
    db.set_defaults(fn=cmd_dashboard)
    ss = sub.add_parser(
        "setup-switch", help="register agents, people and rooms on Switch"
    )
    ss.add_argument(
        "--demo", action="store_true", help="demo team: one shared password from .env"
    )
    ss.set_defaults(fn=cmd_setup_switch)
    sub.add_parser(
        "serve", help="run Khata's agents in #ap-desk and #ap-audit"
    ).set_defaults(fn=cmd_serve)
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    sys.exit(asyncio.run(args.fn(args)))


if __name__ == "__main__":
    main()
