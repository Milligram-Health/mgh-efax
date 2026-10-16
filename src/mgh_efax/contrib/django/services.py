"""The engine behind the outbox and inbox. Celery tasks and the `efax_sweep` command both call
these; nothing here assumes Celery exists.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from ... import errors
from ...client import EFaxClient
from ...models import DELIVERED, FAILED, CoverPage, Destination, Document, SendRequest
from ...numbers import mask_number, normalize_fax_number
from ...webhook import InboundNotification
from . import account_resolver, check_settings, efax_settings, get_client
from .models import InboundFax, InboundState, OutboundFax, OutboundState
from .signals import inbound_fax_received, outbound_fax_finished

log = logging.getLogger("mgh_efax")

MAX_SEND_ATTEMPTS = 5  # provably-unsent failures (auth, 429, network) before giving up
MAX_INBOUND_ATTEMPTS = 10
SENDING_STALE_AFTER = timedelta(minutes=10)  # a worker died mid-send -> UNCERTAIN
RECONCILE_GIVE_UP_AFTER = timedelta(minutes=15)  # eFax lists a send within seconds
POLL_INTERVAL = timedelta(minutes=1)
BATCH = 50


class _Clients:
    """One client (and so one cached token) per sending number within a sweep.

    With ACCOUNT_RESOLVER set this hands out None, so each row resolves its own account; the
    app's resolver should cache one client per account itself so tokens are reused.
    """

    def __init__(self, client: EFaxClient | None = None):
        self._fixed = client
        self._per_row = client is None and account_resolver() is not None
        self._by_user: dict[str, EFaxClient] = {}

    def __call__(self, user_id: str = "") -> EFaxClient | None:
        if self._fixed:
            return self._fixed
        if self._per_row:
            return None
        if user_id not in self._by_user:
            self._by_user[user_id] = get_client(user_id=user_id or None)
        return self._by_user[user_id]


# ---- outbound -----------------------------------------------------------------------------------


def submit(fax: OutboundFax, *, client: EFaxClient | None = None) -> OutboundFax:
    """Send one QUEUED fax. Claims the row first, so two workers can never both send it."""
    claimed = OutboundFax.objects.filter(pk=fax.pk, state=OutboundState.QUEUED).update(
        state=OutboundState.SENDING, attempts=F("attempts") + 1, updated_at=timezone.now()
    )
    fax.refresh_from_db()
    if not claimed:
        return fax
    conf = efax_settings()
    if not conf.get("SENDING_ENABLED", True):
        log.info("efax sending disabled here; suppressed %s", fax.reference)
        return _finish(fax, OutboundState.SUPPRESSED, error="sending disabled in this environment")
    redirect_to = conf.get("REDIRECT_TO") or ""
    if client is None:
        try:
            client = get_client(user_id=fax.from_user_id or None, row=fax, purpose="send")
        except Exception as exc:  # resolver bug or unreadable credentials: nothing was dialled
            log.warning("efax account lookup failed for %s: %s", fax.reference, type(exc).__name__)
            if fax.attempts >= MAX_SEND_ATTEMPTS:
                return _finish(fax, OutboundState.FAILED, error="gave up: account lookup failed")
            return _set(fax, OutboundState.QUEUED, error=f"account lookup: {type(exc).__name__}")
        if client is None:
            return _finish(fax, OutboundState.SUPPRESSED, error="sending disabled for this account")
    try:
        dial = normalize_fax_number(redirect_to) if redirect_to else fax.to_number
        with fax.document.open("rb") as fh:
            content = fh.read()
        cover = None
        if fax.cover_subject or fax.cover_message or fax.cover_from:
            cover = CoverPage(
                from_name=fax.cover_from or None,
                subject=fax.cover_subject or None,
                message=fax.cover_message or None,
            )
        if redirect_to:
            # Always a cover page on a redirected fax, so whoever picks it up knows it's a test.
            intended = " ".join(x for x in (fax.to_name, mask_number(fax.to_number)) if x)
            cover = CoverPage(
                from_name=fax.cover_from or None,
                subject=f"[TEST] {fax.cover_subject}".strip(),
                message=f"TEST FAX, redirected. Intended for: {intended}."
                + (f"\n\n{fax.cover_message}" if fax.cover_message else ""),
            )
        fax.dialed_number = dial
        ref = client.send_fax(
            SendRequest(
                destinations=[Destination(dial, fax.to_name or None, fax.to_company or None)],
                documents=[Document(content)],
                client_reference_id=str(fax.reference),
                cover_page=cover,
            ),
            transaction_id=str(fax.reference),
        )
    except errors.SendUncertain as exc:
        return _set(fax, OutboundState.UNCERTAIN, error=str(exc))
    except (errors.RequestRejected, ValueError) as exc:
        return _finish(fax, OutboundState.FAILED, error=str(exc), code=getattr(exc, "code", None))
    except (errors.EFaxError, OSError) as exc:
        # Provably not sent (auth, rate limit, refused connection, storage read): retry later.
        if fax.attempts >= MAX_SEND_ATTEMPTS:
            return _finish(fax, OutboundState.FAILED, error=f"gave up: {exc}")
        return _set(fax, OutboundState.QUEUED, error=str(exc))
    fax.efax_fax_id = ref.fax_id
    fax.submitted_at = timezone.now()
    _set(fax, OutboundState.SUBMITTED, error="")
    log.info(
        "efax submitted %s fax_id=%s to=%s%s",
        fax.reference,
        ref.fax_id,
        mask_number(dial),
        " (redirected test fax)" if redirect_to else "",
    )
    return fax


def refresh(fax: OutboundFax, *, client: EFaxClient | None = None) -> OutboundFax:
    """Poll eFax for a SUBMITTED fax's delivery outcome."""
    if fax.state != OutboundState.SUBMITTED or not fax.efax_fax_id:
        return fax
    client = client or get_client(user_id=fax.from_user_id or None, row=fax, purpose="track")
    if client is None:
        log.info("efax refresh: no account for %s; will retry", fax.reference)
        return fax
    meta = client.get_metadata(fax.efax_fax_id)
    return apply_outcome(
        fax, meta.transmission_status, meta.pages, meta.error_code, meta.error_message
    )


