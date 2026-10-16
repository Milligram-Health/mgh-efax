import os
import sys

import django
from django.conf import settings

sys.path.insert(0, os.path.dirname(__file__))

if not settings.configured:
    settings.configure(
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "mgh_efax.contrib.django",
            "tests_app",
        ],
        ROOT_URLCONF="tests_app.urls",
        STORAGES={"default": {"BACKEND": "django.core.files.storage.InMemoryStorage"}},
        USE_TZ=True,
        EFAX={
            "APP_ID": "app",
            "API_KEY": "secret-key",
            "USER_ID": "u1",
            "WEBHOOK_SECRET": "s3cret",
            "AUTO_DISPATCH": False,
        },
    )
    django.setup()
    from django.core.management import call_command

    call_command("migrate", run_syncdb=True, verbosity=0)
