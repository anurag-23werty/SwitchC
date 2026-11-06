-- Khata's system of record. Switch keeps the conversation; this keeps the money.
-- Everything lives in the `khata` schema so `make seed` can rebuild it cleanly.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS khata;
SET search_path = khata, public;

-- The buyer: the business running Khata.
CREATE TABLE company (
    id          smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    name        text NOT NULL,
    gstin       char(15) NOT NULL,
    state_code  char(2) NOT NULL,
    address     text NOT NULL,
    bank_name     text NOT NULL,   -- the account payments are debited from
    bank_account  text NOT NULL,
    ifsc          char(11) NOT NULL
);

-- People in the finance room. `username` is their Mattermost username, which is
-- what Switch reports as `sender_name`; the role decides what they may approve.
CREATE TABLE people (
    id            serial PRIMARY KEY,
    username      text NOT NULL UNIQUE,
    display_name  text NOT NULL,
    role          text NOT NULL CHECK (role IN ('clerk', 'finance_manager', 'owner'))
);

-- Vendor master. Bank details here are the only ones Khata will ever pay to.
CREATE TABLE vendors (
    id            serial PRIMARY KEY,
    code          text NOT NULL UNIQUE,
    name          text NOT NULL,
    gstin         char(15) NOT NULL UNIQUE,
    state_code    char(2) NOT NULL,
    address       text NOT NULL,
    phone         text NOT NULL,        -- the number to call back on a bank change
    email         text NOT NULL,
    bank_name     text NOT NULL,
    bank_account  text NOT NULL,
    ifsc          char(11) NOT NULL,
    first_seen    date NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE purchase_orders (
    id         serial PRIMARY KEY,
    po_number  text NOT NULL UNIQUE,
    vendor_id  int NOT NULL REFERENCES vendors(id),
    po_date    date NOT NULL,
    status     text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed'))
);

CREATE TABLE po_lines (
    id           serial PRIMARY KEY,
    po_id        int NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    line_no      int NOT NULL,
    description  text NOT NULL,
    hsn          text NOT NULL,
    qty          numeric(14, 3) NOT NULL CHECK (qty > 0),
    unit         text NOT NULL,
    rate         numeric(14, 2) NOT NULL CHECK (rate >= 0),
    gst_rate     numeric(5, 2) NOT NULL,
    UNIQUE (po_id, line_no)
);

-- Goods received notes: what actually arrived at the warehouse.
CREATE TABLE goods_receipts (
    id             serial PRIMARY KEY,
    grn_number     text NOT NULL UNIQUE,
    po_id          int NOT NULL REFERENCES purchase_orders(id),
    received_date  date NOT NULL,
    received_by    text NOT NULL
);

CREATE TABLE grn_lines (
    id            serial PRIMARY KEY,
    grn_id        int NOT NULL REFERENCES goods_receipts(id) ON DELETE CASCADE,
    po_line_id    int NOT NULL REFERENCES po_lines(id),
    qty_received  numeric(14, 3) NOT NULL CHECK (qty_received >= 0),
    UNIQUE (grn_id, po_line_id)
);

-- Versioned approval policy. Exactly one version is active; a change is a new
-- row (proposed by an agent or a person, approved by a person), never an edit.
CREATE TABLE rules (
    version         int PRIMARY KEY,
    policy          jsonb NOT NULL,
    change_summary  text NOT NULL,
    proposed_by     text NOT NULL,
    approved_by     text NOT NULL,
    active          boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX rules_one_active ON rules (active) WHERE active;

CREATE TABLE invoices (
    id                     serial PRIMARY KEY,
    vendor_id              int REFERENCES vendors(id),   -- null until the vendor is identified
    invoice_no             text,
    invoice_date           date,
    po_number              text,
    supplier_gstin         text,
    buyer_gstin            text,
    taxable_value          numeric(14, 2),
    cgst                   numeric(14, 2),
    sgst                   numeric(14, 2),
    igst                   numeric(14, 2),
    total                  numeric(14, 2),
    bank_account           text,
    ifsc                   text,
    state                  text NOT NULL DEFAULT 'RECEIVED' CHECK (state IN (
                               'RECEIVED', 'EXTRACTED', 'VALIDATED', 'MATCHED', 'REVIEWED',
                               'AWAITING_APPROVAL', 'APPROVED', 'PAYMENT_DRAFTED',
                               'ON_HOLD', 'REJECTED')),
    flags                  text[] NOT NULL DEFAULT '{}',
    extracted              jsonb,                       -- both model readings + the agreed one
    extraction_confidence  numeric(4, 3),
    idempotency_key        text,                        -- hash(vendor, invoice_no, total)
    source_file            text,
    file_sha256            char(64),
    switch_room_id         text,
    switch_thread_id       text,                        -- the invoice's thread in the room
    route                  text,                        -- set by route_approval
    approval_steps         text[] NOT NULL DEFAULT '{}', -- e.g. {manager,owner}
    approvers              text[] NOT NULL DEFAULT '{}', -- usernames for those steps
    callback_instruction   text,                        -- bank-change hard stop
    raw_text               text,                        -- the document's text (untrusted)
    received_at            timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX invoices_vendor_no ON invoices (vendor_id, invoice_no);
CREATE INDEX invoices_vendor_total ON invoices (vendor_id, total, invoice_date);
CREATE INDEX invoices_idempotency ON invoices (idempotency_key);

CREATE TABLE invoice_lines (
    id           serial PRIMARY KEY,
    invoice_id   int NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    line_no      int NOT NULL,
    description  text NOT NULL,
    hsn          text,
    qty          numeric(14, 3) NOT NULL,
    unit         text,
    rate         numeric(14, 2) NOT NULL,
    gst_rate     numeric(5, 2),
    amount       numeric(14, 2) NOT NULL,
    UNIQUE (invoice_id, line_no)
);

-- Every money decision: by a person, by the policy (auto-approve), or a hold.
CREATE TABLE decisions (
    id                 serial PRIMARY KEY,
    invoice_id         int NOT NULL REFERENCES invoices(id),
    decided_by         text NOT NULL,       -- username, or 'policy'
    decider_kind       text NOT NULL CHECK (decider_kind IN ('human', 'policy')),
    decision           text NOT NULL CHECK (decision IN ('approve', 'reject', 'auto_approve')),
    approval_step      text,                -- manager | owner, for two-step approvals
    reason             text NOT NULL,
    rule_version       int REFERENCES rules(version),
    switch_message_id  text,                -- the room message the decision came from
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX decisions_invoice ON decisions (invoice_id);

-- Append-only trail: every state change and every notable agent action.
CREATE TABLE audit_log (
    id          bigserial PRIMARY KEY,
    invoice_id  int REFERENCES invoices(id),
    actor       text NOT NULL,          -- agent name, username, or 'system'
    actor_kind  text NOT NULL CHECK (actor_kind IN ('agent', 'human', 'system')),
    action      text NOT NULL,
    from_state  text,
    to_state    text,
    reason      text,
    details     jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_invoice ON audit_log (invoice_id, created_at);

-- Every tool call an agent makes, allowed or denied by the policy engine.
CREATE TABLE tool_calls (
    id           bigserial PRIMARY KEY,
    invoice_id   int REFERENCES invoices(id),
    agent        text NOT NULL,
    tool         text NOT NULL,
    allowed      boolean NOT NULL,
    args         jsonb NOT NULL DEFAULT '{}',
    result       jsonb,
    error        text,
    duration_ms  int,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- What the team knows about each vendor. Embeddings are 384-d (bge-small /
-- all-MiniLM class, run locally), filled in when the Analyst needs retrieval.
CREATE TABLE vendor_knowledge (
    id                  serial PRIMARY KEY,
    vendor_id           int NOT NULL REFERENCES vendors(id),
    kind                text NOT NULL CHECK (kind IN ('note', 'decision', 'pattern')),
    content             text NOT NULL,
    source_decision_id  int REFERENCES decisions(id),
    embedding           vector(384),
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- Draft payment batches. Khata never pays: a batch is a NEFT bulk-upload CSV
-- and a journal entry for a person to upload to the bank.
CREATE TABLE payment_batches (
    id          serial PRIMARY KEY,
    batch_ref   text NOT NULL UNIQUE,
    status      text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'exported', 'cancelled')),
    created_by  text NOT NULL,
    csv_path    text,
    journal     jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- One line per invoice. The unique idempotency key makes a second draft for the
-- same invoice impossible, however many times Payer is asked.
CREATE TABLE payment_lines (
    id                   serial PRIMARY KEY,
    batch_id             int NOT NULL REFERENCES payment_batches(id),
    invoice_id           int NOT NULL UNIQUE REFERENCES invoices(id),
    idempotency_key      text NOT NULL UNIQUE,
    amount               numeric(14, 2) NOT NULL CHECK (amount > 0),
    beneficiary_name     text NOT NULL,
    beneficiary_account  text NOT NULL,
    ifsc                 char(11) NOT NULL
);
