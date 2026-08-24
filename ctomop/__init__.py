# Imported here so the app exists before autodiscovery, which is what lets
# @shared_task in omop_core/tasks.py bind to it.
from ctomop.celery import app as celery_app

__all__ = ('celery_app',)
