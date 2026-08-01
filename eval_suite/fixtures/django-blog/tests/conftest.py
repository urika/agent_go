import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "1"

pytest_plugins = []


def pytest_configure():
    django.setup()
