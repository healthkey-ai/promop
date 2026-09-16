"""Seed current approved storage recipes; preserve subsequent curator decisions.

Uses the effective catalog; historical migrations keep their frozen inputs.
Approval covers storage semantics, not clinical validity of source examples.
"""
from omop_core.services.genomics_catalog import catalog

from django.utils import timezone


def seed(apps, schema_editor):
    using = schema_editor.connection.alias
    Concept = apps.get_model('omop_core', 'Concept')
    Mapping = apps.get_model('omop_core', 'FieldConceptMapping')
    data = catalog()
    zero = Concept.objects.using(using).filter(pk=0).first()
    # No clinical concept is fabricated when Athena is not loaded. The mapping
    # remains approved, with its portable vocabulary/code ready for resolution.
    recipes = [(m['field_name'], '81252-9', 'measurement', 'genomics:' + m['key'], 'json') for m in data['markers']]
    recipes += [('genetic_mutations.' + a['key'], a['code'], a['table'], a['code'], a['value_kind']) for a in data['attributes']]
    for field, code, table, source, kind in recipes:
        # Resolve without domain filter — the concept's own domain is
        # authoritative.  Marker parents stay in Measurement regardless
        # (event linking depends on it).
        standard = Concept.objects.using(using).filter(vocabulary_id='LOINC', concept_code=code,
            standard_concept='S', invalid_reason__isnull=True).first() if not code.startswith('genomics:') else None
        if standard and not field.startswith('genomics_'):
            table = standard.domain_id.lower()
        notes = ('Genomics catalog v1: approved storage mapping per implementation request. '
                 'Source: CancerBot ' + data['source_commit'] + '. '
                 'Unmapped source text uses concept 0; clinical nomenclature requires expert review.')
        if field == 'genomics_palb2':
            notes = ('Genomics catalog v2: reviewed PALB2 naming; ' + data['naming_decision']
                     + '. Storage mapping only; example annotations remain unvalidated.')
        Mapping.objects.using(using).get_or_create(field_name=field, defaults={
            'concept': standard or zero,
            'vocabulary_id': 'LOINC' if not code.startswith('genomics:') else '',
            'concept_code': code if not code.startswith('genomics:') else '',
            'source_value': source, 'omop_table': table, 'value_kind': kind,
            'unit': '%' if kind == 'number' else '', 'type_concept_id': 32817,
            'multiple': kind == 'json', 'status': 'approved', 'reviewed_at': timezone.now(),
            'notes': notes,
        })
