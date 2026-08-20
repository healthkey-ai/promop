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


# ---------------------------------------------------------------------------
# therapy_ids_provenance (issue: populate provenance during derivation)
# ---------------------------------------------------------------------------

def test_episode_path_records_asserted_provenance_and_release_id():
    """A validated HemOnc episode_source_concept yields origin='asserted', and
    the current published vocabulary release id is stamped."""
    from django.utils import timezone
    from omop_core.models import VocabularyRelease
    rel = VocabularyRelease.objects.create(
        build_timestamp=timezone.now(), status='published', published_at=timezone.now())

    from omop_core.models import ConceptClass
    person = PersonFactory()
    hemonc = _hemonc_vocab()
    _regimen_graph(hemonc)
    # The episode source concept must be a validated HemOnc *Regimen* to count
    # as asserted (standard 'S' + no invalid_reason come from ConceptFactory).
    regimen_class, _ = ConceptClass.objects.get_or_create(
        concept_class_id='Regimen',
        defaults={'concept_class_name': 'Regimen', 'concept_class_concept_id': 0})
    regimen1 = Concept.objects.get(concept_id=REGIMEN_ID)
    regimen1.concept_class = regimen_class
    regimen1.save(update_fields=['concept_class'])
    concepts = {
        'episode': ConceptFactory(concept_name='Treatment Regimen', vocabulary=hemonc),
        'object': ConceptFactory(concept_name='Disease Episode', vocabulary=hemonc),
        'type': ConceptFactory(concept_name='Derived Episode', vocabulary=hemonc),
        'field': ConceptFactory(concept_name='Episode event field'),
    }
    drug1 = DrugExposureFactory(
        person=person, drug_concept=Concept.objects.get(concept_id=COMPONENT_A_ID),
        drug_exposure_start_date=date(2024, 1, 1), drug_exposure_end_date=date(2024, 3, 1))
    _make_episode(person, 1, 1, regimen1, drug1, concepts)

    prov = _get_treatment_data(person)['therapy_ids_provenance']
    assert prov['first_line_therapy_id'] == {
        'value': REGIMEN_ID, 'origin': 'asserted', 'release_id': str(rel.pk)}  # string per API contract

    # A HemOnc source concept that is NOT a Regimen class is reported inferred.
    person2 = PersonFactory(person_id=person.person_id + 1)
    non_regimen = Concept.objects.get(concept_id=COMPONENT_A_ID)  # HemOnc, not Regimen class
    drug2 = DrugExposureFactory(
        person=person2, drug_concept=Concept.objects.get(concept_id=COMPONENT_B_ID),
        drug_exposure_start_date=date(2024, 1, 1), drug_exposure_end_date=date(2024, 3, 1))
    _make_episode(person2, 10, 1, non_regimen, drug2, concepts)
    prov2 = _get_treatment_data(person2)['therapy_ids_provenance']
    assert prov2['first_line_therapy_id']['origin'] == 'inferred'


def test_episode_without_source_concept_is_inferred(monkeypatch):
    """#362 P1 regression: enrichment leaves episode_source_concept UNSET (it derives
    the regimen by name/drugs, not a source assertion). The derivation re-resolves the
    concept_id from drug_source_value and must report 'inferred' — never over-claim
    'asserted' for a derived regimen sitting on an enrichment-built episode."""
    import omop_core.services.lot_regimens as lot_regimens
    from omop_core.services import patient_record_service as prs
    person = PersonFactory()
    hemonc = _hemonc_vocab()
    _regimen_graph(hemonc)
    concepts = {
        'episode': ConceptFactory(concept_name='Treatment Regimen', vocabulary=hemonc),
        'object': ConceptFactory(concept_name='Disease Episode', vocabulary=hemonc),
        'type': ConceptFactory(concept_name='Derived Episode', vocabulary=hemonc),
        'field': ConceptFactory(concept_name='Episode event field'),
    }
    drug = DrugExposureFactory(
        person=person, drug_concept=Concept.objects.get(concept_id=COMPONENT_A_ID),
        drug_source_value='VRD',
        drug_exposure_start_date=date(2024, 1, 1), drug_exposure_end_date=date(2024, 3, 1))
    # regimen=None → episode_source_concept is NULL, exactly as the enrichment fix leaves it.
    _make_episode(person, 1, 1, None, drug, concepts)
    # drug-name path yields nothing; the regimen is only recoverable by name (drug_source_value).
    monkeypatch.setattr(lot_regimens, 'get_regimen_concept_id', lambda _keys: None)
    monkeypatch.setattr(prs, 'get_regimen_concept_id_by_name',
                        lambda sv: REGIMEN_ID if sv == 'VRD' else None)

    entry = _get_treatment_data(person)['therapy_ids_provenance']['first_line_therapy_id']
    assert entry['value'] == REGIMEN_ID
    assert entry['origin'] == 'inferred'      # never 'asserted' for a derived regimen


