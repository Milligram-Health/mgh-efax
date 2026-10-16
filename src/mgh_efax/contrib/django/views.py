"""Webhook endpoint for both inbound faxes and outbound delivery status. Route it in the app:

    path("webhooks/efax/", InboundWebhookView.as_view())

It verifies the HMAC, writes the row and answers. The image download and the app's receivers run
in a worker afterwards: eFax wants an answer within 10 s and retries only on 5xx.
"""

from __future__ import annotations

import json
import logging
import uuid

from django.db import transaction
from django.http import HttpRequest, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from ...errors import WebhookSignatureError
from ...webhook import parse_notification, verify_signature
from . import efax_settings, services, webhook_secret_resolver
from .models import OutboundFax

log = logging.getLogger("mgh_efax")


@method_decorator(csrf_exempt, name="dispatch")
class InboundWebhookView(View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest):
        conf = efax_settings()
        secret = conf.get("WEBHOOK_SECRET", "")
        resolver = webhook_secret_resolver()
        if resolver is not None:
            # One secret per account: the payload is parsed ONLY to choose whose secret to
            # check. Nothing in it is acted on until the HMAC over the raw body verifies.
            try:
                payload = json.loads(request.body)
            except ValueError:
                log.warning("efax webhook rejected: unparseable body")
                return HttpResponseForbidden()
            if not isinstance(payload, dict):
                return HttpResponseForbidden()
            try:
                secret = resolver(payload) or ""
            except Exception as exc:
                log.warning("efax webhook rejected: secret lookup failed (%s)", type(exc).__name__)
                return HttpResponseForbidden()
        try:
            verify_signature(
                request.body,
                request.headers.get(conf.get("WEBHOOK_SIGNATURE_HEADER", "X-HMAC-Signature")),
                secret,
                algorithm=conf.get("WEBHOOK_ALGORITHM", "sha256"),
                encoding=conf.get("WEBHOOK_ENCODING", "hex"),
            )
        except WebhookSignatureError as exc:
            log.warning("efax webhook rejected: %s", exc)
            return HttpResponseForbidden()
        try:
            n = parse_notification(request.body)
        except ValueError:
            log.warning("efax webhook: unparseable notification")
            return HttpResponseBadRequest()

        if n.is_inbound:
            # If this write fails Django answers 500 and eFax redelivers; nothing is lost.
            fax, created = services.record_notification(n)
            if created:
                transaction.on_commit(lambda: _dispatch_inbound(fax.pk))
        else:
            out = OutboundFax.objects.filter(efax_fax_id=n.fax_id).first()
            if out is None and n.client_reference_id:
                # Status can beat our own SUBMITTED write (or follow an UNCERTAIN send).
                out = _by_reference(n.client_reference_id)
            if out:
                if not out.efax_fax_id:
                    out.efax_fax_id = n.fax_id
                services.apply_outcome(out, n.status, n.pages, None, n.error)
        return JsonResponse({"received": True})


def _by_reference(value: str) -> OutboundFax | None:
    try:
        return OutboundFax.objects.filter(reference=uuid.UUID(value)).first()
    except ValueError:
        return None  # not one of ours


def _dispatch_inbound(pk: int) -> None:
    from .api import dispatch

    dispatch("process_inbound", pk)
