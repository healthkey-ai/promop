from importlib import import_module
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from omop_core.signals import suppress_patient_record_refresh


class Command(BaseCommand):
    help = 'Seed missing approved genomics mappings from the versioned catalog; preserve curator decisions.'

    @transaction.atomic
    def handle(self, **options):
        with suppress_patient_record_refresh(), connection.schema_editor() as editor:
            import_module('omop_core.services.genomics_seeding').seed(apps, editor)
            # Components added after the frozen v1 catalog.
            import_module('omop_core.migrations.0226_seed_genomics_status_component').seed_status(apps, editor)
            import_module('omop_core.migrations.0227_seed_genomics_v2_components').seed_v2_components(apps, editor)
            import_module('omop_core.migrations.0231_genomics_variant_name_recipe').promote_variant_name(apps, editor)
            import_module('omop_core.migrations.0252_genomic_feature_recipes').seed_feature_recipes(apps, editor)
        self.stdout.write(self.style.SUCCESS('Genomics catalog mappings seeded. Curator decisions preserved; exact untouched variant-name seeds promoted to v3.'))
