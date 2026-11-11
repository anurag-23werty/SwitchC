# Vendor inbox and the Vendor Liaison (Phase 6)

Vendors send invoices and ask "has it been paid?" all day. The vendor inbox gives
them a room for that: **`#ap-inbox`**, a Telegram group in production (a Mattermost
channel on the demo stack), where **`khata-liaison`** answers.

Vendors are outsiders, so the Liaison is **plain code with no LLM**: nothing they
write can steer a model.

| A registered vendor contact sends | The Liaison |
|---|---|
| "status of DPK/1187?" | Gives that invoice's stage in vendor-safe words (received / waiting for approval / under review / approved, payment as per terms / returned) |
| "any update?" (no number) | Lists their five most recent invoices |
| another company's invoice number | "I can't find this one from <their company>" (nothing about it is revealed) |
| a PDF | Forwards it to `#ap-desk` as a new invoice ("sent by … through the vendor inbox"); the normal workflow takes over |
| "we changed banks, new account …" | **Refuses** ("we never change bank details from a chat message"), alerts **@owner in `#ap-desk`** with the vendor's phone number *on file*, posts to `#ap-audit`, writes `audit_log` |

Anyone who isn't in `vendor_contacts` is told to ask their account manager to register
them, and gets nothing else. Staff posting in the inbox are left alone.

**What a vendor is told about a rejection:** only what they can fix (invalid GSTIN,
wrong GST type, unequal CGST/SGST, arithmetic, wrong buyer GSTIN, duplicate number).
A rejection or hold for anything else (a bank-detail mismatch, text aimed at agents,
a person's decision) is "please contact our accounts team". Fraud signals are never
explained to the possible fraudster.

## On the demo stack (Mattermost)

`uv run khata setup-switch` registers the Liaison, creates Mattermost accounts for the
seeded vendor contacts (`ramesh-dpk` for Deccan Packaging, `sunita-mst` for Mumbai
Steel; password `KHATA_DEMO_PASSWORD` in `.env`), and deploys
`switch/khata-inbox.template.yaml`.

```sh
uv run khata serve                     # in one terminal
uv run python scripts/inbox_demo.py    # status, another vendor's invoice, bank change, PDF
```

## On Telegram

Switch's Telegram bridge long-polls the Bot API, so no public URL is needed.

1. In Telegram, talk to **@BotFather**: `/newbot`, keep the token safe, then
   `/mybots` → the bot → Bot Settings → **Group Privacy → Turn off**.
2. In Switch's operator dashboard: **Messaging Apps → Register messaging app →
   Telegram**, with the bot token and username.
3. Create a Telegram group for vendors and add the bot. Switch adopts the group as a
   room and says so in the chat.
4. In the group: `!invite-agent @khata-liaison`.
5. Put the room's id (from the dashboard) in `.env` as `KHATA_INBOX_ROOM_ID=…` and
   restart `khata serve`. The Liaison now answers in Telegram.
6. Register each vendor contact by the name Switch reports for them
   (`INSERT INTO khata.vendor_contacts (username, vendor_id, display_name,
   registered_by) …`). Unregistered people get the registration message.

This path is documented from Switch's Telegram guide and has **not** been run here:
it needs a bot created on your Telegram account. Everything after "a message arrives
in the room" is the same code that is tested live on Mattermost.

## Verified

- `tests/test_liaison.py` (4) and the inbox tests in `tests/test_switch_service.py`
  (5): unregistered senders learn nothing; a contact sees only their own invoices;
  internal rejection reasons stay internal; a bank-change request is refused, the
  owner alerted with the number on file and the audit log written; a PDF is forwarded
  and picked up by the Dispatcher; staff are ignored.
- Live through Switch (`scripts/inbox_demo.py`): all of the above, with the forwarded
  PDF going through the full workflow in `#ap-desk`.
