import logging

from django.db import DatabaseError, migrations

logger = logging.getLogger(__name__)


def backfill(apps, schema_editor):
    # #1957. Detection is the living code in omop_core.mapping.axis_variants
    # rather than a frozen copy: it reads LOINC axes, canonical units and unit
    # evidence across several modules, and a copy would drift from what ingest
    # flags. A fresh database has no multi-unit codes, so this does nothing
    # there; ingest flags codes as they arrive.
    from omop_core.mapping import axis_variants

    try:
        refreshed = axis_variants.refresh_all()
    except DatabaseError:
        # The living models can be ahead of this schema when a deployment
        # migrates through several releases at once. The flags are curation
        # evidence, so a failure must not block the deploy.
        logger.warning(
            'Axis variant backfill failed; run `manage.py refresh_axis_variants` '
            'after migrating.', exc_info=True)
        return
    logger.info('Axis variant backfill refreshed %s mappings.', refreshed)


class Migration(migrations.Migration):
    # A transaction per batch (refresh_all), not one for the whole backlog:
    # staging has ~40,000 candidate codes, minutes of work.
    atomic = False

    dependencies = [
        ('omop_core', '0299_loinc_axes_and_variants'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
