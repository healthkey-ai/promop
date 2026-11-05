"""Migration 0329 and the code inventory of the demo generators (#0329 audit).

The generators, ``concept_fixtures`` and the bulk importer may carry only codes
the loaded vocabularies contain, under the ids those vocabularies use, and a
HealthKey concept where no vocabulary says the thing. These tests pin the
retired spellings out and exercise the migration's recode on the shapes of row
it meets.
"""
import importlib
import io
import re
import tokenize
from datetime import date
from pathlib import Path

import pytest

from omop_core.models import (
    Concept, FieldConceptMapping, Measurement, Observation, SourceCodeConceptMapping,
)
from tests.factories import (
    ConceptFactory, MeasurementFactory, ObservationFactory, PersonFactory, VocabularyFactory,
)
from tests.migration_utils import run_data_migration

pytestmark = pytest.mark.django_db

MIGRATION = '0329_retire_invented_generator_codes'
mig = importlib.import_module(f'omop_core.migrations.{MIGRATION}')

REPO = Path(__file__).resolve().parents[1]
GENERATOR_FILES = [
    REPO / 'omop_core/management/commands/_mm_generator.py',
    REPO / 'omop_core/management/commands/_fl_generator.py',
    REPO / 'omop_core/management/commands/generate_fhir_bundle.py',
    REPO / 'omop_core/management/commands/generate_synthea_bc.py',
    REPO / 'omop_core/management/commands/enrich_breast_cancer_omop_data.py',
    REPO / 'omop_core/management/commands/bulk_import_fhir_bundle.py',
    REPO / 'omop_core/concept_fixtures.py',
    REPO / 'omop_core/services/patient_record_service.py',
    REPO / 'omop_core/services/mappings.py',
    REPO / 'omop_core/services/provenance_registry.py',
    REPO / 'omop_core/data/field_curation_v1.json',
    REPO / 'patient_portal/api/views.py',
    REPO / 'sample_fhir_complete.json',
]

# Every spelling the audit retired, in the files that used to emit or read it.
RETIRED = set(mig.RECODE) | {'48346-3', '33944-8', '33945-5', '9001003'}


def _run():
    run_data_migration(MIGRATION, 'forwards')


def _loinc(code, name='x', concept_id=None, **extra):
    kwargs = dict(vocabulary_id='LOINC', concept_code=code, concept_name=name, **extra)
    if concept_id is not None:
        kwargs['concept_id'] = concept_id
    return ConceptFactory(**kwargs)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def _quoted_codes(text):
    return set(re.findall(r"""['"]([0-9A-Za-z:_-]+-\d|\d{5,18}|hko:[a-z0-9-]+)['"]""", text))


@pytest.mark.parametrize('path', GENERATOR_FILES, ids=lambda p: p.name)
def test_no_retired_code_is_emitted_or_read(path):
    """A retired spelling may survive only in a comment that says it was retired."""
    text = path.read_text()
    if path.suffix == '.py':
        # Drop comments with the tokenizer: a '#' inside a string literal
        # ('Leukocytes [#/volume]') is not a comment.
        text = ''.join(
            tok.string if tok.type != tokenize.COMMENT else ''
            for tok in tokenize.generate_tokens(io.StringIO(text).readline)
        )
    found = _quoted_codes(text) & RETIRED
    # The migration module itself is the one place the old spellings belong.
    assert not found, f'{path.name} still carries retired code(s) {sorted(found)}'


def test_fixture_seeds_no_real_code_under_a_promop_id():
    """A standard-vocabulary row mirrors Athena: its id is never promop's own."""
    from omop_core.concept_fixtures import _CONCEPTS, LOCAL_CONCEPT_ID_MIN
    offenders = [
        (r['concept_id'], r['vocabulary_id'], r['concept_code']) for r in _CONCEPTS
        if not r['vocabulary_id'].startswith('HK-')
        and (9_000_000 <= r['concept_id'] < 10_000_000 or r['concept_id'] >= LOCAL_CONCEPT_ID_MIN)
    ]
    assert offenders == []


def test_minted_concepts_are_local_and_unique():
    ids = [cid for cid, _code, _name in mig._CONCEPTS]
    codes = [code for _cid, code, _name in mig._CONCEPTS]
    assert len(set(ids)) == len(ids) and len(set(codes)) == len(codes)
    assert all(cid >= mig.LOCAL_CONCEPT_ID_MIN for cid in ids)
    assert all(code.startswith('hko:') for code in codes)


# ---------------------------------------------------------------------------
# Mints
# ---------------------------------------------------------------------------

