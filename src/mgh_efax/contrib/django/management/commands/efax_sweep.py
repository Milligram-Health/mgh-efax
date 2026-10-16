"""Move every outbound/inbound fax one step forward. For projects without Celery beat, run it
from cron every minute; with Celery, beat runs the `mgh_efax.sweep` task instead."""

from django.core.management.base import BaseCommand

from ...services import sweep


class Command(BaseCommand):
    help = "Send queued faxes, poll delivery, reconcile uncertain sends, process inbound faxes."

    def handle(self, *args, **options):
        counts = sweep()
        self.stdout.write(", ".join(f"{k}={v}" for k, v in counts.items()))
