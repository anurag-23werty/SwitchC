"""Onboarding a real business from the example files, and what Khata refuses."""

from __future__ import annotations

import copy
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from khata import migrate
from khata import onboarding as ob
from khata.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"
DB = "khata_onboarding_test"


@pytest.fixture(scope="module")
def conn():
    admin = get_settings().khata_database_url
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as c:
            c.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DB)))
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB)))
    except psycopg.OperationalError:
        pytest.skip("khata-db not reachable; run `make up`")
    url = admin.rsplit("/", 1)[0] + "/" + DB
    migrate.migrate_url(url)
    with psycopg.connect(url) as c:
        cfg = ob.load_config(EX / "khata.yaml")
        ob.apply_config(c, cfg, "meera")
        yield c


def one(c, q, *a):
    return c.execute(q, a).fetchone()[0]


def test_the_config_sets_up_company_team_and_policy_v1(conn):
    assert one(conn, "SELECT name FROM khata.company") == "Kaveri Home Products Pvt Ltd"
    assert one(conn, "SELECT count(*) FROM khata.people") == 3
    assert (
        one(conn, "SELECT policy->>'po_required' FROM khata.rules WHERE active")
        == "false"
    )
    assert one(conn, "SELECT version FROM khata.rules WHERE active") == 1


def test_re_applying_the_same_config_changes_nothing_and_needs_the_owner(conn):
    cfg = ob.load_config(EX / "khata.yaml")
    assert "policy: v1 unchanged" in ob.apply_config(conn, cfg, "meera")
    with pytest.raises(ob.OnboardingError, match="owner"):
        ob.apply_config(conn, cfg, "arun")


def test_examples_import_and_re_import_cleanly(conn):
    v = ob.import_vendors(conn, ob.read_csv(EX / "vendors.csv", ob.VENDOR_COLS), "arun")
    assert sorted(v.added) == ["NOV", "SHI", "TAM"] and not v.refused
    p = ob.import_pos(conn, ob.read_csv(EX / "pos.csv", ob.PO_COLS), "arun")
    assert sorted(p.added) == ["PO-2026-0001", "PO-2026-0002"]
    g = ob.import_grns(conn, ob.read_csv(EX / "grns.csv", ob.GRN_COLS), "divya")
    assert g.added == ["GRN-2026-0001"]
    c = ob.import_contacts(
        conn, ob.read_csv(EX / "contacts.csv", ob.CONTACT_COLS), "arun"
    )
    assert c.added == ["ravi-nova"]
    again = ob.import_vendors(
        conn, ob.read_csv(EX / "vendors.csv", ob.VENDOR_COLS), "arun"
    )
    assert sorted(again.unchanged) == ["NOV", "SHI", "TAM"]
    assert ob.import_pos(
        conn, ob.read_csv(EX / "pos.csv", ob.PO_COLS), "arun"
    ).unchanged


def test_bad_rows_are_refused_with_a_reason(conn):
    rows = ob.read_csv(EX / "vendors.csv", ob.VENDOR_COLS)
    bad = copy.deepcopy(rows[:1])
    bad[0].update(code="BAD", gstin="29AAFCN5678P1ZZ")  # wrong check digit
    rep = ob.import_vendors(conn, bad, "arun")
    assert rep.refused and "GSTIN" in rep.refused[0]
    po = [
        {
            **ob.read_csv(EX / "pos.csv", ob.PO_COLS)[0],
            "po_number": "PO-X",
            "vendor_code": "ZZZ",
        }
    ]
    assert "vendor master" in ob.import_pos(conn, po, "arun").refused[0]
    with pytest.raises(ob.OnboardingError, match="finance_manager or owner"):
        ob.import_vendors(conn, rows, "divya")  # a clerk


def test_a_changed_bank_account_needs_the_owner_and_is_audited(conn):
    rows = ob.read_csv(EX / "vendors.csv", ob.VENDOR_COLS)
    changed = [{**rows[0], "bank_account": "999988887777"}]
    rep = ob.import_vendors(conn, changed, "arun")
    assert rep.refused and "number on file" in rep.refused[0]
    assert (
        one(conn, "SELECT bank_account FROM khata.vendors WHERE code='NOV'")
        == "0412201005555"
    )
    with pytest.raises(ob.OnboardingError, match="only the owner"):
        ob.import_vendors(conn, changed, "arun", allow_bank_change=True)
    ok = ob.import_vendors(conn, changed, "meera", allow_bank_change=True)
    assert ok.updated == ["NOV"]
    assert (
        one(conn, "SELECT bank_verified_by FROM khata.vendors WHERE code='NOV'")
        == "meera"
    )
    assert (
        one(
            conn,
            "SELECT count(*) FROM khata.audit_log WHERE action='vendor_bank_changed'",
        )
        == 1
    )


def test_only_the_owner_changes_the_policy_and_each_change_is_a_version(conn):
    with pytest.raises(ob.OnboardingError, match="owner"):
        ob.set_policy(conn, {"auto_approve_below": "5000"}, "arun", "lower limit")
    with pytest.raises(ob.OnboardingError):
        ob.set_policy(conn, {"manager_up_to": "1000"}, "meera", "below the auto limit")
    v = ob.set_policy(
        conn, {"auto_approve_below": "5000", "po_required": "true"}, "meera", "tighter"
    )
    assert v == 2
    assert (
        one(conn, "SELECT policy->>'auto_approve_below' FROM khata.rules WHERE active")
        == "5000"
    )
    assert one(conn, "SELECT count(*) FROM khata.rules") == 2