def test_mints_every_hk_observation_concept_once():
    _run()
    _run()
    for concept_id, code, name in mig._CONCEPTS:
        rows = Concept.objects.filter(vocabulary=VocabularyFactory(vocabulary_id='HK-Observation'), concept_code=code)
        assert rows.count() == 1
        concept = rows.get()
        assert (concept.concept_id, concept.concept_name) == (concept_id, name)
        assert concept.source == 'HealthKey' and concept.standard_concept is None
        assert concept.domain_id == 'Observation'


def test_mint_moves_off_an_id_a_curator_already_took():
    taken_id, code, _name = mig._CONCEPTS[0]
    ConceptFactory(concept_id=taken_id, concept_code='something-else')
    _run()
    minted = Concept.objects.get(vocabulary=VocabularyFactory(vocabulary_id='HK-Observation'), concept_code=code)
    assert minted.concept_id != taken_id
    assert minted.concept_id >= mig.LOCAL_CONCEPT_ID_MIN


# ---------------------------------------------------------------------------
# Recode
# ---------------------------------------------------------------------------

def test_invented_code_rows_move_to_the_real_code_and_concept():
    person = PersonFactory()
    fake = _loinc('32730-5', 'Protein [electrophoresis] in Urine', concept_id=9001003, source='HealthKey')
    real = _loinc('42482-0', 'Protein.monoclonal [Mass/time] in 24 hour Urine', concept_id=3034655)
    row = MeasurementFactory(
        person=person, measurement_concept=fake, measurement_source_concept=fake,
        measurement_source_value='32730-5', value_as_number=50,
    )
    alias = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='LOINC', source_code='32730-5', target_concept=real, status='approved',
    )

    _run()

    row.refresh_from_db()
    assert row.measurement_source_value == '42482-0'
    assert row.measurement_concept_id == real.pk
    assert row.measurement_source_concept_id == real.pk
    assert not SourceCodeConceptMapping.objects.filter(pk=alias.pk).exists()
    assert not Concept.objects.filter(pk=fake.pk).exists()


def test_invented_code_row_without_the_real_concept_keeps_its_fk():
    """Athena not loaded: the code routes, the concept waits, the fake stays."""
    person = PersonFactory()
    fake = _loinc('24646-7', 'Bone lesion XR study', concept_id=2100000012)
    row = ObservationFactory(person=person, observation_concept=fake, observation_source_value='24646-7',
                             value_as_number=1)
    _run()
    row.refresh_from_db()
    assert row.observation_source_value == 'hko:bone-lesions'
    # hko:bone-lesions is minted by the migration itself, so the FK does move.
    assert row.observation_concept.concept_code == 'hko:bone-lesions'
    assert not Concept.objects.filter(pk=fake.pk).exists()


def test_misidentified_real_code_moves_only_the_generators_rows():
    person = PersonFactory()
    toxo = _loinc('89599-5', 'Toxoplasma gondii [Presence] in CSF', concept_id=36305296)
    demo = ObservationFactory(person=person, observation_concept=toxo, observation_source_value='89599-5',
                              value_as_number=1)
    genuine = ObservationFactory(person=person, observation_concept=toxo, observation_source_value='89599-5',
                                 value_as_string='Not detected')
    _run()
    demo.refresh_from_db()
    genuine.refresh_from_db()
    assert demo.observation_source_value == 'hko:meets-crab-criteria'
    assert demo.observation_concept.concept_code == 'hko:meets-crab-criteria'
    assert genuine.observation_source_value == '89599-5'
    assert genuine.observation_concept_id == toxo.pk
    assert Concept.objects.filter(pk=toxo.pk).exists()


def test_isotype_guard_keeps_a_stool_result():
    person = PersonFactory()
    stool = _loinc('57905-2', 'Hemoglobin.gastrointestinal.lower [Presence] in Stool', concept_id=40761014)
    typed = ObservationFactory(person=person, observation_concept=stool, observation_source_value='57905-2',
                               value_as_string='IgG')
    result = ObservationFactory(person=person, observation_concept=stool, observation_source_value='57905-2',
                                value_as_string='Positive')
    _run()
    typed.refresh_from_db()
    result.refresh_from_db()
    assert typed.observation_source_value == '415109007'
    assert result.observation_source_value == '57905-2'


