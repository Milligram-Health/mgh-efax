"""The one call every feature uses to fax something.

    from mgh_efax.contrib.django.api import send_fax

    send_fax(
        to=clinic.fax, document=pdf_bytes,
        source=referral, tag="referrals.letter",
        to_name=clinic.name, cover_subject="Referral",
    )

The row is written now and sent by a worker after the surrounding transaction commits. Delivery
is reported through `signals.outbound_fax_finished`.
"""

from __future__ import annotations

import logging
from typing import IO, Any

from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile, File
from django.db import models, transaction

from ...numbers import normalize_fax_number
from . import efax_settings
from .models import OutboundFax

log = logging.getLogger("mgh_efax")


def send_fax(
    *,
    to: str,
    document: bytes | IO[bytes] | File,
    source: models.Model | None = None,
    tag: str = "",
    to_name: str = "",
    to_company: str = "",
    from_user_id: str = "",
    cover_from: str = "",
    cover_subject: str = "",
    cover_message: str = "",
    metadata: dict[str, Any] | None = None,
) -> OutboundFax:
    """Queue a PDF fax. Raises ValueError for a number that isn't a valid fax number."""
    fax = OutboundFax(
        to_number=normalize_fax_number(to),
        to_name=to_name[:100],
        to_company=to_company[:100],
        from_user_id=from_user_id,
        tag=tag,
        cover_from=cover_from[:100],
        cover_subject=cover_subject[:200],
        cover_message=cover_message,
        metadata=metadata or {},
    )
    if source is not None:
        fax.source_type = ContentType.objects.get_for_model(source)
        fax.source_id = str(source.pk)
    content = (
        document
        if isinstance(document, File)
        else (ContentFile(document) if isinstance(document, bytes) else File(document))
    )
    with transaction.atomic():  # never leave a queued row without its document
        fax.save()  # created_at + reference exist before the file path is built
        fax.document.save(f"{fax.reference}.pdf", content, save=True)
    transaction.on_commit(lambda: _dispatch(fax.pk))
    return fax


def faxes_for(obj: models.Model):
    """Every outbound fax sent about `obj`, newest first."""
    return OutboundFax.objects.filter(
        source_type=ContentType.objects.get_for_model(obj), source_id=str(obj.pk)
    )


def _dispatch(pk: int) -> None:
    dispatch("submit_outbound", pk)


def dispatch(task_name: str, pk: int) -> None:
    """Best-effort hand-off to a Celery worker. The row is already committed, so if Celery is
    missing or the broker is down, the next sweep does the work; never fail the caller for it."""
    if not efax_settings().get("AUTO_DISPATCH", True):
        return
    try:
        from . import tasks
    except ImportError:
        return
    try:
        getattr(tasks, task_name).delay(pk)
    except Exception:
        log.warning("efax: could not queue %s for %s; the sweep will pick it up", task_name, pk)
