from django.db import models


class Intervention(models.Model):
    """Stands in for any app object a fax is about."""

    title = models.CharField(max_length=50)

    class Meta:
        app_label = "tests_app"
