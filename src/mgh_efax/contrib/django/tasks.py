"""Celery tasks (importing this module requires Celery).

Add the sweep to beat; it is the safety net under every path:

    CELERY_BEAT_SCHEDULE["efax-sweep"] = {"task": "mgh_efax.sweep", "schedule": 60.0}
"""

from __future__ import annotations

from celery import shared_task

from . import services
from .models import InboundFax, OutboundFax


@shared_task(name="mgh_efax.submit_outbound", ignore_result=True)
def submit_outbound(pk: int) -> None:
    fax = OutboundFax.objects.filter(pk=pk).first()
    if fax:
        services.submit(fax)


@shared_task(name="mgh_efax.process_inbound", ignore_result=True)
def process_inbound(pk: int) -> None:
    fax = InboundFax.objects.filter(pk=pk).first()
    if fax:
        services.process_inbound(fax)


@shared_task(name="mgh_efax.sweep", ignore_result=True)
def sweep() -> dict[str, int]:
    return services.sweep()