def test_closed_value_guards_keep_a_genuine_result():
    person = PersonFactory()
    orientation = _loinc('76690-7', 'Sexual orientation', concept_id=46234789)
    fungus = _loinc('42804-5', 'Fungus identified in Isolate', concept_id=3033396)
    demo_meno = ObservationFactory(person=person, observation_concept=orientation,
                                   observation_source_value='76690-7', value_as_string='Postmenopausal')
    real_meno = ObservationFactory(person=person, observation_concept=orientation,
                                   observation_source_value='76690-7', value_as_string='Heterosexual')
    demo_intent = MeasurementFactory(person=person, measurement_concept=fungus,
                                     measurement_source_value='42804-5', value_as_string='ADJUVANT')
    real_intent = MeasurementFactory(person=person, measurement_concept=fungus,
                                     measurement_source_value='42804-5', value_as_string='Candida albicans')
    _run()
    for row in (demo_meno, real_meno, demo_intent, real_intent):
        row.refresh_from_db()
    assert demo_meno.observation_source_value == 'hko:menopausal-status'
    assert real_meno.observation_source_value == '76690-7'
    assert demo_intent.measurement_source_value == '104803-2'
    assert real_intent.measurement_source_value == '42804-5'


def test_invented_code_row_on_another_concept_is_repointed():
    """A row the uploader attached to a name-matched concept still follows its code."""
    person = PersonFactory()
    other = _loinc('99999-9', 'Some lab', concept_id=8_000_001)
    real = _loinc('44940-5', 'Alcoholic drinks per week - Reported', concept_id=3043872)
    row = MeasurementFactory(person=person, measurement_concept=other,
                             measurement_source_value='11286-7', value_as_number=4)
    _run()
    row.refresh_from_db()
    assert (row.measurement_source_value, row.measurement_concept_id) == ('44940-5', real.pk)


def test_recode_is_idempotent():
    person = PersonFactory()
    row = MeasurementFactory(person=person, measurement_source_value='11286-7', value_as_number=3)
    _run()
    _run()
    row.refresh_from_db()
    assert row.measurement_source_value == '44940-5'
    assert Measurement.objects.filter(measurement_source_value='44940-5').count() == 1


# ---------------------------------------------------------------------------
# Mapping rows
# ---------------------------------------------------------------------------

def test_field_mapping_moves_when_it_still_points_at_the_retired_code():
    fake = _loinc('11286-7', 'Drinks per week', concept_id=2029606278, source='HealthKey')
    real = _loinc('44940-5', 'Alcoholic drinks per week - Reported', concept_id=3043872)
    row = FieldConceptMapping.objects.create(
        field_name='drinks_per_week', concept=fake, vocabulary_id='LOINC', concept_code='11286-7',
        source_value='11286-7', omop_table='measurement', value_kind='number', status='approved',
        notes='Seeded by migration 0160.',
    )
    _run()
    row.refresh_from_db()
    assert (row.concept_id, row.vocabulary_id, row.concept_code, row.source_value) == (
        real.pk, 'LOINC', '44940-5', '44940-5')
    assert 'migration 0329' in row.notes
    assert not Concept.objects.filter(pk=fake.pk).exists()


def test_recurated_field_mapping_is_left_alone():
    other = ConceptFactory(vocabulary=VocabularyFactory(vocabulary_id='SNOMED'), concept_code='999999', concept_name='Curator choice')
    row = FieldConceptMapping.objects.create(
        field_name='consent_capability', concept=other, vocabulary_id='SNOMED', concept_code='999999',
        omop_table='Observation', status='approved',
    )
    _run()
    row.refresh_from_db()
    assert row.concept_id == other.pk and row.concept_code == '999999'


def test_rows_the_editor_projected_follow_the_moved_mapping():
    """An approved mapping's own rows keep deriving after the repoint."""
    person = PersonFactory()
    wrong = ConceptFactory(vocabulary=VocabularyFactory(vocabulary_id='SNOMED'), concept_code='405193005',
                           concept_name='Caregiver wellbeing status')
    FieldConceptMapping.objects.create(
        field_name='consent_capability', concept=wrong, vocabulary_id='SNOMED', concept_code='405193005',
        omop_table='observation', value_kind='boolean', status='approved',
    )
    mine = ObservationFactory(person=person, observation_concept=wrong, observation_source_value='405193005',
                              value_as_number=1)
    other_field = ObservationFactory(person=person, observation_source_value='405193005', value_as_number=1)
    _run()
    mine.refresh_from_db()
    other_field.refresh_from_db()
    assert mine.observation_source_value == 'hko:consent-capability'
    assert mine.observation_concept.concept_code == 'hko:consent-capability'
    assert other_field.observation_source_value == '405193005'


