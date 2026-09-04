"""Celery entrypoint. ``celery -A ctomop worker`` finds the app through this."""

import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ctomop.settings')

app = Celery('ctomop')
# Namespaced so every Celery knob is a CELERY_* Django setting and nothing
# else in settings.py can collide with one.
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
