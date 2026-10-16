# Changelog

## 0.3.0 — 2026-10-16
- One eFax account per tenant, through two optional hooks; with neither set, behaviour is unchanged.
  - `EFAX["ACCOUNT_RESOLVER"]`: `(row, purpose) -> EFaxClient | None`, used by submit ("send"),
    refresh/reconcile ("track") and process_inbound ("inbound"). None on send ends the row
    `suppressed` (a tenant kill switch that also holds queued faxes); on track the row is left
    alone; on inbound the fax stays in the retry path. APP_ID/API_KEY/USER_ID not required.
  - `EFAX["WEBHOOK_SECRET_RESOLVER"]`: `(payload) -> str`, for per-account webhook secrets; the
    HMAC over the raw body is verified with that secret before anything else happens.
  - POLL_INBOUND together with ACCOUNT_RESOLVER raises ImproperlyConfigured.
- `send_fax(..., account_key=)` and `OutboundFax.account_key`; `InboundFax.app_id` recorded from
  notifications. Migration 0003.

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
