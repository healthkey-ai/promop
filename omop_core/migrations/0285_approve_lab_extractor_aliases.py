"""Approve unambiguous extractor LOINC aliases for incoming Measurements.

The approved FieldConceptMapping is the destination authority.  Never replace
an existing source-code mapping, including a curator's proposal or rejection.
Concept IDs are the stable Athena IDs in the field-curation fixture; they let
fresh installs seed these rows before the full Athena load completes.
"""
from django.db import migrations


# Only codes which differ from the field's canonical LOINC. The demo-only
# 33944-8/33945-5 codes are intentionally absent: neither is a real LOINC.
ALIASES = {
    '4544-3': ('hematocrit_percent', '20570-8', 3009542),
    '49765-1': ('serum_calcium_mg_dl', '17861-6', 3006906),
    '38483-4': ('serum_creatinine_mg_dl', '2160-0', 3016723),
    '33914-3': ('egfr_ml_min_173m2', '62238-1', 40764999),
    '6299-2': ('bun_mg_dl', '3094-0', 3013682),
    '2947-0': ('sodium_meq_l', '2951-2', 3019550),
    '6298-4': ('potassium_meq_l', '2823-3', 3023103),
    '2339-0': ('glucose_mg_dl', '2345-7', 3004501),
    '51435-6': ('monoclonal_protein_serum', '33358-3', 3046299),
    '32730-5': ('monoclonal_protein_urine', '42482-0', 3034655),
    '36916-5': ('kappa_flc', '104544-2', 1091621),
    '80515-0': ('kappa_flc', '104544-2', 1091621),
    '80516-8': ('lambda_flc', '33944-0', 3047169),
    '48378-4': ('kappa_lambda_ratio', '104546-7', 1091281),
    '80517-6': ('kappa_lambda_ratio', '104546-7', 1091281),
    '11118-7': ('clonal_plasma_cells', '93021-4', 37019751),
}


def approve_aliases(apps, schema_editor):
    Mapping = apps.get_model('omop_core', 'FieldConceptMapping')
    Source = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')
    db = schema_editor.connection.alias
    for code, (field, canonical_code, target_id) in ALIASES.items():
        mapping = Mapping.objects.using(db).filter(field_name=field).first()
        if (mapping is None or mapping.status != 'approved'
                or mapping.vocabulary_id != 'LOINC'
                or mapping.concept_code != canonical_code
                or mapping.omop_table.lower() != 'measurement'
                or mapping.concept_id not in (None, target_id)):
            continue
        # Earlier imports cache direct Athena hits as approved identity rows.
        # Those are machine-generated, not curator decisions, and must be
        # upgraded or this alias would never take effect on populated sites.
        # Any other existing row (including a proposal) is left alone.
        existing = Source.objects.using(db).filter(
            organization_id=None, source_vocabulary_id='LOINC', source_code=code,
        ).first()
        if existing is not None:
            if (existing.origin_system == 'athena-direct'
                    and existing.origin == 'import'
                    and existing.source == 'Athena'
                    and existing.status == 'approved'
                    and existing.reviewer_id is None
                    and existing.target_concept_id == existing.source_concept_id):
                Source.objects.using(db).filter(pk=existing.pk).update(
                    target_concept_id=target_id,
                    destination_vocabulary_id='LOINC',
                    domain_id='Measurement', omop_table='measurement',
                    origin_system='lab-extractor-alias-v1', source='HealthKey',
                    notes=f'Unambiguous extractor alias for approved PatientRecord field {field}.',
                )
            continue
        source_id = Concept.objects.using(db).filter(
            vocabulary_id='LOINC', concept_code=code,
        ).values_list('concept_id', flat=True).first()
        Source.objects.using(db).create(
            organization_id=None, source_vocabulary_id='LOINC', source_code=code,
            source_concept_id=source_id, target_concept_id=target_id,
            domain_id='Measurement', destination_vocabulary_id='LOINC',
            omop_table='measurement', status='approved', origin='import',
            origin_system='lab-extractor-alias-v1', source='HealthKey',
            notes=f'Unambiguous extractor alias for approved PatientRecord field {field}.',
        )


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0284_source_code_context_fields')]
    operations = [migrations.RunPython(approve_aliases, migrations.RunPython.noop)]