def test_bone_imaging_result_loses_its_invented_concept():
    fake = _loinc('24646-7', 'Bone lesion XR study', concept_id=2100000012)
    row = FieldConceptMapping.objects.create(
        field_name='bone_imaging_result', concept=fake, vocabulary_id='LOINC', concept_code='24646-7',
        omop_table='Observation', status='proposed', notes='Auto-proposed from field suggestion.',
    )
    _run()
    row.refresh_from_db()
    assert row.concept_id is None and row.vocabulary_id == '' and row.concept_code == ''
    assert '0329' in row.notes
    assert not Concept.objects.filter(pk=fake.pk).exists()


def test_queue_rows_for_invented_codes_go_but_real_codes_stay():
    target = ConceptFactory(vocabulary=VocabularyFactory(vocabulary_id='SNOMED'), concept_code='734005006', concept_name='Telephone number')
    gone = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='', source_code='21908-9-riss', target_concept=target, status='proposed',
    )
    kept = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='LOINC', source_code='73985-4', target_concept=target, status='approved',
    )
    _run()
    assert not SourceCodeConceptMapping.objects.filter(pk=gone.pk).exists()
    assert SourceCodeConceptMapping.objects.filter(pk=kept.pk).exists()


def test_vocabulary_metadata_on_an_invented_concept_goes_with_it():
    """DO_NOTHING FKs (concept_relationship) would otherwise fail the commit."""
    from omop_core.models import ConceptRelationship, Relationship
    fake = _loinc('32730-5', 'Protein [electrophoresis] in Urine', concept_id=9001003)
    for rel, reverse in (('Maps to', 'Mapped from'), ('Mapped from', 'Maps to')):
        relationship, _ = Relationship.objects.get_or_create(
            relationship_id=rel, defaults=dict(relationship_name=rel, is_hierarchical=0,
                                               defines_ancestry=0, reverse_relationship_id=reverse,
                                               relationship_concept_id=0),
        )
        ConceptRelationship.objects.create(
            concept_1=fake, concept_2=fake, relationship=relationship,
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
    _run()
    assert not Concept.objects.filter(pk=fake.pk).exists()
    assert not ConceptRelationship.objects.filter(concept_1_id=fake.pk).exists()


def test_invented_concept_without_its_replacement_is_detached_and_deleted():
    """Athena not loaded: rows move to concept 0 so the fake can go."""
    person = PersonFactory()
    ConceptFactory(concept_id=0, concept_code='No matching concept', concept_name='No matching concept')
    fake = _loinc('11286-7', 'Drinks per week', concept_id=2029606278)
    row = MeasurementFactory(person=person, measurement_concept=fake, measurement_source_concept=fake,
                             measurement_source_value='11286-7', value_as_number=2)
    _run()
    row.refresh_from_db()
    assert row.measurement_source_value == '44940-5'
    assert row.measurement_concept_id == 0 and row.measurement_source_concept_id is None
    assert not Concept.objects.filter(pk=fake.pk).exists()


def test_percent_guard_moves_the_generators_pd_l1_rows():
    person = PersonFactory()
    erbb2 = _loinc('96893-3', 'ERBB2 gene duplication in Tumor by FISH', concept_id=99_000_001)
    row = MeasurementFactory(person=person, measurement_concept=erbb2,
                             measurement_source_value='96893-3', value_as_number=45)
    _run()
    row.refresh_from_db()
    assert row.measurement_source_value == '105303-2'


def test_reviewed_real_code_mapping_is_not_overruled():
    real = _loinc('42759-1', 'B lymphocytes [#/volume] in Bone marrow', concept_id=3031240)
    row = FieldConceptMapping.objects.create(
        field_name='clonal_bone_marrow_b_lymphocytes', concept=real, vocabulary_id='LOINC',
        concept_code='42759-1', omop_table='measurement', value_kind='number', status='approved',
    )
    _run()
    row.refresh_from_db()
    assert row.concept_code == '42759-1'


def test_still_referenced_invented_concept_is_left_protected():
    """A table this migration does not recode keeps the concept alive."""
    from omop_core.models import ConditionOccurrence
    person = PersonFactory()
    fake = _loinc('47082-2', 'Plasma cells/100 cells in Bone marrow', concept_id=2100000014)
    ConditionOccurrence.objects.create(
        condition_occurrence_id=1, person=person, condition_concept=fake,
        condition_start_date=date(2024, 1, 1), condition_type_concept=fake,
    )
    _run()
    assert Concept.objects.filter(pk=fake.pk).exists()
