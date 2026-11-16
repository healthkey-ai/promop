"""Gene identity is an OMOP Genomic concept, not a display symbol (#5673)."""
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection
from django.core.management import call_command
from rest_framework.exceptions import ValidationError

from omop_core.models import Concept, FieldConceptMapping, Measurement
from omop_core.services.genomics import save_variant
from omop_core.services.genomics_catalog import gene_concept_id, gene_concepts, markers
from omop_core.services.genomics_features import describe_finding
from tests.factories import (
    ConceptClassFactory, ConceptFactory, DomainFactory, PatientRecordFactory,
    PersonFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db

migration = importlib.import_module('omop_core.migrations.0325_gene_concept_ids')


def _gene_concepts_loaded(*keys):
    VocabularyFactory(vocabulary_id='OMOP Genomic', vocabulary_name='OMOP Genomic')
    DomainFactory(domain_id='Measurement', domain_name='Measurement')
    ConceptClassFactory(concept_class_id='Genetic Variation', concept_class_name='Genetic Variation')
    catalog = {m['key']: m for m in markers()}
    for key in keys:
        marker = catalog[key]
        ConceptFactory(
            concept_id=marker['omop_concept_id'],
            vocabulary__vocabulary_id='OMOP Genomic',
            concept_code=marker['hgnc_id'].split(':')[1],
            concept_name=f"{marker['gene']} gene variant measurement",
            domain__domain_id='Measurement',
            concept_class__concept_class_id='Genetic Variation',
            standard_concept='S',
        )


def _setup(*keys):
    person = PersonFactory()
    PatientRecordFactory(person=person)
    ConceptFactory(vocabulary__vocabulary_id='CDM', concept_code='measurement.measurement_id',
                   concept_name='measurement.measurement_id')
    # The real LOINC parent is a Clinical Observation, not a Measurement, so
    # the generic recipe degrades to concept 0 instead of a wrong domain.
    ConceptFactory(concept_code='81252-9', domain__domain_id='Observation')
    _gene_concepts_loaded(*keys)
    call_command('seed_genomics_catalog', stdout=SimpleNamespace(write=lambda *a, **k: None))
    return person


def test_every_catalog_gene_has_one_hgnc_and_omop_id():
    genes = [m for m in markers() if m['kind'] == 'gene']
    assert len(genes) == 24
    assert all(m.get('hgnc_id', '').startswith('HGNC:') for m in genes)
    assert all(m.get('hgnc_symbol') for m in genes)
    assert all(isinstance(m.get('omop_concept_id'), int) for m in genes)
    assert len({m['omop_concept_id'] for m in genes}) == len(genes)
    assert {m['key']: m['hgnc_id'] for m in genes}['brca1'] == 'HGNC:1100'
    assert {m['key']: m['hgnc_symbol'] for m in genes}['fam46c'] == 'TENT5C'


def test_gene_symbols_and_reviewed_aliases_resolve_to_one_id():
    assert gene_concept_id('BRCA1') == 35950928
    assert gene_concept_id('brca1') == 35950928
    assert gene_concept_id('PALB1') == gene_concept_id('PALB2') == 35949597
    assert gene_concept_id('FAM46C') == gene_concept_id('TENT5C') == 35952051
    assert gene_concept_id('NOT-A-GENE') is None
    assert gene_concept_id(None) is None


def test_findings_expose_gene_concept_id_and_never_a_symbol():
    finding = describe_finding({'gene': 'BRCA1', 'variant': 'c.68_69delAG', 'feature_type': 'Gene'})
    assert finding['gene_concept_id'] == 35950928
    # An abnormality is a distinct finding: it must not satisfy a gene
    # requirement through the gene symbol it happens to mention.
    deletion = describe_finding({'gene': 'TP53', 'variant': 'del(17p)'})
    assert deletion['feature_type'] == 'Chromosome arm/region'
    assert deletion['gene_concept_id'] is None
    # Explicit structure wins even when the text or a gene marker names a gene.
    assert describe_finding({
        'gene': 'TP53', 'genomic_feature': '17p', 'feature_type': 'Chromosome arm/region',
        'finding_category': 'Deletion', 'variant': '17p loss',
    })['gene_concept_id'] is None
    assert describe_finding({
        'gene': 'BCL2', 'feature_type': 'Gene', 'finding_category': 'Gain',
        'variant': 'copy number gain',
    })['gene_concept_id'] is None
    # And unmistakable cytogenetic free text is not a sequence variant.
    assert describe_finding({'gene': 'BCL2', 'variant': 'copy number gain'})['gene_concept_id'] is None
    assert describe_finding({'gene': 'BCL2', 'variant': 'BCL2 gain'})['gene_concept_id'] is None
    # HGVS spelling stays a sequence variant, including splice-site offsets and
    # descriptions that happen to contain structural words.
    for variant in ('c.212+1G>A', 'c.213-2A>G', 'c.68_69delAG (frameshift deletion)',
                    'p.Glu23ValfsTer17', 'BRCA1 loss of function'):
        assert describe_finding({
            'gene': 'BRCA1', 'variant': variant,
            'feature_type': 'Gene', 'finding_category': 'Sequence variant',
        })['gene_concept_id'] == 35950928, variant
    unknown = describe_finding({'gene': 'UNKNOWN1'})
    assert unknown['gene_concept_id'] is None


def test_seeding_uses_the_gene_measurement_concept_and_writes_it():
    person = _setup('brca1', 'tp53')
    mapping = FieldConceptMapping.objects.get(field_name='genomics_brca1')
    assert mapping.concept_id == 35950928
    assert (mapping.vocabulary_id, mapping.concept_code) == ('OMOP Genomic', '1100')
    assert mapping.provenance == 'genomics-catalog v3'

    variant = save_variant(person, {'gene': 'BRCA1', 'variant': 'c.68_69delAG'})
    parent = Measurement.objects.get(pk=variant['id'])
    assert parent.measurement_concept_id == 35950928
    assert parent.measurement_source_concept_id == 35950928
    assert parent.measurement_source_value == 'genomics:brca1'
    assert variant['gene_concept_id'] == 35950928


def test_a_copy_number_finding_never_answers_a_gene_requirement():
    person = _setup('bcl2')
    variant = save_variant(person, {'gene': 'BCL2', 'variant': 'BCL2 gain'})
    # Structural routing (amplification marker) is #5674; the gene-identity
    # contract is that this finding contributes no gene_concept_id.
    assert variant['gene_concept_id'] is None


def test_tent5c_uses_the_fam46c_recipe_and_gene_concept():
    """The HGNC symbol is a reviewed alias, not a separate generic finding."""
    person = _setup('fam46c')
    variant = save_variant(person, {
        'gene': 'TENT5C', 'genomic_feature': 'TENT5C', 'feature_type': 'Gene',
        'variant': 'p.S428PfsTer2',
    })
    assert variant['marker_key'] == 'fam46c'
    assert variant['gene_concept_id'] == 35952051
    parent = Measurement.objects.get(pk=variant['id'])
    assert parent.measurement_concept_id == 35952051
    assert parent.measurement_source_value == 'genomics:fam46c'


def test_a_portable_gene_recipe_resolves_by_code_when_the_fk_is_unset():
    """The shipped fixture can be seeded before Athena is loaded."""
    person = _setup('brca1')
    FieldConceptMapping.objects.filter(field_name='genomics_brca1').update(concept=None)
    variant = save_variant(person, {'gene': 'BRCA1', 'variant': 'c.68_69delAG'})
    parent = Measurement.objects.get(pk=variant['id'])
    assert parent.measurement_concept_id == 35950928


def test_seeding_leaves_the_recipe_unmapped_when_the_gene_concept_is_not_loaded():
    person = _setup('brca1')
    FieldConceptMapping.objects.filter(field_name='genomics_brca1').delete()
    Concept.objects.filter(concept_id=35950928).delete()
    call_command('seed_genomics_catalog', stdout=SimpleNamespace(write=lambda *a, **k: None))
    mapping = FieldConceptMapping.objects.get(field_name='genomics_brca1')
    assert (mapping.vocabulary_id, mapping.concept_code) == ('LOINC', '81252-9')
    assert mapping.concept.domain_id == 'Observation'
    # Identity still comes from the reviewed catalog and is reported when the
    # vocabulary does not carry it; no display symbol is substituted.
    variant = save_variant(person, {'gene': 'BRCA1', 'variant': 'c.68_69delAG'})
    assert variant['gene_concept_id'] == 35950928
    parent = Measurement.objects.get(pk=variant['id'])
    assert parent.measurement_concept_id == 0


def test_an_echoed_gene_concept_id_is_accepted_but_never_rewritten():
    person = _setup('brca1', 'tp53')
    variant = save_variant(person, {'gene': 'BRCA1', 'variant': 'c.68_69delAG'})
    edited = save_variant(person, {**variant, 'variant': 'c.68_69delAG (repeat)'}, variant['id'])
    assert edited['gene_concept_id'] == 35950928
    with pytest.raises(ValidationError):
        save_variant(person, {**variant, 'gene_concept_id': 999}, variant['id'])
    with pytest.raises(ValidationError):
        save_variant(person, {**variant, 'gene_concept_id': None}, variant['id'])
    with pytest.raises(ValidationError):
        save_variant(person, {'gene': 'TP53', 'variant': 'p.R175H', 'gene_concept_id': 35950928})


def test_backfill_moves_only_the_untouched_recipe_and_restores_it_exactly():
    from patient_portal.models import Identity

    _gene_concepts_loaded('brca1', 'tp53', 'esr1', 'ccnd1', 'palb2', 'fam46c', 'kras')
    brca1 = Concept.objects.get(pk=35950928)
    tp53 = Concept.objects.get(pk=35948926)
    esr1 = Concept.objects.get(pk=35948983)
    ccnd1 = Concept.objects.get(pk=35963336)
    palb2 = Concept.objects.get(pk=35949597)
    kras = Concept.objects.get(pk=35948791)
    # Legacy seeding resolved the LOINC parent when Athena was already loaded.
    loinc = ConceptFactory(concept_code='81252-9', domain__domain_id='Observation')
    reviewer = Identity.objects.create_user(email='reviewer@example.test', password='pw')
    editor = SimpleNamespace(connection=connection)

    def mapping(field_name, concept, **overrides):
        values = dict(
            field_name=field_name, concept=concept, vocabulary_id='LOINC',
            concept_code='81252-9', source_value='genomics:' + field_name.removeprefix('genomics_'),
            omop_table='measurement', value_kind='json', multiple=True,
            type_concept_id=32817, status='approved', provenance='system_generated',
            notes=migration.LEGACY_NOTE,
        )
        values.update(overrides)
        return FieldConceptMapping.objects.create(**values)

    legacy = mapping('genomics_brca1', loinc)
    # 0233 renamed the field but kept the original PALB1 source key.
    legacy_palb2 = mapping('genomics_palb2', loinc, source_value='genomics:palb1')
    curated = mapping('genomics_tp53', tp53, provenance='curator',
                      notes='Curator kept the generic variant row.')
    reviewed = mapping('genomics_esr1', esr1, provenance='approver@example.test',
                       reviewer=reviewer, notes='Reviewed and approved.')
    proposed = mapping('genomics_kras', kras, status='proposed')
    # A curator replaced the concept but kept the legacy code columns.
    different = mapping('genomics_fam46c', brca1)
    runtime_v3 = FieldConceptMapping.objects.create(
        field_name='genomics_ccnd1', concept=ccnd1, vocabulary_id='OMOP Genomic',
        concept_code='1582', source_value='genomics:ccnd1',
        omop_table='measurement', value_kind='json', multiple=True,
        type_concept_id=32817, status='approved', provenance='genomics-catalog v3',
        notes='Genomics catalog v3: gene identity is the reviewed concept.',
    )

    migration.apply_gene_concepts(apps, editor)
    for row in (legacy, legacy_palb2):
        row.refresh_from_db()
        assert row.vocabulary_id == 'OMOP Genomic'
        assert row.provenance == 'genomics-catalog v3'
        assert row.notes.startswith(migration.MIGRATION_NOTE)
    assert (legacy.concept_id, legacy.concept_code) == (35950928, '1100')
    assert (legacy_palb2.concept_id, legacy_palb2.concept_code) == (35949597, '26144')
    assert legacy_palb2.source_value == 'genomics:palb1'
    for row, expected in (
        (curated, (tp53, 'LOINC', '81252-9')),
        (reviewed, (esr1, 'LOINC', '81252-9')),
        (proposed, (kras, 'LOINC', '81252-9')),
        (different, (brca1, 'LOINC', '81252-9')),
        (runtime_v3, (ccnd1, 'OMOP Genomic', '1582')),
    ):
        row.refresh_from_db()
        assert (row.concept_id, row.vocabulary_id, row.concept_code) == (
            expected[0].pk, expected[1], expected[2])
    proposed.refresh_from_db()
    assert proposed.status == 'proposed'
    curated.refresh_from_db()
    assert curated.provenance == 'curator'

    migration.apply_gene_concepts(apps, editor)  # idempotent
    legacy.refresh_from_db()
    assert legacy.concept_id == 35950928

    migration.restore_legacy_recipe(apps, editor)
    for row, original in ((legacy, loinc), (legacy_palb2, loinc)):
        row.refresh_from_db()
        # Exact prior state, not a canonical substitute.
        assert (row.concept_id, row.vocabulary_id, row.concept_code) == (
            original.pk, 'LOINC', '81252-9')
        assert row.provenance == 'system_generated'
        assert row.notes == migration.LEGACY_NOTE
    legacy_palb2.refresh_from_db()
    assert legacy_palb2.source_value == 'genomics:palb1'
    # Rows the forward did not write, including runtime-seeded v3 mappings,
    # must survive the reverse.
    for row, expected in (
        (curated, (tp53, 'LOINC', '81252-9')),
        (reviewed, (esr1, 'LOINC', '81252-9')),
        (proposed, (kras, 'LOINC', '81252-9')),
        (different, (brca1, 'LOINC', '81252-9')),
        (runtime_v3, (ccnd1, 'OMOP Genomic', '1582')),
    ):
        row.refresh_from_db()
        assert (row.concept_id, row.vocabulary_id, row.concept_code) == (
            expected[0].pk, expected[1], expected[2])

    migration.restore_legacy_recipe(apps, editor)  # idempotent
    legacy.refresh_from_db()
    assert legacy.concept_id == loinc.pk


def test_reverse_leaves_a_mapping_a_curator_changed_after_the_forward():
    _gene_concepts_loaded('brca1')
    loinc = ConceptFactory(concept_code='81252-9', domain__domain_id='Observation')
    row = FieldConceptMapping.objects.create(
        field_name='genomics_brca1', concept=loinc, vocabulary_id='LOINC',
        concept_code='81252-9', source_value='genomics:brca1',
        omop_table='measurement', value_kind='json', multiple=True,
        type_concept_id=32817, status='approved', provenance='system_generated',
        notes=migration.LEGACY_NOTE,
    )
    editor = SimpleNamespace(connection=connection)
    migration.apply_gene_concepts(apps, editor)
    FieldConceptMapping.objects.filter(pk=row.pk).update(provenance='curator')
    row.refresh_from_db()
    assert row.vocabulary_id == 'OMOP Genomic'

    migration.restore_legacy_recipe(apps, editor)
    row.refresh_from_db()
    assert (row.vocabulary_id, row.concept_code) == ('OMOP Genomic', '1100')
    assert row.provenance == 'curator'


def test_the_shipped_catalog_fixture_agrees_with_the_backfill():
    data = json.loads((Path(__file__).parents[1] / 'omop_core/data/genomics_catalog_v1.json').read_text())
    shipped = {m['field_name']: m['omop_concept_id'] for m in data['markers'] if m['kind'] == 'gene'}
    assert set(shipped.values()) == set(migration.GENE_CONCEPTS.values())
    for field_name, concept_id in migration.GENE_CONCEPTS.items():
        # The frozen file still spells the retired field genomics_palb1;
        # catalog() rewrites it to genomics_palb2.
        source = 'genomics_palb1' if field_name == 'genomics_palb2' else field_name
        assert shipped[source] == concept_id
    assert data['markers'][5]['field_name'] == 'genomics_palb1'
