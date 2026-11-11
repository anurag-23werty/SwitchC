"""Setting Khata up for a real business, and keeping its masters current.

    uv run khata init khata.yaml --as owner         # company, team, approval policy
    uv run khata import vendors vendors.csv --as priya
    uv run khata import pos pos.csv --as priya
    uv run khata import grns grns.csv --as ravi
    uv run khata import contacts contacts.csv --as priya
    uv run khata policy set auto_approve_below=5000 --as owner --reason "..."

Every import is validated row by row and is safe to re-run (existing rows are
updated or skipped, never duplicated). Two things are deliberately hard:

* **Bank details.** Khata pays only to the account in the vendor master, so a
  re-import that changes a vendor's account or IFSC is refused unless the owner
  passes --allow-bank-change (after calling the vendor on the number on file),
  and the change is written to the audit log.
* **The approval policy.** Only the owner can change it, and a change is a new
  version in `rules`, never an edit.

Templates for each file are in examples/.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from khata.checks.gst import STATE_NAMES, is_valid_gstin

IFSC = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
ROLES = ("clerk", "finance_manager", "owner")


class OnboardingError(ValueError):
    """Something in a config or import file Khata won't accept."""


def _gstin(v: str) -> str:
    v = re.sub(r"\s", "", v).upper()
    if not is_valid_gstin(v):
        raise ValueError(f"{v!r} is not a valid GSTIN (format or check digit)")
    return v


def _ifsc(v: str) -> str:
    v = re.sub(r"\s", "", v).upper()
    if not IFSC.match(v):
        raise ValueError(f"{v!r} is not a valid IFSC (4 letters, 0, 6 characters)")
    return v


def _account(v: str) -> str:
    digits = re.sub(r"[\s-]", "", v)
    if not re.fullmatch(r"\d{6,20}", digits):
        raise ValueError(f"{v!r} is not a bank account number (6-20 digits)")
    return digits


def _money(v: Any, name: str) -> Decimal:
    try:
        d = Decimal(str(v).replace(",", "").strip())
    except InvalidOperation as e:
        raise ValueError(f"{name}: {v!r} is not a number") from e
    if d < 0:
        raise ValueError(f"{name} can't be negative")
    return d


# --- the config file (company, team, policy) ------------------------------------


class CompanyIn(BaseModel):
    name: str = Field(min_length=2)
    gstin: str
    address: str = Field(min_length=5)
    bank_name: str
    bank_account: str
    ifsc: str

    @field_validator("gstin")
    @classmethod
    def _valid_gstin(cls, v: str) -> str:
        return _gstin(v)

    @field_validator("ifsc")
    @classmethod
    def _valid_ifsc(cls, v: str) -> str:
        return _ifsc(v)

    @field_validator("bank_account")
    @classmethod
    def _valid_account(cls, v: str) -> str:
        return _account(v)

    @property
    def state_code(self) -> str:
        return self.gstin[:2]


class PersonIn(BaseModel):
    username: str
    display_name: str = Field(min_length=2)
    role: str
    email: str | None = None

    @field_validator("username")
    @classmethod
    def _user(cls, v: str) -> str:
        v = v.strip().lstrip("@").lower()
        if not USERNAME.match(v):
            raise ValueError(f"{v!r}: use lowercase letters, digits, '.', '_' or '-'")
        return v

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        if v not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        return v


