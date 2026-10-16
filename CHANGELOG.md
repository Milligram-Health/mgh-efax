# Changelog

## 0.2.0 — 2026-10-16
- `EFAX["SENDING_ENABLED"]`: when False, `submit()` dials nothing and the row ends in the new final
  state `suppressed` (fires `outbound_fax_finished`).
- `EFAX["REDIRECT_TO"]`: when set, every outbound fax dials that number instead, with a `[TEST]`
  cover page naming the intended recipient (masked). New `OutboundFax.dialed_number` records what
  was dialled; `was_redirected` property. An invalid redirect number fails the fax, never falls
  through to the real recipient.
- Migration 0002 (adds `dialed_number`, the `suppressed` state).

## 0.1.0 — 2026-10-01
- Stdlib eFax Enterprise API client: tokens, send (single + multi, eFax's 50/25 limits), metadata,
  image (PDF/TIFF), list sent/received with paging, delete, cancel.
- Region (data sovereignty) on the account config: `us` default -> api.securedocex.us, `ca`,
  `global` (.com) or any code; `base_url` overrides the host entirely.
- Send safety: no blind retries of POST /faxes; `SendUncertain` + `find_sent(client_reference_id)`.
- Webhook verification (X-HMAC-Signature, hex SHA256) and inbound/outbound notification parsing.
- Django app `mgh_efax.contrib.django`: shared `OutboundFax` / `InboundFax` tables (migration 0001),
  `api.send_fax()` from any feature with a generic `source` link, atomic claim-before-send,
  sweep (Celery beat task or `efax_sweep` command), webhook view, `inbound_fax_received` /
  `outbound_fax_finished` signals, read-only admin.
- Field names from eFax's Fax Services OAS2 spec v1.2.
