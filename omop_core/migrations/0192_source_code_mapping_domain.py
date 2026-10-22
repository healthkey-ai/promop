"""Domain and source-code concept for SourceCodeConceptMapping.

Separate from 0191 rather than folded into it: 0191 has already been applied
to development databases, and Django will not re-run an applied migration, so
editing it in place would leave every one of them silently short two columns
while reporting the migration state as current.

``domain_id`` is the curator's first choice -- it scopes which source code
systems are plausible and derives ``omop_table``.  ``source_concept`` is the
concept for the *source* code itself where that vocabulary is loaded, kept
distinct from ``target_concept`` so the two stop being conflated.
"""
import logging

import django.db.models.deletion
from django.db import migrations, models

from omop_core.services import source_vocabularies


logger = logging.getLogger(__name__)


def backfill_domain_id(apps, schema_editor):
    """Populate domain_id from the omop_table each row already carries.

    The domain is the curator-facing choice and the table follows from it, so
    rows written before the column existed have the derivation run backwards
    once here.  A row with no table, or one whose table is not a domain
    destination, is left blank rather than guessed at -- a wrong domain would
    re-scope the source code system list and mislead the next curator to open
    it.
    """
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')

    rows = [r for r in Mapping.objects.exclude(omop_table='')]
    changed = []
    for row in rows:
        domain = source_vocabularies.domain_for_table(row.omop_table)
        if not domain:
            logger.warning(
                'source_code_concept_mapping %s has omop_table %r with no '
                'matching domain; domain_id left blank.',
                row.id, row.omop_table,
            )
            continue
        row.domain_id = domain
        changed.append(row)
    if changed:
        Mapping.objects.bulk_update(changed, ['domain_id'])


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0191_source_code_mapping_direction'),
    ]

    operations = [
        migrations.AddField(
            model_name='sourcecodeconceptmapping',
            name='domain_id',
            field=models.CharField(
                blank=True, db_index=True, default='', max_length=20,
                help_text=(
                    'OMOP domain of the fact this code describes (Condition, Drug, '
                    'Measurement, Observation, Procedure). The curator picks it first: '
                    'it scopes which source code systems are plausible and settles '
                    'which clinical table the fact lands in, so omop_table follows '
                    'from it rather than being chosen separately.'
                ),
            ),
        ),
        migrations.AddField(
            model_name='sourcecodeconceptmapping',
            name='source_concept',
            field=models.ForeignKey(
                blank=True, null=True, db_constraint=False,
                on_delete=django.db.models.deletion.DO_NOTHING,
                related_name='source_code_mappings_as_source',
                to='omop_core.concept',
                help_text=(
                    'The OMOP concept for the source code *itself*, when that '
                    'vocabulary is loaded. Distinct from target_concept, which is the '
                    'destination: an ICD-10-CM code has a concept of its own even '
                    'though the fact should carry a different, standard one. Null is '
                    'normal -- most source systems are ones we receive codes in '
                    'without holding their concepts.'
                ),
            ),
        ),
        migrations.RunPython(backfill_domain_id, migrations.RunPython.noop),
    ]
