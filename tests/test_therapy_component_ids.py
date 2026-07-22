"""Tests for therapy component concept_ids (issues #189/#231).

Covers:
- _expand_component_ids: HemOnc regimen→component graph expansion, exposure-derived
  ids, and 'Maps to'/'Has ingredient' ingredient leveling.
- Episodes derivation path (_get_treatment_data): per-line + aggregate component ids.
- Inferred-LOT derivation path (_apply_inferred_lots): same, without Episodes.
- Model persistence of the new PatientRecord JSONFields.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from omop_core.models import Concept, ConceptRelationship, PatientRecord, Relationship
from omop_oncology.models import Episode, EpisodeEvent
from omop_core.services.patient_record_service import (
    _apply_inferred_lots,
    _expand_component_ids,
    _get_treatment_data,
)
from tests.factories import ConceptFactory, DrugExposureFactory, PersonFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

REGIMEN_ID = 35_800_001
REGIMEN2_ID = 35_800_002
COMPONENT_A_ID = 35_900_001  # HemOnc drug (targeted therapy)
COMPONENT_B_ID = 35_900_002  # HemOnc drug (cytotoxic chemo)
COMPONENT_C_ID = 35_900_003  # HemOnc drug (component of regimen 2)
RXNORM_CD_ID = 1_900_001     # RxNorm clinical drug ('Maps to' target of A)
RXNORM_ING_ID = 1_900_002    # RxNorm ingredient ('Has ingredient' target of CD)


def _link(c1, c2, rel_id):
    """Create a concept_relationship row (plus its Relationship fixture row)."""
    Relationship.objects.get_or_create(
        relationship_id=rel_id,
        defaults=dict(
            relationship_name=rel_id, is_hierarchical=0, defines_ancestry=0,
            reverse_relationship_id='rev', relationship_concept_id=0,
        ),
    )
    return ConceptRelationship.objects.create(
        concept_1_id=c1, concept_2_id=c2, relationship_id=rel_id,
        valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
    )


def _hemonc_vocab():
    return VocabularyFactory(vocabulary_id='HemOnc', vocabulary_name='HemOnc')


def _regimen_graph(hemonc):
    """RVD-like regimen: 2 HemOnc components, A maps to an RxNorm clinical drug
    which has an RxNorm ingredient."""
    ConceptFactory(concept_id=REGIMEN_ID, concept_name='RVD', vocabulary=hemonc)
    ConceptFactory(concept_id=COMPONENT_A_ID, concept_name='bortezomib', vocabulary=hemonc)
    ConceptFactory(concept_id=COMPONENT_B_ID, concept_name='lenalidomide', vocabulary=hemonc)
    ConceptFactory(concept_id=RXNORM_CD_ID, concept_name='bortezomib 1 MG Injection')
    ConceptFactory(concept_id=RXNORM_ING_ID, concept_name='bortezomib (ingredient)')
    _link(REGIMEN_ID, COMPONENT_A_ID, 'Has targeted therapy')
    _link(REGIMEN_ID, COMPONENT_B_ID, 'Has cytotoxic chemo')
    _link(COMPONENT_A_ID, RXNORM_CD_ID, 'Maps to')
    _link(RXNORM_CD_ID, RXNORM_ING_ID, 'Has ingredient')
    return {COMPONENT_A_ID, COMPONENT_B_ID, RXNORM_CD_ID, RXNORM_ING_ID}


# ---------------------------------------------------------------------------
# _expand_component_ids unit tests
# ---------------------------------------------------------------------------

def test_expand_regimen_graph_with_ingredient_leveling():
    expected = _regimen_graph(_hemonc_vocab())
    assert _expand_component_ids([REGIMEN_ID], []) == expected


def test_expand_exposure_derived_with_leveling():
    drug = ConceptFactory(concept_name='dexamethasone')
    ing = ConceptFactory(concept_name='dexamethasone (ingredient)')
    _link(drug.concept_id, ing.concept_id, 'Has ingredient')
    assert _expand_component_ids([], [drug.concept_id]) == {drug.concept_id, ing.concept_id}


def test_expand_unions_regimen_and_exposures():
    expected = _regimen_graph(_hemonc_vocab())
    extra = ConceptFactory(concept_name='dexamethasone')
    assert _expand_component_ids([REGIMEN_ID], [extra.concept_id]) == expected | {extra.concept_id}


def test_expand_empty_inputs():
    assert _expand_component_ids([], []) == set()
    assert _expand_component_ids(None, None) == set()
    assert _expand_component_ids([None], [0]) == set()


def test_expand_unknown_regimen_yields_empty():
    assert _expand_component_ids([99_999_999], []) == set()


# ---------------------------------------------------------------------------
# Episodes derivation path
# ---------------------------------------------------------------------------

def _make_episode(person, episode_id, number, regimen, drug, concepts):
    Episode.objects.create(
        episode_id=episode_id,
        person=person,
        episode_concept=concepts['episode'],
        episode_start_date=drug.drug_exposure_start_date,
        episode_end_date=drug.drug_exposure_end_date,
        episode_number=number,
        episode_object_concept=concepts['object'],
        episode_type_concept=concepts['type'],
        episode_source_concept=regimen,
    )
    EpisodeEvent.objects.create(
        episode_id=episode_id,
        event_id=drug.drug_exposure_id,
        episode_event_field_concept=concepts['field'],
    )


def test_episode_path_populates_per_line_and_aggregate_component_ids():
    person = PersonFactory()
    hemonc = _hemonc_vocab()
    line1_expected = _regimen_graph(hemonc)
    regimen1 = Concept.objects.get(concept_id=REGIMEN_ID)

    # Second-line regimen with a single HemOnc component.
    regimen2 = ConceptFactory(concept_id=REGIMEN2_ID, concept_name='Kd', vocabulary=hemonc)
    comp_c = ConceptFactory(concept_id=COMPONENT_C_ID, concept_name='carfilzomib', vocabulary=hemonc)
    _link(REGIMEN2_ID, COMPONENT_C_ID, 'Has targeted therapy')

    concepts = {
        'episode': ConceptFactory(concept_name='Treatment Regimen', vocabulary=hemonc),
        'object': ConceptFactory(concept_name='Disease Episode', vocabulary=hemonc),
        'type': ConceptFactory(concept_name='Derived Episode', vocabulary=hemonc),
        'field': ConceptFactory(concept_name='Episode event field'),
    }
    comp_a = Concept.objects.get(concept_id=COMPONENT_A_ID)
    drug1 = DrugExposureFactory(
        person=person, drug_concept=comp_a,
        drug_exposure_start_date=date(2024, 1, 1), drug_exposure_end_date=date(2024, 3, 1),
    )
    drug2 = DrugExposureFactory(
        person=person, drug_concept=comp_c,
        drug_exposure_start_date=date(2024, 5, 1), drug_exposure_end_date=date(2024, 7, 1),
    )
    _make_episode(person, 1, 1, regimen1, drug1, concepts)
    _make_episode(person, 2, 2, regimen2, drug2, concepts)

    data = _get_treatment_data(person)

    assert data['first_line_therapy_id'] == REGIMEN_ID
    assert set(data['first_line_component_ids']) == line1_expected
    assert data['second_line_therapy_id'] == REGIMEN2_ID
    assert set(data['second_line_component_ids']) == {COMPONENT_C_ID}
    assert set(data['therapy_component_ids']) == line1_expected | {COMPONENT_C_ID}
    # sorted lists, not sets, on the wire
    assert data['first_line_component_ids'] == sorted(line1_expected)


# ---------------------------------------------------------------------------
# Inferred-LOT derivation path (no Episodes)
# ---------------------------------------------------------------------------

def test_apply_inferred_lots_populates_component_ids():
    person = PersonFactory()
    drug_a = DrugExposureFactory(
        person=person, drug_concept=ConceptFactory(concept_name='bortezomib'),
        drug_exposure_start_date=date(2024, 1, 1), drug_exposure_end_date=date(2024, 1, 21),
    )
    drug_b = DrugExposureFactory(
        person=person, drug_concept=ConceptFactory(concept_name='lenalidomide'),
        drug_exposure_start_date=date(2024, 1, 1), drug_exposure_end_date=date(2024, 1, 21),
    )
    ing = ConceptFactory(concept_name='bortezomib (ingredient)')
    _link(drug_a.drug_concept_id, ing.concept_id, 'Has ingredient')

    lots = [
        SimpleNamespace(
            lot_number=1,
            exposure_ids=[drug_a.drug_exposure_id, drug_b.drug_exposure_id],
            start=date(2024, 1, 1), end=date(2024, 1, 21),
        ),
    ]
    data = {}
    _apply_inferred_lots(data, lots)

    expected = {drug_a.drug_concept_id, drug_b.drug_concept_id, ing.concept_id}
    assert set(data['first_line_component_ids']) == expected
    assert set(data['therapy_component_ids']) == expected
    assert 'later_component_ids' not in data


def test_apply_inferred_lots_later_line_components():
    person = PersonFactory()
    drugs = [
        DrugExposureFactory(
            person=person, drug_concept=ConceptFactory(concept_name=f'drug-{n}'),
            drug_exposure_start_date=date(2024, n, 1), drug_exposure_end_date=date(2024, n, 21),
        )
        for n in (1, 3, 5)
    ]
    lots = [
        SimpleNamespace(lot_number=i + 1, exposure_ids=[d.drug_exposure_id],
                        start=d.drug_exposure_start_date, end=d.drug_exposure_end_date)
        for i, d in enumerate(drugs)
    ]
    data = {}
    _apply_inferred_lots(data, lots)

    assert set(data['first_line_component_ids']) == {drugs[0].drug_concept_id}
    assert set(data['second_line_component_ids']) == {drugs[1].drug_concept_id}
    assert set(data['later_component_ids']) == {drugs[2].drug_concept_id}
    assert set(data['therapy_component_ids']) == {d.drug_concept_id for d in drugs}


# ---------------------------------------------------------------------------
# Model persistence
# ---------------------------------------------------------------------------

def test_component_id_fields_persist_and_default():
    person = PersonFactory()
    record = PatientRecord.objects.create(
        person=person,
        first_line_component_ids=[COMPONENT_A_ID, RXNORM_ING_ID],
        therapy_component_ids=[COMPONENT_A_ID, COMPONENT_B_ID, RXNORM_ING_ID],
    )
    record.refresh_from_db()
    assert record.first_line_component_ids == [COMPONENT_A_ID, RXNORM_ING_ID]
    assert record.therapy_component_ids == [COMPONENT_A_ID, COMPONENT_B_ID, RXNORM_ING_ID]
    assert record.second_line_component_ids == []
    assert record.later_component_ids == []
