"""The standard outbox and inbox. One table each per project, shared by every feature in it.

A row links back to whatever it is about through `source` (outbound) or `subject` (inbound),
generic relations, so no feature needs its own fax table.
"""

from __future__ import annotations

import uuid

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


def fax_storage():
    """Storage for fax documents and images. Callable so migrations don't freeze the backend."""
    from django.core.files.storage import storages

    from . import efax_settings

    return storages[efax_settings().get("STORAGE", "default")]


def _outbound_path(instance: OutboundFax, filename: str) -> str:
    return f"efax/outbound/{instance.created_at:%Y/%m}/{instance.reference}.pdf"


def _inbound_path(instance: InboundFax, filename: str) -> str:
    return f"efax/inbound/{instance.received_at:%Y/%m}/{instance.efax_fax_id}.pdf"


class OutboundState(models.TextChoices):
    QUEUED = "queued", "Queued"
    SENDING = "sending", "Sending"  # claimed by a worker; POST /faxes in flight
    SUBMITTED = "submitted", "Accepted by eFax"
    DELIVERED = "delivered", "Delivered"
    FAILED = "failed", "Failed"
    # POST /faxes may or may not have been accepted. Resolved by reconcile, never by resending.
    UNCERTAIN = "uncertain", "Uncertain (reconciling)"
    # Never sent because EFAX["SENDING_ENABLED"] is off in this environment. Final: turning
    # sending back on does not release a backlog of old faxes.
    SUPPRESSED = "suppressed", "Not sent (sending disabled here)"


class OutboundFax(models.Model):
    reference = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    state = models.CharField(
        max_length=16, choices=OutboundState.choices, default=OutboundState.QUEUED, db_index=True
    )

    # What it's about: any model row in the project, plus a free-form tag for filtering/routing.
    source_type = models.ForeignKey(
        ContentType, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    source_id = models.CharField(max_length=64, blank=True)
    source = GenericForeignKey("source_type", "source_id")
    tag = models.CharField(max_length=64, blank=True, db_index=True)  # e.g. "referrals.letter"
    metadata = models.JSONField(default=dict, blank=True)

    # Where it goes and how it looks.
    to_number = models.CharField(max_length=20)
    to_name = models.CharField(max_length=100, blank=True)
    to_company = models.CharField(max_length=100, blank=True)
    from_user_id = models.CharField(
        max_length=64, blank=True, help_text="eFax user-id (sending number); blank = default"
    )
    cover_from = models.CharField(max_length=100, blank=True)
    cover_subject = models.CharField(max_length=200, blank=True)
    cover_message = models.TextField(blank=True)
    document = models.FileField(storage=fax_storage, upload_to=_outbound_path, max_length=255)

    # What eFax said.
    efax_fax_id = models.CharField(max_length=64, blank=True, db_index=True)
    # The number actually dialled. Differs from to_number only when EFAX["REDIRECT_TO"] sent
    # it to a test number instead.
    dialed_number = models.CharField(max_length=20, blank=True)
    provider_status = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=32, blank=True)
    error = models.CharField(max_length=500, blank=True)
    page_count = models.PositiveIntegerField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["source_type", "source_id"])]
        verbose_name = "outbound fax"

    def __str__(self) -> str:
        return f"Outbound fax {self.reference} ({self.state})"

    @property
    def is_finished(self) -> bool:
        return self.state in (
            OutboundState.DELIVERED,
            OutboundState.FAILED,
            OutboundState.SUPPRESSED,
        )

    @property
    def was_redirected(self) -> bool:
        return bool(self.dialed_number) and self.dialed_number != self.to_number


class InboundState(models.TextChoices):
    RECEIVED = "received", "Received (image pending)"
    STORED = "stored", "Image stored"
    HANDLED = "handled", "Handled"
    HANDLER_FAILED = "handler_failed", "Handler failed"


class InboundFax(models.Model):
    efax_fax_id = models.CharField(max_length=64, unique=True)
    state = models.CharField(
        max_length=16, choices=InboundState.choices, default=InboundState.RECEIVED, db_index=True
    )
    notification_id = models.CharField(max_length=64, blank=True)
    from_number = models.CharField(max_length=20, blank=True)
    to_number = models.CharField(max_length=20, blank=True, db_index=True)
    user_id = models.CharField(max_length=64, blank=True)
    page_count = models.PositiveIntegerField(null=True, blank=True)
    image = models.FileField(
        storage=fax_storage, upload_to=_inbound_path, max_length=255, blank=True
    )

    # Set by whichever app handles it: what this fax turned out to be about.
    subject_type = models.ForeignKey(
        ContentType, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    subject_id = models.CharField(max_length=64, blank=True)
    subject = GenericForeignKey("subject_type", "subject_id")
    tag = models.CharField(max_length=64, blank=True, db_index=True)

    handler_error = models.CharField(max_length=500, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    raw = models.JSONField(default=dict, blank=True)  # notification/list payload; no image
    received_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    stored_at = models.DateTimeField(null=True, blank=True)
    handled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [models.Index(fields=["subject_type", "subject_id"])]
        verbose_name = "inbound fax"
        verbose_name_plural = "inbound faxes"

    def __str__(self) -> str:
        return f"Inbound fax {self.efax_fax_id} ({self.state})"
