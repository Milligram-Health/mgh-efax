from django.urls import path

from mgh_efax.contrib.django.views import InboundWebhookView

urlpatterns = [path("webhooks/efax/", InboundWebhookView.as_view())]
