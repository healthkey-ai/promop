"""Withdraw proven question/answer and analyte mix-ups; never rewrite patients."""
import json

from django.db import migrations


# field: (incorrect codes, replacement vocabulary, question code, value kind, unit)
REPAIRS = {
    'ki67_proliferation_index': ({'85319-2', '85337-4'}, 'LOINC', '29593-1', 'number', '%'),
    'test_methodology': ({'85337-4'}, 'LOINC', '85069-3', 'string', ''),
    'oncotype_dx_score': ({'85337-4', 'breast@2876@010'}, 'NAACCR', '3904', 'number', ''),
    'pd_l1_tumor_cells': ({'83052-1'}, 'LOINC', '105304-0', 'number', '%'),
    'pd_l1_ic_percentage': ({'83055-4', '85336-6'}, 'LOINC', '105305-7', 'number', '%'),
    'pd_l1_combined_positive_score': ({'83054-7', 'LA34414-5', '96267-2', '96893-3'}, 'LOINC', '105303-2', 'number', ''),
    'lymph_node_status': ({'92837-4', '21906-3'}, '', '', 'string', ''),
    'bone_only_metastasis_status': ({'44667-4', 'LA4202-3', '21907-1'}, '', '', 'boolean', ''),
    'pd_l1_assay': ({'105302-4', '83052-1'}, '', '', 'string', ''),
}


def forward(apps, schema_editor):
    Mapping = apps.get_model('omop_core', 'FieldConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')
    db = schema_editor.connection.alias
    for field, (wrong, vocab, code, kind, unit) in REPAIRS.items():
        row = Mapping.objects.using(db).filter(field_name=field).select_related('concept').first()
        if row is None or (row.concept_code not in wrong and (not row.concept_id or row.concept.concept_code not in wrong)):
            continue
        old = {key: getattr(row, key) for key in ('concept_id', 'concept_code', 'vocabulary_id', 'status', 'reviewer_id', 'reviewed_at', 'omop_table', 'source_value', 'value_kind', 'unit')}
        concept = Concept.objects.using(db).filter(vocabulary_id=vocab, concept_code=code,
            standard_concept='S', invalid_reason__isnull=True, domain_id='Measurement',
            concept_id__lt=2_000_000_000).exclude(source='HealthKey').first() if code else None
        row.concept_id = concept.pk if concept else None
        row.vocabulary_id, row.concept_code = vocab, code
        row.omop_table, row.source_value = ('measurement' if code else ''), code
        row.value_kind, row.unit = kind, unit
        row.status, row.reviewer_id, row.reviewed_at = 'proposed', None, None
        row.notes += '\n#21/#1227: withdrawn incorrect clinical recipe; no patient facts changed. Replacement is a proposal requiring context review. Prior decision: ' + json.dumps(old, default=str)
        row.save(using=db)
    for field, code in [('tumor_size', '21889-1'), ('hrd_status', '107286-7')]:
        row = Mapping.objects.using(db).filter(field_name=field, concept_code=code, omop_table__iexact='observation').first()
        if row:
            row.notes += '\n#21/#1227: Observation destination withdrawn; this LOINC question has Measurement domain. Prior status: ' + row.status
            row.omop_table, row.status = 'measurement', 'proposed'
            row.reviewer_id, row.reviewed_at = None, None
            row.save(using=db)


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0232_migrate_field_choice_identity')]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
