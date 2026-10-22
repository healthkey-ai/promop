"""Read-only preview of local SNOMED Maps to proposal repair."""
import json
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection
from omop_core.data_migrations.snomed_relationships_v1 import reconcile, summarize


class Command(BaseCommand):
    help = 'Preview SNOMED crossmap repair using local SCCM, concepts and Maps to relationships.'

    def handle(self, **options):
        receipts = reconcile(apps, connection, dry_run=True)
        self.stdout.write(json.dumps(dict(summary=summarize(receipts), rows=receipts), indent=2))