class PolicyIn(BaseModel):
    """The approval policy. Defaults suit a small business; change them in the file."""

    auto_approve_below: Decimal = Decimal("10000.00")
    manager_up_to: Decimal = Decimal("100000.00")
    variance_pct: Decimal = Decimal("2.0")
    duplicate_window_days: int = 7
    arithmetic_tolerance: Decimal = Decimal("1.00")
    po_required: bool = True
    grn_required: bool = True
    min_extraction_confidence: Decimal = Decimal("0.70")
    injection_score_threshold: Decimal = Decimal("0.90")
    approvers: dict[str, str]  # finance_manager, owner -> username

    @model_validator(mode="after")
    def _check(self) -> PolicyIn:
        if self.manager_up_to <= self.auto_approve_below:
            raise ValueError("manager_up_to must be above auto_approve_below")
        missing = {"finance_manager", "owner"} - set(self.approvers)
        if missing:
            raise ValueError(f"approvers needs {', '.join(sorted(missing))}")
        return self

    def as_policy(self, base: dict[str, Any] | None = None) -> dict[str, Any]:
        """The `rules.policy` JSON, keeping anything else the current version has
        (Steward settings, vendor exceptions)."""
        out = dict(base or {})
        out.setdefault(
            "steward",
            {
                "min_overrides": 2,
                "eligible_flags": ["PRICE_VARIANCE"],
                "max_variance_pct": "10.0",
            },
        )
        out.setdefault("vendor_exceptions", [])
        out.setdefault("bank_change", {"route": "owner", "hard_stop": True})
        out.setdefault("first_time_vendor_route", "manager")
        for k, v in self.model_dump().items():
            out[k] = str(v) if isinstance(v, Decimal) else v
        return out


class OrgConfig(BaseModel):
    company: CompanyIn
    people: list[PersonIn] = Field(min_length=1)
    policy: PolicyIn

    @model_validator(mode="after")
    def _approvers_exist(self) -> OrgConfig:
        roles = {p.username: p.role for p in self.people}
        for role, user in self.policy.approvers.items():
            if roles.get(user) != role:
                raise ValueError(
                    f"approver {user!r} for {role} must be listed in people with role {role}"
                )
        return self


def load_config(path: Path) -> OrgConfig:
    try:
        return OrgConfig.model_validate(yaml.safe_load(path.read_text()))
    except (yaml.YAMLError, ValueError) as e:
        raise OnboardingError(f"{path}: {e}") from e


def _person(conn: psycopg.Connection, username: str) -> dict | None:
    row = conn.execute(
        "SELECT username, role, display_name FROM khata.people WHERE username = %s AND active",
        (username.lstrip("@").lower(),),
    ).fetchone()
    return (
        dict(zip(("username", "role", "display_name"), row, strict=True))
        if row
        else None
    )


def _require(conn: psycopg.Connection, username: str, roles: tuple[str, ...]) -> dict:
    who = _person(conn, username)
    if who is None or who["role"] not in roles:
        raise OnboardingError(
            f"{username} must be an active {' or '.join(roles)} in Khata to do this"
        )
    return who


def _audit(conn, actor: str, action: str, reason: str, details: dict) -> None:
    conn.execute(
        """INSERT INTO khata.audit_log (actor, actor_kind, action, reason, details)
           VALUES (%s, 'human', %s, %s, %s)""",
        (actor, action, reason, json.dumps(details, default=str)),
    )


