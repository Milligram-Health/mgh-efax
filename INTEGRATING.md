# Integrating mgh-efax into a Django app

A checklist to work through from top to bottom, written for the engineer or agent doing the integration.
The [README](README.md) explains how the package works; this file is the order to do things in and the
places where you must stop and ask.

**⛔ STOP** marks a question for the product owner (whoever owns the app's product decisions). Don't guess an answer; ask,
record it in the "Decisions" table at the end, and carry on with what doesn't depend on it.

---

## 0. Rules that hold throughout

- **The app's own eFax account only.** Whoever owns the app signs with eFax directly (agreement + BAA).
  Never reuse another organisation's eFax credentials, and never share yours with another app.
- **Credentials live in the app's own secret store** (env vars / Secrets Manager in the app's own AWS account).
  Never commit them, log them or paste them into tickets.
- **Fax documents and images are PHI.** They go only to the storage alias in `EFAX["STORAGE"]`, which must be
  private and encrypted. Don't log fax contents. Don't log full fax numbers; the package's `mask_number()`
  keeps the last four digits.
- **Never resend a fax by hand** because its result was unclear. An `uncertain` row is resolved by the sweep
  (`reconcile`), which checks eFax's sent list first. A duplicate clinical fax is an incident.
- **Don't fork or copy the package.** Pin a tag. If something needs changing, change it in `mgh-efax` and release a
  new version.
- **Region:** US accounts are US-sovereign, which is the default (`REGION: "us"` → `api.securedocex.us`).
  Don't point at `.com`.

## 1. Check the app can take it

- [ ] Python ≥ 3.10 and Django ≥ 4.2. The package relies on the `STORAGES` setting.
- [ ] Celery with beat **or** cron. Either works: Celery beat runs the `mgh_efax.sweep` task; without
  Celery, cron runs `manage.py efax_sweep` every minute.
- [ ] If Celery: the app's Celery instance calls `app.autodiscover_tasks()`, so the tasks in
  `mgh_efax.contrib.django.tasks` are found.
- [ ] A storage backend for PHI exists or can be added, e.g. an S3 bucket in the app's own account with SSE,
  no public access and an alias in `STORAGES`.
- [ ] The app can expose a public HTTPS URL on port **443 or 8443**. eFax won't call other ports. Only
  needed if the app receives faxes or wants delivery webhooks.

If any box fails, **⛔ STOP** and report which one before going further.

## 2. Install the package

The repo is public, so the build needs no credentials. Add a pinned tag to the app's requirements:

```
mgh-efax[django,celery] @ git+https://github.com/Milligram-Health/mgh-efax@v0.2.0
```

(Drop `celery` from the extras if the app has no Celery.) If the build can't reach GitHub at all, vendor the
tagged release into the app as a fallback, with a `VERSION` file. Never vendor a modified copy.

## 3. eFax account (people, not code). Gate for going live, not for coding

These run on eFax's timeline, so start them first. Track them; don't block coding on them.

- [ ] The client has signed the eFax agreement **and BAA** in its own name.
- [ ] eFax has issued an **App ID + API key** per environment (DEV, and PROD later). Each App ID is a fully
  separate environment.
- [ ] Fax numbers are provisioned, or existing numbers are being ported (porting can take weeks), and you have the
  eFax **user-id** for the sending number.
- [ ] DEV has a demo number that can fax itself (used in step 7).
- [ ] If receiving: in the eFax Admin portal (Groups → Advanced Settings → API Settings), enable inbound
  (and optionally outbound) webhooks and enter the URL from step 5.
- [ ] If receiving: ask the eFax account team to **enrol the webhook in HMAC** and set the secret, which we
  choose. The webhook view refuses unsigned calls.

## 4. Wire the package in

```python
# settings
INSTALLED_APPS += ["mgh_efax.contrib.django"]

EFAX = {
    "APP_ID": env("EFAX_APP_ID"),
    "API_KEY": env("EFAX_API_KEY"),
    "USER_ID": env("EFAX_USER_ID"),            # default sending number's eFax user-id
    "REGION": "us",
    "STORAGE": "efax",                         # an alias in STORAGES; private + encrypted
    "AUTO_DISPATCH": True,                     # False if the app has no Celery
    "POLL_INBOUND": False,                     # True only if receiving WITHOUT a webhook
    "WEBHOOK_SECRET": env("EFAX_WEBHOOK_SECRET", default=""),
}

# Celery beat (or cron: `manage.py efax_sweep` every minute)
CELERY_BEAT_SCHEDULE["efax-sweep"] = {"task": "mgh_efax.sweep", "schedule": 60.0}
```

