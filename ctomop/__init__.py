"""Compatibility entrypoint for deployments using the former project name."""
from promop import celery_app

__all__ = ('celery_app',)
