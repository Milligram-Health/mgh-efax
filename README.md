# mgh-efax

Milligram Health's client for the **eFax Enterprise API** (eFax Corporate), plus a drop-in Django fax outbox and inbox. It is shared code, not a service:
each app installs a pinned version and talks to eFax directly with **its own** eFax account and keys.
Whoever owns the app signs with eFax (agreement + BAA) and configures their own credentials; the package
supplies none.

Fax PHI stays with whoever owns the account. The package holds no credentials, sends no telemetry and
contacts nothing but the eFax API. It never logs fax bodies, and it logs fax numbers masked to the
last four digits.

**Integrating this into an app? Start with [INTEGRATING.md](INTEGRATING.md)**, a step-by-step checklist with the questions to ask before building.

## Install

```
pip install "mgh-efax @ git+https://github.com/Milligram-Health/mgh-efax@v0.2.0"          # client only
pip install "mgh-efax[django,celery] @ git+https://github.com/Milligram-Health/mgh-efax@v0.2.0"  # + Django app
```

The repo is public, so installing needs no credentials. The client has no runtime dependencies (stdlib `urllib`), so it doesn't disturb the host app's pins. Always pin a tag.

## Client

```python
from mgh_efax import EFaxClient, EFaxConfig, SendRequest, Destination, Document, SendUncertain

client = EFaxClient(EFaxConfig(app_id=..., api_key=..., user_id=...))  # one per AppID + fax number
ref = client.send_fax(SendRequest(
    destinations=[Destination("612-555-0142", to_name="Main St Pharmacy")],
    documents=[Document(pdf_bytes)],
    client_reference_id=str(my_row.reference),
))
client.get_metadata(ref.fax_id).status
```

- Talks to **`https://api.securedocex.us`** by default. Our eFax accounts are US-sovereign, and eFax requires `.us`, not `.com`.
- Tokens are cached and refreshed 60 s before they expire, and once more on a 401.
- **Reads** retry network errors, 429 and 5xx with backoff.
- **Sends** retry only when eFax provably did not take the request (a 429, or a connection that never opened).
  Otherwise they raise `SendUncertain`. Don't resend after that: call `find_sent(client_reference_id)` to check what went out.
- `verify_signature(body, signature, secret)` and `parse_notification(body)` handle webhooks. Both inbound faxes and outbound delivery status arrive this way.

## Region (data sovereignty)

An eFax account can only be reached on its own region's host, so region is part of the account config:

| `region` | Host |
|---|---|
| `us` (default) | `https://api.securedocex.us` |
| `ca` | `https://api.securedocex.ca` |
| `global` | `https://api.securedocex.com` |
| any other code | `https://api.securedocex.<code>` |

`EFaxConfig(region="ca")` or `EFAX["REGION"] = "ca"` selects a region. `base_url` / `EFAX["BASE_URL"]` replaces the whole host for an endpoint that doesn't follow the pattern. HTTPS is enforced.

## Django app: one fax outbox + inbox per project (`mgh_efax.contrib.django`)

The package ships the tables, so no feature builds its own. Every feature in a project queues into the same
`OutboundFax` table, and every received fax lands in `InboundFax`. Each deployment has its own tables in its own
database, so two apps never share fax rows.

```python
INSTALLED_APPS += ["mgh_efax.contrib.django"]          # then: manage.py migrate
EFAX = {"APP_ID": env("EFAX_APP_ID"), "API_KEY": env("EFAX_API_KEY"), "USER_ID": env("EFAX_USER_ID"),
        "REGION": "us", "STORAGE": "default",
        "WEBHOOK_SECRET": env("EFAX_WEBHOOK_SECRET", default="")}   # only if receiving
CELERY_BEAT_SCHEDULE["efax-sweep"] = {"task": "mgh_efax.sweep", "schedule": 60.0}
# no Celery? run `manage.py efax_sweep` from cron every minute instead
```

**Send from anywhere:**

```python
from mgh_efax.contrib.django.api import send_fax, faxes_for

send_fax(to=clinic.fax, document=pdf_bytes, source=referral, tag="referrals.letter",
         to_name=clinic.name, cover_subject="Referral")
faxes_for(referral)            # every fax sent about it
```

`send_fax` writes the row and stores the PDF in one transaction. After commit it hands the row to a Celery worker. If Celery or the broker is unavailable, the next sweep sends it instead.

**React to what happens:**

```python
from django.dispatch import receiver
from mgh_efax.contrib.django.signals import inbound_fax_received, outbound_fax_finished

@receiver(inbound_fax_received)     # fax.image is already stored
def route_inbound(sender, fax, **kw): ...

@receiver(outbound_fax_finished)    # fax.state is DELIVERED or FAILED
def mark_sent(sender, fax, **kw): ...
```

