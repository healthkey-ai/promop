"""Read-only preview of the SNOMED identifier cleanup migration."""
import json

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection

from omop_core.data_migrations.snomed_oid_v1 import reconcile, summarize


class Command(BaseCommand):
    help = 'Preview SNOMED OID normalization and duplicate merges without writing.'

    def handle(self, **options):
        receipts = reconcile(apps, connection, dry_run=True)
        self.stdout.write(json.dumps(dict(summary=summarize(receipts), rows=receipts), indent=2))