def apply_config(conn: psycopg.Connection, cfg: OrgConfig, by: str) -> list[str]:
    """Write the company, people and policy. First run: anyone (there is no team yet).
    Later runs: only the owner, and a policy change becomes a new version."""
    report: list[str] = []
    with conn.transaction():
        first = conn.execute("SELECT count(*) FROM khata.people").fetchone()[0] == 0
        if first:
            if not any(p.username == by and p.role == "owner" for p in cfg.people):
                raise OnboardingError(
                    f"--as {by}: the first run must be by an owner listed in the file"
                )
        else:
            _require(conn, by, ("owner",))
        c = cfg.company
        conn.execute(
            """INSERT INTO khata.company (id, name, gstin, state_code, address, bank_name,
                                          bank_account, ifsc)
               VALUES (1, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, gstin = EXCLUDED.gstin,
                 state_code = EXCLUDED.state_code, address = EXCLUDED.address,
                 bank_name = EXCLUDED.bank_name, bank_account = EXCLUDED.bank_account,
                 ifsc = EXCLUDED.ifsc""",
            (
                c.name,
                c.gstin,
                c.state_code,
                c.address,
                c.bank_name,
                c.bank_account,
                c.ifsc,
            ),
        )
        report.append(
            f"company: {c.name} ({c.gstin}, {STATE_NAMES.get(c.state_code, c.state_code)})"
        )
        for p in cfg.people:
            conn.execute(
                """INSERT INTO khata.people (username, display_name, role, email, active)
                   VALUES (%s, %s, %s, %s, true)
                   ON CONFLICT (username) DO UPDATE SET display_name = EXCLUDED.display_name,
                     role = EXCLUDED.role, email = EXCLUDED.email, active = true""",
                (p.username, p.display_name, p.role, p.email),
            )
        report.append(
            f"people: {', '.join(f'{p.username} ({p.role})' for p in cfg.people)}"
        )
        active = conn.execute(
            "SELECT version, policy FROM khata.rules WHERE active"
        ).fetchone()
        new = cfg.policy.as_policy(active[1] if active else None)
        if active is None:
            conn.execute(
                """INSERT INTO khata.rules (version, policy, change_summary, proposed_by,
                                            approved_by, active)
                   VALUES (1, %s, 'Initial approval policy', %s, %s, true)""",
                (json.dumps(new), by, by),
            )
            report.append("policy: v1 created")
        elif new != active[1]:
            v = _new_version(conn, new, "Policy updated from the config file", by)
            report.append(f"policy: v{v} created (v{active[0]} kept)")
        else:
            report.append(f"policy: v{active[0]} unchanged")
        _audit(conn, by, "config_applied", "khata init", {"report": report})
    return report


def _new_version(conn, policy: dict, summary: str, by: str) -> int:
    v = conn.execute(
        "SELECT COALESCE(max(version), 0) + 1 FROM khata.rules"
    ).fetchone()[0]
    conn.execute("UPDATE khata.rules SET active = false WHERE active")
    conn.execute(
        """INSERT INTO khata.rules (version, policy, change_summary, proposed_by, approved_by, active)
           VALUES (%s, %s, %s, %s, %s, true)""",
        (v, json.dumps(policy), summary, by, by),
    )
    return v


POLICY_KEYS = {
    "auto_approve_below": "money",
    "manager_up_to": "money",
    "variance_pct": "money",
    "arithmetic_tolerance": "money",
    "duplicate_window_days": "int",
    "po_required": "bool",
    "grn_required": "bool",
}


def set_policy(
    conn: psycopg.Connection, changes: dict[str, str], by: str, reason: str
) -> int:
    """Owner-only: a new policy version with these values changed."""
    if len(reason.strip()) < 3:
        raise OnboardingError("a policy change needs a reason (--reason)")
    with conn.transaction():
        _require(conn, by, ("owner",))
        row = conn.execute(
            "SELECT policy FROM khata.rules WHERE active FOR UPDATE"
        ).fetchone()
        if row is None:
            raise OnboardingError("no policy yet: run `khata init` first")
        policy = dict(row[0])
        parts = []
        for k, raw in changes.items():
            kind = POLICY_KEYS.get(k)
            if kind is None:
                raise OnboardingError(
                    f"unknown policy key {k!r}; one of {', '.join(POLICY_KEYS)}"
                )
            if kind == "money":
                value: Any = str(_money(raw, k))
            elif kind == "int":
                value = int(raw)
            else:
                if raw.lower() not in ("true", "false", "yes", "no", "1", "0"):
                    raise OnboardingError(f"{k} must be true or false")
                value = raw.lower() in ("true", "yes", "1")
            parts.append(f"{k} {policy.get(k)} -> {value}")
            policy[k] = value
        try:
            PolicyIn.model_validate(policy)  # still a coherent policy
        except ValueError as e:
            raise OnboardingError(f"that would make the policy invalid: {e}") from e
        v = _new_version(conn, policy, f"{'; '.join(parts)} ({reason.strip()})", by)
        _audit(
            conn,
            by,
            "policy_changed",
            reason.strip(),
            {"version": v, "changes": changes},
        )
    return v