def apply_outcome(
    fax: OutboundFax,
    transmission_status: str | None,
    pages: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> OutboundFax:
    """COMPLETE -> DELIVERED, ERROR/CANCELED -> FAILED; anything else keeps it SUBMITTED."""
    if fax.is_finished:
        return fax
    status = (transmission_status or "").upper()
    fax.provider_status = status[:64]
    if pages is not None:
        fax.page_count = pages
    if status in DELIVERED:
        return _finish(fax, OutboundState.DELIVERED, error="")
    if status in FAILED:
        detail = " ".join(x for x in (error_code, error_message) if x) or status
        return _finish(fax, OutboundState.FAILED, error=f"eFax {detail}", code=error_code)
    fax.save(update_fields=["provider_status", "page_count", "updated_at"])
    return fax


def reconcile(fax: OutboundFax, *, client: EFaxClient | None = None) -> OutboundFax:
    """UNCERTAIN -> SUBMITTED if eFax has it; back to QUEUED once it clearly doesn't."""
    if fax.state != OutboundState.UNCERTAIN:
        return fax
    client = client or get_client(user_id=fax.from_user_id or None, row=fax, purpose="track")
    if client is None:
        log.info("efax reconcile: no account for %s; will retry", fax.reference)
        return fax
    found = client.find_sent(str(fax.reference))
    if found:
        fax.efax_fax_id = found.fax_id
        fax.submitted_at = fax.submitted_at or timezone.now()
        _set(fax, OutboundState.SUBMITTED, error="")
        if found.transmission_status:
            apply_outcome(fax, found.transmission_status, found.pages, found.error_code)
    elif timezone.now() - fax.updated_at > RECONCILE_GIVE_UP_AFTER:
        _set(fax, OutboundState.QUEUED, error="reconciled: not found at eFax")
    return fax


def _set(fax: OutboundFax, state: str, *, error: str | None = None) -> OutboundFax:
    fax.state = state
    if error is not None:
        fax.error = error[:500]
    fax.save()
    return fax


def _finish(fax: OutboundFax, state: str, *, error: str, code: str | None = None) -> OutboundFax:
    fax.completed_at = timezone.now()
    fax.error_code = (code or "")[:32]
    _set(fax, state, error=error)
    transaction.on_commit(lambda: outbound_fax_finished.send(sender=OutboundFax, fax=fax))
    return fax


# ---- inbound ------------------------------------------------------------------------------------


def record_inbound(
    fax_id: str,
    *,
    notification_id: str | None = None,
    from_number: str | None = None,
    to_number: str | None = None,
    user_id: str | None = None,
    app_id: str | None = None,
    pages: int | None = None,
    raw: dict[str, Any] | None = None,
) -> tuple[InboundFax, bool]:
    """Idempotent on eFax's fax_id: webhook redeliveries and polling overlaps are no-ops."""
    return InboundFax.objects.get_or_create(
        efax_fax_id=fax_id,
        defaults={
            "notification_id": notification_id or "",
            "from_number": (from_number or "")[:20],
            "to_number": (to_number or "")[:20],
            "user_id": user_id or "",
            "app_id": (app_id or "")[:64],
            "page_count": pages,
            "raw": raw or {},
        },
    )


def record_notification(n: InboundNotification) -> tuple[InboundFax, bool]:
    return record_inbound(
        n.fax_id,
        notification_id=n.notification_id,
        from_number=n.from_number,
        to_number=n.to_number,
        user_id=n.raw.get("user_id"),
        app_id=n.raw.get("app_id"),
        pages=n.pages,
        raw=n.raw,
    )


def process_inbound(fax: InboundFax, *, client: EFaxClient | None = None) -> InboundFax:
    """Download and store the image if needed, then hand the fax to the app's receivers."""
    if fax.state == InboundState.HANDLED:
        return fax
    InboundFax.objects.filter(pk=fax.pk).update(attempts=F("attempts") + 1)
    fax.refresh_from_db()
    if not fax.image:
        client = client or get_client(user_id=fax.user_id or None, row=fax, purpose="inbound")
        if client is None:
            # Kept in the retry path like a receiver failure: a misconfigured account must
            # never lose a received fax.
            fax.handler_error = "no eFax account for this fax yet"
            fax.save(update_fields=["handler_error", "updated_at"])
            return fax
        try:
            content = client.download_image(fax.efax_fax_id)
        except errors.EFaxError as exc:
            fax.handler_error = f"download: {exc}"[:500]
            fax.save(update_fields=["handler_error", "updated_at"])
            return fax
        from django.core.files.base import ContentFile

        fax.image.save(f"{fax.efax_fax_id}.pdf", ContentFile(content), save=False)
        fax.state = InboundState.STORED
        fax.stored_at = timezone.now()
        fax.save()
    try:
        inbound_fax_received.send(sender=InboundFax, fax=fax)
    except Exception as exc:  # a receiver bug must not lose the fax; the sweep retries it
        log.exception("efax inbound receiver failed for fax_id=%s", fax.efax_fax_id)
        fax.state = InboundState.HANDLER_FAILED
        fax.handler_error = f"{type(exc).__name__}: {exc}"[:500]
        fax.save()
        return fax
    fax.state = InboundState.HANDLED
    fax.handled_at = timezone.now()
    fax.handler_error = ""
    fax.save()
    return fax


def poll_received(*, client: EFaxClient | None = None) -> int:
    """For projects without an inbound webhook: pull faxes not yet downloaded.

    Not available with ACCOUNT_RESOLVER: there is no list of accounts to poll.
    """
    check_settings()
    client = client or get_client()
    new = 0
    for meta in client.iter_received(image_downloaded="false"):
        raw = meta.raw
        fax, created = record_inbound(
            meta.fax_id,
            from_number=raw.get("originating_fax_number"),
            app_id=raw.get("app_id"),
            to_number=raw.get("destination_fax_number"),
            pages=meta.pages,
            raw=raw,
        )
        if created:
            new += 1
            process_inbound(fax, client=client)
    return new


# ---- sweep --------------------------------------------------------------------------------------


def sweep(*, client: EFaxClient | None = None) -> dict[str, int]:
    """Move every row one step forward. Safe to run concurrently and as often as you like."""
    check_settings()
    clients = _Clients(client)
    now = timezone.now()
    counts = {"submitted": 0, "stale": 0, "reconciled": 0, "refreshed": 0, "inbound": 0}

    def each(qs, fn, key, user_attr):
        for row in qs[:BATCH]:
            try:
                fn(row, client=clients(getattr(row, user_attr)))
                counts[key] += 1
            except Exception:
                log.exception("efax sweep: %s failed for %s", key, row.pk)

    each(
        OutboundFax.objects.filter(state=OutboundState.QUEUED).order_by("created_at"),
        submit,
        "submitted",
        "from_user_id",
    )
    counts["stale"] = OutboundFax.objects.filter(
        state=OutboundState.SENDING, updated_at__lt=now - SENDING_STALE_AFTER
    ).update(state=OutboundState.UNCERTAIN, updated_at=now)
    each(
        OutboundFax.objects.filter(state=OutboundState.UNCERTAIN),
        reconcile,
        "reconciled",
        "from_user_id",
    )
    each(
        OutboundFax.objects.filter(
            state=OutboundState.SUBMITTED, updated_at__lt=now - POLL_INTERVAL
        ).order_by("updated_at"),
        refresh,
        "refreshed",
        "from_user_id",
    )
    each(
        InboundFax.objects.filter(
            state__in=[InboundState.RECEIVED, InboundState.STORED, InboundState.HANDLER_FAILED],
            attempts__lt=MAX_INBOUND_ATTEMPTS,
        ).order_by("received_at"),
        process_inbound,
        "inbound",
        "user_id",
    )
    if efax_settings().get("POLL_INBOUND"):
        try:
            counts["inbound"] += poll_received(client=clients())
        except Exception:
            log.exception("efax sweep: inbound poll failed")
    return counts
