"""Django app that ships a standard fax outbox and inbox for the whole project.

Add "mgh_efax.contrib.django" to INSTALLED_APPS and run migrate. Every feature in the project
queues faxes through `mgh_efax.contrib.django.api.send_fax(...)` into the one OutboundFax table,
and every inbound fax lands in InboundFax. Apps react through signals rather than owning tables.

Settings (all read from the host app; the package supplies no credentials):

    EFAX = {
        "APP_ID": ..., "API_KEY": ..., "USER_ID": ...,   # USER_ID = default sending number
        "REGION": "us",                # data-sovereignty region: us (default), ca, global...
        "BASE_URL": None,              # full override of the API host, if ever needed
        "STORAGE": "default",          # STORAGES alias for fax documents (PHI: use encrypted)
        "AUTO_DISPATCH": True,         # queue a Celery send on commit; False = sweep only
        "SENDING_ENABLED": True,       # False: nothing is dialled; rows end "suppressed"
        "REDIRECT_TO": "",             # set: EVERY fax dials this number instead (test envs)
        "POLL_INBOUND": False,         # pull /faxes/received when no webhook is configured
        "WEBHOOK_SECRET": ...,         # only apps that receive webhooks
        "WEBHOOK_SIGNATURE_HEADER": "X-HMAC-Signature",
        "WEBHOOK_ENCODING": "hex",
    }
"""

from __future__ import annotations

from typing import Any

from ...client import DEFAULT_REGION, EFaxClient, EFaxConfig


def efax_settings() -> dict[str, Any]:
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured

    conf = getattr(settings, "EFAX", None)
    if not isinstance(conf, dict):
        raise ImproperlyConfigured("settings.EFAX is not configured")
    return conf


def get_client(*, user_id: str | None = None) -> EFaxClient:
    """A client for the project's eFax account; `user_id` picks a sending number other than the
    default USER_ID."""
    from django.core.exceptions import ImproperlyConfigured

    conf = efax_settings()
    missing = [k for k in ("APP_ID", "API_KEY", "USER_ID") if not conf.get(k)]
    if missing:
        raise ImproperlyConfigured(f"settings.EFAX is missing {', '.join(missing)}")
    return EFaxClient(
        EFaxConfig(
            app_id=conf["APP_ID"],
            api_key=conf["API_KEY"],
            user_id=user_id or conf["USER_ID"],
            region=conf.get("REGION") or DEFAULT_REGION,
            base_url=conf.get("BASE_URL") or None,
            timeout=float(conf.get("TIMEOUT", 30)),
        )
    )