- [ ] **Non-production environments must not fax real recipients.** Set `REDIRECT_TO` to a test fax number
  you own, and `SENDING_ENABLED` to `bool(that number)`, so a blank test number means nothing is sent. Production
  sets `REDIRECT_TO: ""` and `SENDING_ENABLED: True` explicitly, ignoring any test number. This matters most
  when dev and prod share an eFax account.
- [ ] Run `manage.py migrate`. This creates the `mgh_efax_outboundfax` and `mgh_efax_inboundfax` tables. Don't
  create fax tables of your own; link to your records through `source` / `subject`.
- [ ] Add the `EFAX_*` secrets to every environment through the app's usual secret path (not `.env` files in git).
- [ ] Only register the admin pages for users who are cleared to see PHI.

## 5. Webhook (only if receiving faxes or wanting delivery webhooks)

```python
from mgh_efax.contrib.django.views import InboundWebhookView
urlpatterns += [path("webhooks/efax/", InboundWebhookView.as_view())]
```

- [ ] Make sure this path is reachable **without login**. The HMAC is its authentication, so exclude it from
  auth middleware, IP allow-lists, etc. It is already CSRF-exempt.
- [ ] Give the full HTTPS URL to whoever does step 3.
- [ ] If the app only sends faxes, skip this step; the sweep polls delivery status instead.

## 6. App behaviour (this is where the decisions are)

**Outbound:** call `send_fax` wherever the app should fax something:

```python
from mgh_efax.contrib.django.api import send_fax
send_fax(to=number, document=pdf_bytes, source=record, tag="<app>.<thing>",
         to_name=..., cover_subject=...)
```

- [ ] **⛔ STOP** *What triggers a fax?* (Which event/screen, automatic or a button, who's allowed to.)
- [ ] **⛔ STOP** *What document?* PDFs must be flat: no fillable forms, encryption, passwords or PDF/A-3.
  Is there a template, and is a cover page wanted?
- [ ] **⛔ STOP** *Where do recipient fax numbers come from?* (The app's own contacts table? NPPES?) What happens
  when a number is missing or invalid? `send_fax` raises `ValueError` for a bad number.
- [ ] Give each kind of fax a `tag` (e.g. `"referrals.provider_letter"`) and pass the record it's about as `source`.

**Outcomes:** react in a receiver; don't poll the table:

```python
@receiver(outbound_fax_finished)   # fax.state is "delivered" or "failed"; fax.error has eFax's reason
def on_finished(sender, fax, **kw): ...
```

- [ ] **⛔ STOP** *What should users see when a fax fails, and who is told?*

**Inbound:** every received fax is stored, then handed to receivers:

```python
@receiver(inbound_fax_received)    # fax.image is the stored PDF
def on_inbound(sender, fax, **kw): ...   # must be idempotent; raising = retried by the sweep
```

- [ ] **⛔ STOP** *What happens to a received fax?* Who works it, does it need matching to a patient or
  provider, and does the app need an inbox screen to view, assign and close faxes? This is usually the biggest
  piece of the integration; scope it separately.
- [ ] When a fax is matched, set `fax.subject` (the record it's about) and `fax.tag`, then save.

## 7. Prove it works (DEV App ID only)

- [ ] Unit tests in the app for its own receivers and triggers. Mock `get_client` or call `services.*` with a
  fake client; see `tests/` in this repo for patterns.
- [ ] Live, against DEV: `send_fax(to=<DEV demo number>, document=<one-page test PDF with no PHI>)`.
  Within a few minutes the row should go `queued → submitted → delivered`.
- [ ] The same fax arrives inbound on the demo number. With the webhook (or `POLL_INBOUND`) an `InboundFax` row
  should appear, reach `stored`, then `handled`, and your receiver should run once.
- [ ] Post a request to the webhook with a wrong signature and expect **403**.
- [ ] Check the logs: no fax contents, no full fax numbers, no credentials.

## 8. Go live

- [ ] Every ⛔ decision is recorded below and approved.
- [ ] The PROD App ID, numbers and BAA are in place (step 3), and the PROD secrets are set.
- [ ] The sweep is running in PROD (check `manage.py efax_sweep` output or the beat logs).
- [ ] Someone owns watching for `failed` and `uncertain` rows. A row stuck in `uncertain` for more than an
  hour needs a human.

## Decisions (fill in as you go)

| # | Question | Answer | Who / when |
|---|---|---|---|
| 1 | What triggers an outbound fax? | | |
| 2 | Which document / template / cover page? | | |
| 3 | Where do recipient numbers come from? Missing-number handling? | | |
| 4 | Failure handling: what users see, who is told | | |
| 5 | Inbound: who works received faxes; matching; inbox screen? | | |
| 6 | Storage alias / bucket for fax PHI | | |
| 7 | Sending number(s) and eFax user-id(s) | | |