**Inbound:**
- `path("webhooks/efax/", InboundWebhookView.as_view())` verifies the HMAC, records the fax once per `fax_id` and answers 200 immediately.
- A worker then downloads the PDF and fires `inbound_fax_received`.
- If a receiver raises, the fax is marked `handler_failed` and the sweep retries it, so receivers must be idempotent.
- Without a webhook, set `POLL_INBOUND: True` and the sweep pulls undownloaded faxes instead.
- Outbound delivery-status webhooks hit the same URL and finish the matching outbound row.

**Outbound states:**

| State | Meaning |
|---|---|
| `queued` | Waiting for a worker |
| `sending` | A worker has claimed the row atomically, so two workers can never both send it |
| `submitted` | eFax accepted the fax |
| `delivered` / `failed` | Final outcome |
| `uncertain` | The send's result is unknown. Reconciled against eFax's sent list by our reference, never resent blindly |

A `sending` row whose worker died is moved to `uncertain` after 10 minutes. Failures record eFax's `error_code` (E44 and so on).

**Inbound states:** `received → stored → handled`, or `handler_failed`, which the sweep retries.

**Test environments: never fax real recipients from dev.**

| Setting | Effect |
|---|---|
| `SENDING_ENABLED: False` | Nothing is dialled. Rows end in `suppressed`, which is final, so re-enabling never releases a backlog. |
| `REDIRECT_TO: "<number>"` | **Every** fax dials that number instead. The intended recipient stays in `to_number`, and `dialed_number` records what was actually dialled. A `[TEST]` cover page names the intended recipient, with the number masked. |

A typical non-production setup is `REDIRECT_TO = env("EFAX_TEST_FAX_NUMBER")` and `SENDING_ENABLED = bool(REDIRECT_TO)`, with production forcing both off and on respectively. Both checks happen inside `submit()`, the only place a fax is dialled, so no code path can get around them.

**PHI:** documents and images go to the `STORAGE` alias, so point it at encrypted, private storage. The admin pages are read-only.

## Source of truth

Field names come from eFax's Fax Services OAS2 spec (Stoplight project `consensus/fax-services`, v1.2) and the
[Enterprise API docs](https://docs.efaxcorporate.com/API/content/index.htm). Details that matter in practice:

- **Host:** `api.securedocex.us`. The public docs only show `.com`. eFax told us to use `.us` for US data sovereignty; the host resolves and `/health` answers 200.
- **Send:** `POST /faxes` answers **201 with an array**, one `{fax_id, destination_fax_number}` per destination. Accepted does not mean delivered.
  - Limits: 50 destinations (eFax silently drops extras, so we raise), 25 documents and 100 MB after base64.
  - PDFs that are PDF/A-3, fillable, unflattened, encrypted or password-protected are rejected. Flatten them before sending.
- **Delivery status:** `transmission_data.transmission_status`, which is `NEW | INPROGRESS | COMPLETE | ERROR | CANCELED`.
  - `fax_status` is image *storage* (`STORED`/`NOT_STORED`); don't read it as delivery.
  - Failures carry `error_code` (E06, E44, ...) and `error_message`.
- **Client reference:** called `client_tracking_data` on `/metadata` and `client_data` on `/faxes/sent` and webhooks. `/faxes/sent` can filter on `client_reference_id`, which `find_sent` uses.
- **Lists:** `{first_record, last_record, faxes: [...]}`. Paging is through the `pagination-offset`/`pagination-limit` request headers, with 100 as the maximum page size.
- **Image:** JSON `{fax_id, file_name, image: <base64>}`, requested with `desired_format=PDF|TIFF`. Downloading marks the fax as read.
- **Webhooks:**
  - **Signature:** `X-HMAC-Signature` is a hex SHA256 HMAC of the raw body. Enrolment in HMAC goes through the eFax account team, and the secret is ours to choose.
  - **Replies:** answer 200 with JSON within 10 s. eFax retries only 5xx responses, 5 times over about 20 minutes.
  - **Payload:** never contains the image. We ignore `resource_url` and fetch the image by `fax_id` ourselves.
- **`transaction-id`** is for tracing only, **not** an idempotency key, which is why a failed send is never blindly retried.
- **Rate limits:** a 429 means too many calls, but eFax doesn't document the actual numbers.

## When this should become a service instead

Consider it at three or more consuming apps, a consumer that isn't Python, or one fax number shared across apps that needs central inbound routing.
Until then, a package keeps each client's PHI in its own account and gives us nothing extra to run. The client API was kept small, so it could later sit in front of a service without the apps changing.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q && .venv/bin/ruff check src tests
```

eFax's own reference material (API guide, agreements, security sheets) is confidential to eFax customers and is
deliberately **not** in this repo. Get it from your eFax account team or the
[Enterprise API portal](https://docs.efaxcorporate.com/API/content/index.htm).

## Licence

Proprietary, all rights reserved. Published so Milligram Health's customers can install it; see [LICENSE](LICENSE).