# --- CSV imports --------------------------------------------------------------------


@dataclass
class ImportReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"added {len(self.added)}, updated {len(self.updated)}, "
            f"unchanged {len(self.unchanged)}, refused {len(self.refused)}"
        ]
        out += [f"  refused: {r}" for r in self.refused]
        return out


def read_csv(path: Path, required: set[str]) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = {c.strip() for c in reader.fieldnames or []}
        missing = required - cols
        if missing:
            raise OnboardingError(
                f"{path.name}: missing columns {', '.join(sorted(missing))}"
            )
        return [
            {k.strip(): (v or "").strip() for k, v in row.items() if k}
            for row in reader
        ]


VENDOR_COLS = {
    "code",
    "name",
    "gstin",
    "address",
    "phone",
    "email",
    "bank_name",
    "bank_account",
    "ifsc",
}


def import_vendors(
    conn: psycopg.Connection,
    rows: list[dict[str, str]],
    by: str,
    allow_bank_change: bool = False,
) -> ImportReport:
    rep = ImportReport()
    with conn.transaction():
        who = _require(conn, by, ("finance_manager", "owner"))
        if allow_bank_change and who["role"] != "owner":
            raise OnboardingError("only the owner can allow bank-detail changes")
        for n, r in enumerate(rows, start=2):
            label = f"line {n} ({r.get('code') or '?'})"
            try:
                code = r["code"].upper()
                if not re.fullmatch(r"[A-Z0-9-]{2,12}", code):
                    raise ValueError("code: 2-12 letters, digits or '-'")
                gstin, ifsc, acct = (
                    _gstin(r["gstin"]),
                    _ifsc(r["ifsc"]),
                    _account(r["bank_account"]),
                )
                if not r["phone"]:
                    raise ValueError(
                        "phone is required: it's the number Khata tells the owner to call"
                    )
                first_seen = (
                    date.fromisoformat(r["first_seen"])
                    if r.get("first_seen")
                    else date.today()
                )
            except (ValueError, KeyError) as e:
                rep.refused.append(f"{label}: {e}")
                continue
            cur = conn.execute(
                "SELECT bank_account, ifsc, name, gstin, address, phone, email, bank_name "
                "FROM khata.vendors WHERE code = %s",
                (code,),
            ).fetchone()
            values = (
                r["name"],
                gstin,
                gstin[:2],
                r["address"],
                r["phone"],
                r["email"],
                r["bank_name"],
                acct,
                ifsc,
            )
            if cur is None:
                clash = conn.execute(
                    "SELECT code FROM khata.vendors WHERE gstin = %s", (gstin,)
                ).fetchone()
                if clash:
                    rep.refused.append(
                        f"{label}: GSTIN already belongs to vendor {clash[0]}"
                    )
                    continue
                conn.execute(
                    """INSERT INTO khata.vendors (code, name, gstin, state_code, address, phone, email,
                           bank_name, bank_account, ifsc, first_seen, bank_verified_by, bank_verified_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())""",
                    (code, *values, first_seen, by),
                )
                rep.added.append(code)
                continue
            bank_changed = (cur[0], cur[1]) != (acct, ifsc)
            if bank_changed and not allow_bank_change:
                rep.refused.append(
                    f"{label}: bank details differ from the vendor master. Call the vendor on "
                    f"the number on file ({cur[5]}); if confirmed, the owner re-runs with "
                    "--allow-bank-change"
                )
                continue
            if (cur[2], cur[3], cur[4], cur[5], cur[6], cur[7], cur[0], cur[1]) == (
                r["name"],
                gstin,
                r["address"],
                r["phone"],
                r["email"],
                r["bank_name"],
                acct,
                ifsc,
            ):
                rep.unchanged.append(code)
                continue
            conn.execute(
                """UPDATE khata.vendors SET name = %s, gstin = %s, state_code = %s, address = %s,
                       phone = %s, email = %s, bank_name = %s, bank_account = %s, ifsc = %s,
                       updated_at = now()
                   WHERE code = %s""",
                (*values, code),
            )
            if bank_changed:
                conn.execute(
                    "UPDATE khata.vendors SET bank_verified_by = %s, bank_verified_at = now() WHERE code = %s",
                    (by, code),
                )
                _audit(
                    conn,
                    by,
                    "vendor_bank_changed",
                    f"{code} bank details changed by the owner",
                    {
                        "vendor": code,
                        "old": f"xxxx{cur[0][-4:]} / {cur[1]}",
                        "new": f"xxxx{acct[-4:]} / {ifsc}",
                    },
                )
            rep.updated.append(code)
        _audit(
            conn,
            by,
            "vendors_imported",
            "CSV import",
            {"added": rep.added, "updated": rep.updated, "refused": len(rep.refused)},
        )
    return rep