def test_episode_recovers_regimen_from_object_concept_when_name_unaliased(monkeypatch):
    """#362: enrichment puts the derived regimen in episode_object_concept, not the
    source slot. When the name fallbacks can't recover it (a FHIR regimen with a
    valid id but a display name outside the alias table), the id is still recovered
    from the object slot — 'inferred', never 'asserted'. Guards the data-loss the
    regimen_source_concept=None fix would otherwise cause."""
    import omop_core.services.lot_regimens as lot_regimens
    from omop_core.services import patient_record_service as prs
    from omop_core.models import ConceptClass
    person = PersonFactory()
    hemonc = _hemonc_vocab()
    _regimen_graph(hemonc)
    regimen_class, _ = ConceptClass.objects.get_or_create(
        concept_class_id='Regimen',
        defaults={'concept_class_name': 'Regimen', 'concept_class_concept_id': 0})
    regimen1 = Concept.objects.get(concept_id=REGIMEN_ID)
    regimen1.concept_class = regimen_class
    regimen1.save(update_fields=['concept_class'])
    concepts = {
        'episode': ConceptFactory(concept_name='Treatment Regimen', vocabulary=hemonc),
        'object': regimen1,      # the derived regimen lives in the object slot
        'type': ConceptFactory(concept_name='Derived Episode', vocabulary=hemonc),
        'field': ConceptFactory(concept_name='Episode event field'),
    }
    drug = DrugExposureFactory(
        person=person, drug_concept=Concept.objects.get(concept_id=COMPONENT_A_ID),
        drug_source_value='UNALIASED-NAME',
        drug_exposure_start_date=date(2024, 1, 1), drug_exposure_end_date=date(2024, 3, 1))
    _make_episode(person, 1, 1, None, drug, concepts)      # source concept None
    monkeypatch.setattr(lot_regimens, 'get_regimen_concept_id', lambda _keys: None)
    monkeypatch.setattr(prs, 'get_regimen_concept_id_by_name', lambda _sv: None)

    entry = _get_treatment_data(person)['therapy_ids_provenance']['first_line_therapy_id']
    assert entry['value'] == REGIMEN_ID    # recovered from episode_object_concept
    assert entry['origin'] == 'inferred'


def test_regimen_from_exposures_reports_origin(monkeypatch):
    # de_info tuple: (concept_id, vocabulary_id, name). The no-Episode inference
    # path never asserts — even a HemOnc regimen concept on an exposure is
    # 'inferred' (it may come from a text/synthetic backfill, not a source code).
    from omop_core.services import patient_record_service as prs
    _n, cid, origin = prs._regimen_from_exposures([1], {1: (REGIMEN_ID, 'HemOnc', 'RVD')})
    assert (cid, origin) == (REGIMEN_ID, 'inferred')
    # inferred via drug-name matching
    monkeypatch.setattr(prs, 'get_regimen_concept_id', lambda _keys: REGIMEN_ID)
    _n2, cid2, origin2 = prs._regimen_from_exposures([2], {2: (555, 'RxNorm', 'bortezomib')})
    assert (cid2, origin2) == (REGIMEN_ID, 'inferred')
    # unresolved: nothing matches → no concept_id, no origin
    monkeypatch.setattr(prs, 'get_regimen_concept_id', lambda _keys: None)
    monkeypatch.setattr(prs, 'get_regimen_name', lambda _keys: None)
    _n3, cid3, origin3 = prs._regimen_from_exposures([2], {2: (555, 'RxNorm', 'bortezomib')})
    assert (cid3, origin3) == (None, None)


def test_apply_inferred_lots_records_inferred_provenance(monkeypatch):
    from omop_core.services import patient_record_service as prs
    monkeypatch.setattr(prs, 'get_regimen_concept_id', lambda _keys: REGIMEN_ID)
    person = PersonFactory()
    drug = DrugExposureFactory(
        person=person, drug_concept=ConceptFactory(concept_name='bortezomib'),
        drug_exposure_start_date=date(2024, 1, 1), drug_exposure_end_date=date(2024, 1, 21))
    lots = [SimpleNamespace(lot_number=1, exposure_ids=[drug.drug_exposure_id],
                            start=date(2024, 1, 1), end=date(2024, 1, 21))]
    data = {}
    prs._apply_inferred_lots(data, lots)
    assert data['first_line_therapy_id'] == REGIMEN_ID
    entry = data['therapy_ids_provenance']['first_line_therapy_id']
    assert entry['origin'] == 'inferred'
    assert entry['release_id'] is None  # no published release in this test


def test_is_asserted_regimen_guards():
    """asserted requires HemOnc + Regimen class + standard + non-invalid; any
    miss (wrong vocab/class, non-standard, retired, or None) is not asserted."""
    from types import SimpleNamespace
    from omop_core.services.patient_record_service import _is_asserted_regimen

    def c(**kw):
        d = dict(vocabulary_id='HemOnc', concept_class_id='Regimen',
                 standard_concept='S', invalid_reason=None)
        d.update(kw)
        return SimpleNamespace(**d)

    assert _is_asserted_regimen(c()) is True
    assert _is_asserted_regimen(c(vocabulary_id='RxNorm')) is False
    assert _is_asserted_regimen(c(concept_class_id='Ingredient')) is False
    assert _is_asserted_regimen(c(standard_concept='C')) is False   # non-standard
    assert _is_asserted_regimen(c(invalid_reason='D')) is False      # retired
    assert _is_asserted_regimen(None) is False
