import logging

from django.db import DatabaseError, migrations

logger = logging.getLogger(__name__)


def backfill(apps, schema_editor):
    # #1965. unit_code now reads lab spellings ("gm/dL", "Grams/Deciliter",
    # "GDL") that 0300 left unrecognised, so stored variants regroup and codes
    # those spellings make multi-property gain a flag. Same refresh as 0300,
    # with the same reasons for using the living code and not blocking a
    # deploy on a database error.
    from omop_core.mapping import axis_variants

    try:
        refreshed = axis_variants.refresh_all()
    except DatabaseError:
        logger.warning(
            'Axis variant backfill failed; run `manage.py refresh_axis_variants` '
            'after migrating.', exc_info=True)
        return
    logger.info('Axis variant backfill refreshed %s mappings.', refreshed)


class Migration(migrations.Migration):
    # A transaction per batch (refresh_all), as in 0300.
    atomic = False

    dependencies = [
        ('omop_core', '0300_backfill_axis_variants'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
