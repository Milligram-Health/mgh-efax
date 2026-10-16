"""Read-only admin. Fax rows are written by the engine, not by hand; the document/image links
open PHI, so register this only where admin users are already cleared for it."""

from django.contrib import admin

from .models import InboundFax, OutboundFax


class _ReadOnly(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(OutboundFax)
class OutboundFaxAdmin(_ReadOnly):
    list_display = ("reference", "state", "tag", "to_number", "efax_fax_id", "created_at")
    list_filter = ("state", "tag")
    search_fields = ("reference", "efax_fax_id", "source_id")


@admin.register(InboundFax)
class InboundFaxAdmin(_ReadOnly):
    list_display = ("efax_fax_id", "state", "tag", "from_number", "to_number", "received_at")
    list_filter = ("state", "tag")
    search_fields = ("efax_fax_id", "subject_id")
