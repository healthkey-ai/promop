"""Seed current approved storage recipes; preserve subsequent curator decisions.

Uses the effective catalog; historical migrations keep their frozen inputs.
Approval covers storage semantics, not clinical validity of source examples.
"""
from omop_core.services.genomics_catalog import catalog
from omop_core.services.field_mapping_provenance import genomics_catalog_provenance

from django.utils import timezone


def gene_concept(Concept, concept_id, using='default'):
    """The reviewed OMOP Genomic gene concept, or None when it is not loaded.

    A gene is identified by a standard Measurement concept whose concept_code
    is the HGNC id (cancerbot #5673). Never substitute a display symbol or a
    generic panel for a missing gene concept: leave the recipe unmapped and let
    the audit report it.
    """
    concept = Concept.objects.using(using).filter(pk=concept_id).first() if concept_id else None
    if (concept is None
            or concept.vocabulary_id != 'OMOP Genomic'
            or concept.domain_id.lower() != 'measurement'
            or concept.standard_concept != 'S'
            or concept.invalid_reason is not None):
        return None
    return concept


def seed(apps, schema_editor):
    using = schema_editor.connection.alias
    Concept = apps.get_model('omop_core', 'Concept')
    Mapping = apps.get_model('omop_core', 'FieldConceptMapping')
    data = catalog()
    zero = Concept.objects.using(using).filter(pk=0).first()
    markers = {m['field_name']: m for m in data['markers']}
    # No clinical concept is fabricated when Athena is not loaded. The mapping
    # remains approved, with its portable vocabulary/code ready for resolution.
    recipes = [(m['field_name'], '81252-9', 'measurement', 'genomics:' + m['key'], 'json') for m in data['markers']]
    recipes += [('genetic_mutations.' + a['key'], a['code'], a['table'], a['code'], a['value_kind']) for a in data['attributes']]
    for field, code, table, source, kind in recipes:
        marker = markers.get(field)
        reviewed = gene_concept(Concept, marker.get('omop_concept_id'), using) if marker else None
        if reviewed is not None:
            standard, vocabulary_id, concept_code = reviewed, 'OMOP Genomic', str(reviewed.concept_code)
        else:
            # Resolve without domain filter — the concept's own domain is
            # authoritative.  Marker parents stay in Measurement regardless
            # (event linking depends on it).
            standard = Concept.objects.using(using).filter(vocabulary_id='LOINC', concept_code=code,
                standard_concept='S', invalid_reason__isnull=True).first() if not code.startswith('genomics:') else None
            if standard and not field.startswith('genomics_'):
                table = standard.domain_id.lower()
            vocabulary_id = 'LOINC' if not code.startswith('genomics:') else ''
            concept_code = code if not code.startswith('genomics:') else ''
        notes = ('Genomics catalog v1: approved storage mapping per implementation request. '
                 'Source: CancerBot ' + data['source_commit'] + '. '
                 'Unmapped source text uses concept 0; clinical nomenclature requires expert review.')
        if field == 'genomics_palb2':
            notes = ('Genomics catalog v2: reviewed PALB2 naming; ' + data['naming_decision']
                     + '. Storage mapping only; example annotations remain unvalidated.')
        if reviewed is not None:
            notes = ('Genomics catalog v3: gene identity is the OMOP Genomic concept whose '
                     'concept_code is the HGNC id (cancerbot #5673).')
        Mapping.objects.using(using).get_or_create(field_name=field, defaults={
            'concept': standard or zero,
            'vocabulary_id': vocabulary_id,
            'concept_code': concept_code,
            'source_value': source, 'omop_table': table, 'value_kind': kind,
            'unit': '%' if kind == 'number' else '', 'type_concept_id': 32817,
            'multiple': kind == 'json', 'status': 'approved', 'reviewed_at': timezone.now(),
            'provenance': genomics_catalog_provenance(data['version']),
            'notes': notes,
        })