PO_COLS = {
    "po_number",
    "vendor_code",
    "po_date",
    "line_no",
    "description",
    "hsn",
    "qty",
    "unit",
    "rate",
    "gst_rate",
}


def import_pos(
    conn: psycopg.Connection, rows: list[dict[str, str]], by: str
) -> ImportReport:
    """Purchase orders, one row per line. A PO already in Khata is left as it is."""
    rep = ImportReport()
    groups: dict[str, list[tuple[int, dict[str, str]]]] = {}
    for n, r in enumerate(rows, start=2):
        groups.setdefault(r.get("po_number", "").upper(), []).append((n, r))
    with conn.transaction():
        _require(conn, by, ("finance_manager", "owner"))
        for po_no, lines in groups.items():
            label = f"{po_no or '(no number)'} (line {lines[0][0]})"
            if conn.execute(
                "SELECT 1 FROM khata.purchase_orders WHERE po_number = %s", (po_no,)
            ).fetchone():
                rep.unchanged.append(po_no)
                continue
            try:
                if not po_no:
                    raise ValueError("po_number is empty")
                head = lines[0][1]
                vendor = conn.execute(
                    "SELECT id FROM khata.vendors WHERE code = %s",
                    (head["vendor_code"].upper(),),
                ).fetchone()
                if vendor is None:
                    raise ValueError(
                        f"vendor {head['vendor_code']!r} isn't in the vendor master"
                    )
                if any(
                    r["vendor_code"].upper() != head["vendor_code"].upper()
                    for _, r in lines
                ):
                    raise ValueError("all lines of a PO must have the same vendor")
                parsed = []
                for n, r in lines:
                    qty, rate = _money(r["qty"], "qty"), _money(r["rate"], "rate")
                    if qty <= 0:
                        raise ValueError(f"line {n}: qty must be above 0")
                    parsed.append(
                        (
                            int(r["line_no"]),
                            r["description"],
                            r["hsn"],
                            qty,
                            r["unit"],
                            rate,
                            _money(r["gst_rate"], "gst_rate"),
                        )
                    )
                po_date = date.fromisoformat(head["po_date"])
            except (ValueError, KeyError) as e:
                rep.refused.append(f"{label}: {e}")
                continue
            po_id = conn.execute(
                "INSERT INTO khata.purchase_orders (po_number, vendor_id, po_date) VALUES (%s, %s, %s) RETURNING id",
                (po_no, vendor[0], po_date),
            ).fetchone()[0]
            for line in parsed:
                conn.execute(
                    """INSERT INTO khata.po_lines (po_id, line_no, description, hsn, qty, unit, rate, gst_rate)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (po_id, *line),
                )
            rep.added.append(po_no)
        _audit(
            conn,
            by,
            "pos_imported",
            "CSV import",
            {"added": rep.added, "refused": len(rep.refused)},
        )
    return rep


GRN_COLS = {
    "grn_number",
    "po_number",
    "received_date",
    "received_by",
    "line_no",
    "qty_received",
}


def import_grns(
    conn: psycopg.Connection, rows: list[dict[str, str]], by: str
) -> ImportReport:
    """Goods receipts, one row per received PO line (line_no is the PO's line)."""
    rep = ImportReport()
    groups: dict[str, list[tuple[int, dict[str, str]]]] = {}
    for n, r in enumerate(rows, start=2):
        groups.setdefault(r.get("grn_number", "").upper(), []).append((n, r))
    with conn.transaction():
        _require(conn, by, ("clerk", "finance_manager", "owner"))
        for grn_no, lines in groups.items():
            label = f"{grn_no or '(no number)'} (line {lines[0][0]})"
            if conn.execute(
                "SELECT 1 FROM khata.goods_receipts WHERE grn_number = %s", (grn_no,)
            ).fetchone():
                rep.unchanged.append(grn_no)
                continue
            try:
                if not grn_no:
                    raise ValueError("grn_number is empty")
                head = lines[0][1]
                po = conn.execute(
                    "SELECT id FROM khata.purchase_orders WHERE po_number = %s",
                    (head["po_number"].upper(),),
                ).fetchone()
                if po is None:
                    raise ValueError(
                        f"PO {head['po_number']!r} isn't in Khata; import it first"
                    )
                parsed = []
                for n, r in lines:
                    pl = conn.execute(
                        "SELECT id FROM khata.po_lines WHERE po_id = %s AND line_no = %s",
                        (po[0], int(r["line_no"])),
                    ).fetchone()
                    if pl is None:
                        raise ValueError(
                            f"line {n}: PO {head['po_number']} has no line {r['line_no']}"
                        )
                    parsed.append((pl[0], _money(r["qty_received"], "qty_received")))
                received = date.fromisoformat(head["received_date"])
            except (ValueError, KeyError) as e:
                rep.refused.append(f"{label}: {e}")
                continue
            grn_id = conn.execute(
                """INSERT INTO khata.goods_receipts (grn_number, po_id, received_date, received_by)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (grn_no, po[0], received, head["received_by"] or by),
            ).fetchone()[0]
            for pl_id, qty in parsed:
                conn.execute(
                    "INSERT INTO khata.grn_lines (grn_id, po_line_id, qty_received) VALUES (%s, %s, %s)",
                    (grn_id, pl_id, qty),
                )
            rep.added.append(grn_no)
        _audit(
            conn,
            by,
            "grns_imported",
            "CSV import",
            {"added": rep.added, "refused": len(rep.refused)},
        )
    return rep


CONTACT_COLS = {"username", "vendor_code", "display_name"}


def import_contacts(
    conn: psycopg.Connection, rows: list[dict[str, str]], by: str
) -> ImportReport:
    """Vendor-side people allowed in the vendor inbox, each for one vendor only."""
    rep = ImportReport()
    with conn.transaction():
        _require(conn, by, ("finance_manager", "owner"))
        for n, r in enumerate(rows, start=2):
            user = r.get("username", "").lstrip("@").lower()
            vendor = conn.execute(
                "SELECT id FROM khata.vendors WHERE code = %s",
                (r.get("vendor_code", "").upper(),),
            ).fetchone()
            if not USERNAME.match(user) or vendor is None or not r.get("display_name"):
                rep.refused.append(
                    f"line {n}: needs a valid username, a known vendor_code and a display_name"
                )
                continue
            res = conn.execute(
                """INSERT INTO khata.vendor_contacts (username, vendor_id, display_name, registered_by)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (username) DO UPDATE SET vendor_id = EXCLUDED.vendor_id,
                     display_name = EXCLUDED.display_name, registered_by = EXCLUDED.registered_by
                   RETURNING (xmax = 0)""",
                (user, vendor[0], r["display_name"], by),
            ).fetchone()
            (rep.added if res[0] else rep.updated).append(user)
    return rep
