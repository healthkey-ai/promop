"""Deploy domain-correct, diagnosis-scoped narrative mappings for PHR D03 (#1857).

LOINC 48767-8 is Measurement, whereas 85847-2 is Observation. Do not write
both to Observation. Vocabulary/code selectors survive deployments that load
Athena later; runtime writers require the standard concept in the right domain.
No patient facts are changed and existing source keys remain readable.
"""
from django.db import migrations
from django.utils import timezone

MAPPINGS = (
    ('condition.note', '48767-8', 'Measurement', 'measurement', 'phr:condition-note'),
    ('condition.care_team', '85847-2', 'Observation', 'observation', 'phr:condition-care-team'),
)


def forwards(apps, schema_editor):
    using = schema_editor.connection.alias
    Concept = apps.get_model('omop_core', 'Concept')
    Mapping = apps.get_model('omop_core', 'FieldConceptMapping')
    for field, code, domain, table, source in MAPPINGS:
        mapping = Mapping.objects.using(using).filter(field_name=field).first()
        # Preserve an explicit curator approval of a nonzero concept. The
        # original system-generated/unmapped seeds are the rows being repaired.
        if (mapping and mapping.status == 'approved' and mapping.concept_id
                and mapping.provenance != 'system_generated'):
            continue
        concept = Concept.objects.using(using).filter(
            vocabulary_id='LOINC', concept_code=code, domain_id=domain,
            standard_concept='S', invalid_reason__isnull=True,
        ).first()
        Mapping.objects.using(using).update_or_create(field_name=field, defaults={
            'concept': concept, 'vocabulary_id': 'LOINC', 'concept_code': code,
            'omop_table': table, 'source_value': source, 'value_kind': 'string',
            'type_concept_id': 32817, 'unit': '', 'multiple': False, 'status': 'approved',
            'provenance': 'system_generated', 'reviewed_at': timezone.now(),
            'notes': 'PHR D03 (#1857): diagnosis-scoped narrative; typed CDM condition event linkage.',
        })


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0297_genetic_test_specimen_and_notes')]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
