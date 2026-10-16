"""How apps react to faxes without owning fax tables.

    from django.dispatch import receiver
    from mgh_efax.contrib.django.signals import inbound_fax_received, outbound_fax_finished

    @receiver(inbound_fax_received)
    def route(sender, fax, **kwargs):      # fax: InboundFax with .image stored
        ...                                # attach to a queue; set fax.subject / fax.tag if useful

    @receiver(outbound_fax_finished)
    def done(sender, fax, **kwargs):       # fax: OutboundFax, DELIVERED or FAILED
        if fax.tag == "referrals.letter": ...

Receivers run in a worker, not in the webhook request. One that raises marks an inbound fax
HANDLER_FAILED and the sweep retries it, so receivers must be idempotent.
"""

from django.dispatch import Signal

inbound_fax_received = Signal()
outbound_fax_finished = Signal()
