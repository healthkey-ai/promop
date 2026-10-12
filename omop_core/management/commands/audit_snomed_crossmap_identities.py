"""Read-only preview of standard SNOMED identities hidden by RxNorm proposals."""
import json

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection
from omop_core.data_migrations.snomed_crossmap_v1 import reconcile, summarize


class Command(BaseCommand):
    help = 'Preview unreviewed RxNorm proposals eligible for standard SNOMED self-mapping.'

    def handle(self, **options):
        receipts = reconcile(apps, connection, dry_run=True)
        self.stdout.write(json.dumps(dict(summary=summarize(receipts), rows=receipts), indent=2))
