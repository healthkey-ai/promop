"""Tests for therapy-class ("type") concept_ids (ADR 0002, Phase 2).

promop pre-expands each therapy line's drug-class ("type") concept_ids so
consumers (EXACT) can match trial type criteria by plain class-concept_id
overlap. The derivation walks HemOnc
'Component --[Is a]--> Component Class' edges transitively from the line's
component concept_ids.

Covers:
- _expand_class_ids: single-hop, transitive sub-class chains, non-class-target
  filtering, cycle safety, empty/miss inputs.
- Episode derivation path (_get_treatment_data_from_episodes): per-line +
  aggregate class ids.
- Inferred-LOT derivation path (_apply_inferred_lots): per-line + aggregate +
  later-line class ids.
- Model persistence of the new PatientRecord JSONFields.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from omop_core.models import (
    Concept, ConceptClass, ConceptRelationship, PatientRecord, Relationship,
)
from omop_oncology.models import Episode, EpisodeEvent
from omop_core.services.patient_record_service import (
    _apply_inferred_lots,
    _expand_class_ids,
    _get_treatment_data,
)
from tests.factories import (
    ConceptFactory, DrugExposureFactory, PersonFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db

# Drug (Component) concept_ids
BORTEZOMIB_ID = 35_802_928
LENALIDOMIDE_ID = 35_803_000
CARFILZOMIB_ID = 35_803_100

# Drug-class (Component Class) concept_ids
PROTEASOME_INHIBITOR_ID = 35_807_295
IMID_ID = 35_807_403
TARGETED_THERAPY_ID = 912_163   # broad class; proteasome inhibitor Is a targeted therapy

# HemOnc regimen concept_ids
REGIMEN_RVD_ID = 35_800_501
REGIMEN_KD_ID = 35_800_502


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


def _hemonc():
    return VocabularyFactory(vocabulary_id='HemOnc', vocabulary_name='HemOnc')


def _component_class(concept_id, name):
    """A HemOnc drug-class concept (concept_class_id='Component Class')."""
    cc, _ = ConceptClass.objects.get_or_create(
        concept_class_id='Component Class',
        defaults=dict(concept_class_name='Component Class', concept_class_concept_id=0),
    )
    return ConceptFactory(concept_id=concept_id, concept_name=name,
                          vocabulary=_hemonc(), concept_class=cc)


def _component(concept_id, name):
    """A HemOnc drug (component) concept."""
    return ConceptFactory(concept_id=concept_id, concept_name=name, vocabulary=_hemonc())


# ---------------------------------------------------------------------------
# _expand_class_ids unit tests
# ---------------------------------------------------------------------------

def test_expand_single_hop_class():
    _component(BORTEZOMIB_ID, 'Bortezomib')
    _component_class(PROTEASOME_INHIBITOR_ID, 'Proteasome inhibitor')
    _link(BORTEZOMIB_ID, PROTEASOME_INHIBITOR_ID, 'Is a')
    assert _expand_class_ids([BORTEZOMIB_ID]) == {PROTEASOME_INHIBITOR_ID}


def test_expand_transitive_subclass_chain():
    """drug --Is a--> narrow class --Is a--> broad class: both are returned."""
    _component(BORTEZOMIB_ID, 'Bortezomib')
    _component_class(PROTEASOME_INHIBITOR_ID, 'Proteasome inhibitor')
    _component_class(TARGETED_THERAPY_ID, 'Targeted therapy')
    _link(BORTEZOMIB_ID, PROTEASOME_INHIBITOR_ID, 'Is a')
    _link(PROTEASOME_INHIBITOR_ID, TARGETED_THERAPY_ID, 'Is a')
    assert _expand_class_ids([BORTEZOMIB_ID]) == {PROTEASOME_INHIBITOR_ID, TARGETED_THERAPY_ID}


def test_expand_ignores_non_class_is_a_targets():
    """An 'Is a' edge to a non-Component-Class concept is not a type."""
    _component(BORTEZOMIB_ID, 'Bortezomib')
    other = _component(BORTEZOMIB_ID + 1, 'Some non-class concept')  # concept_class != Component Class
    _link(BORTEZOMIB_ID, other.concept_id, 'Is a')
    assert _expand_class_ids([BORTEZOMIB_ID]) == set()


def test_expand_multiple_components_union():
    _component(BORTEZOMIB_ID, 'Bortezomib')
    _component(LENALIDOMIDE_ID, 'Lenalidomide')
    _component_class(PROTEASOME_INHIBITOR_ID, 'Proteasome inhibitor')
    _component_class(IMID_ID, 'IMiD')
    _link(BORTEZOMIB_ID, PROTEASOME_INHIBITOR_ID, 'Is a')
    _link(LENALIDOMIDE_ID, IMID_ID, 'Is a')
    assert _expand_class_ids([BORTEZOMIB_ID, LENALIDOMIDE_ID]) == {
        PROTEASOME_INHIBITOR_ID, IMID_ID,
    }


def test_expand_cycle_is_safe():
    """A pathological class↔class cycle must terminate, not loop forever."""
    _component(BORTEZOMIB_ID, 'Bortezomib')
    _component_class(PROTEASOME_INHIBITOR_ID, 'Proteasome inhibitor')
    _component_class(TARGETED_THERAPY_ID, 'Targeted therapy')
    _link(BORTEZOMIB_ID, PROTEASOME_INHIBITOR_ID, 'Is a')
    _link(PROTEASOME_INHIBITOR_ID, TARGETED_THERAPY_ID, 'Is a')
    _link(TARGETED_THERAPY_ID, PROTEASOME_INHIBITOR_ID, 'Is a')  # cycle
    assert _expand_class_ids([BORTEZOMIB_ID]) == {PROTEASOME_INHIBITOR_ID, TARGETED_THERAPY_ID}


def test_expand_depth_cap_truncates_and_warns(caplog):
    """A class chain deeper than _CLASS_MAX_HOPS returns the reachable prefix
    and logs a WARNING (never a silent drop — project convention)."""
    import logging
    from omop_core.services import patient_record_service as svc

    # drug --Is a--> c1 --Is a--> c2 --> ... --> c6  (6 class hops)
    _component(BORTEZOMIB_ID, 'Bortezomib')
    chain = list(range(35_808_001, 35_808_007))  # c1..c6
    for cid in chain:
        _component_class(cid, f'Class {cid}')
    _link(BORTEZOMIB_ID, chain[0], 'Is a')
    for a, b in zip(chain, chain[1:]):
        _link(a, b, 'Is a')

    with caplog.at_level(logging.WARNING, logger=svc.__name__):
        result = _expand_class_ids([BORTEZOMIB_ID])

    # _CLASS_MAX_HOPS=5 reaches c1..c5 but not c6.
    assert result == set(chain[:svc._CLASS_MAX_HOPS])
    assert chain[svc._CLASS_MAX_HOPS] not in result
    assert any('depth cap' in r.message for r in caplog.records)


def test_expand_within_depth_cap_does_not_warn(caplog):
    """A chain that fully resolves before the cap must not emit the warning."""
    import logging
    from omop_core.services import patient_record_service as svc

    _component(BORTEZOMIB_ID, 'Bortezomib')
    _component_class(PROTEASOME_INHIBITOR_ID, 'Proteasome inhibitor')
    _component_class(TARGETED_THERAPY_ID, 'Targeted therapy')
    _link(BORTEZOMIB_ID, PROTEASOME_INHIBITOR_ID, 'Is a')
    _link(PROTEASOME_INHIBITOR_ID, TARGETED_THERAPY_ID, 'Is a')

    with caplog.at_level(logging.WARNING, logger=svc.__name__):
        result = _expand_class_ids([BORTEZOMIB_ID])

    assert result == {PROTEASOME_INHIBITOR_ID, TARGETED_THERAPY_ID}
    assert not any('depth cap' in r.message for r in caplog.records)


def test_expand_empty_and_miss():
    assert _expand_class_ids([]) == set()
    assert _expand_class_ids(None) == set()
    assert _expand_class_ids([0, None]) == set()
    # Known component with no class edge → empty.
    _component(BORTEZOMIB_ID, 'Bortezomib')
    assert _expand_class_ids([BORTEZOMIB_ID]) == set()


# ---------------------------------------------------------------------------
# Episode derivation path
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


def test_episode_path_populates_per_line_and_aggregate_class_ids():
    person = PersonFactory()
    hemonc = _hemonc()

    # 1L RVD: bortezomib (→ proteasome inhibitor → targeted therapy).
    regimen1 = ConceptFactory(concept_id=REGIMEN_RVD_ID, concept_name='RVD', vocabulary=hemonc)
    bort = _component(BORTEZOMIB_ID, 'Bortezomib')
    _component_class(PROTEASOME_INHIBITOR_ID, 'Proteasome inhibitor')
    _component_class(TARGETED_THERAPY_ID, 'Targeted therapy')
    _link(REGIMEN_RVD_ID, BORTEZOMIB_ID, 'Has targeted therapy')
    _link(BORTEZOMIB_ID, PROTEASOME_INHIBITOR_ID, 'Is a')
    _link(PROTEASOME_INHIBITOR_ID, TARGETED_THERAPY_ID, 'Is a')

    # 2L Kd: carfilzomib (→ proteasome inhibitor → targeted therapy).
    regimen2 = ConceptFactory(concept_id=REGIMEN_KD_ID, concept_name='Kd', vocabulary=hemonc)
    carf = _component(CARFILZOMIB_ID, 'Carfilzomib')
    _link(REGIMEN_KD_ID, CARFILZOMIB_ID, 'Has targeted therapy')
    _link(CARFILZOMIB_ID, PROTEASOME_INHIBITOR_ID, 'Is a')

    concepts = {
        'episode': ConceptFactory(concept_name='Treatment Regimen', vocabulary=hemonc),
        'object': ConceptFactory(concept_name='Disease Episode', vocabulary=hemonc),
        'type': ConceptFactory(concept_name='Derived Episode', vocabulary=hemonc),
        'field': ConceptFactory(concept_name='Episode event field'),
    }
    drug1 = DrugExposureFactory(
        person=person, drug_concept=bort,
        drug_exposure_start_date=date(2024, 1, 1), drug_exposure_end_date=date(2024, 3, 1),
    )
    drug2 = DrugExposureFactory(
        person=person, drug_concept=carf,
        drug_exposure_start_date=date(2024, 5, 1), drug_exposure_end_date=date(2024, 7, 1),
    )
    _make_episode(person, 1, 1, regimen1, drug1, concepts)
    _make_episode(person, 2, 2, regimen2, drug2, concepts)

    data = _get_treatment_data(person)

    both = {PROTEASOME_INHIBITOR_ID, TARGETED_THERAPY_ID}
    assert set(data['first_line_component_class_ids']) == both
    assert set(data['second_line_component_class_ids']) == both
    assert set(data['therapy_component_class_ids']) == both
    # sorted lists on the wire, not sets
    assert data['first_line_component_class_ids'] == sorted(both)


# ---------------------------------------------------------------------------
# Inferred-LOT derivation path (no Episodes)
# ---------------------------------------------------------------------------

def test_apply_inferred_lots_populates_class_ids():
    person = PersonFactory()
    bort = _component(BORTEZOMIB_ID, 'Bortezomib')
    lena = _component(LENALIDOMIDE_ID, 'Lenalidomide')
    _component_class(PROTEASOME_INHIBITOR_ID, 'Proteasome inhibitor')
    _component_class(IMID_ID, 'IMiD')
    _link(BORTEZOMIB_ID, PROTEASOME_INHIBITOR_ID, 'Is a')
    _link(LENALIDOMIDE_ID, IMID_ID, 'Is a')

    drug_a = DrugExposureFactory(
        person=person, drug_concept=bort,
        drug_exposure_start_date=date(2024, 1, 1), drug_exposure_end_date=date(2024, 1, 21),
    )
    drug_b = DrugExposureFactory(
        person=person, drug_concept=lena,
        drug_exposure_start_date=date(2024, 1, 1), drug_exposure_end_date=date(2024, 1, 21),
    )
    lots = [
        SimpleNamespace(
            lot_number=1,
            exposure_ids=[drug_a.drug_exposure_id, drug_b.drug_exposure_id],
            start=date(2024, 1, 1), end=date(2024, 1, 21),
        ),
    ]
    data = {}
    _apply_inferred_lots(data, lots)

    expected = {PROTEASOME_INHIBITOR_ID, IMID_ID}
    assert set(data['first_line_component_class_ids']) == expected
    assert set(data['therapy_component_class_ids']) == expected
    assert 'later_component_class_ids' not in data


def test_apply_inferred_lots_later_line_class_ids():
    person = PersonFactory()
    bort = _component(BORTEZOMIB_ID, 'Bortezomib')
    lena = _component(LENALIDOMIDE_ID, 'Lenalidomide')
    carf = _component(CARFILZOMIB_ID, 'Carfilzomib')
    _component_class(PROTEASOME_INHIBITOR_ID, 'Proteasome inhibitor')
    _component_class(IMID_ID, 'IMiD')
    _link(BORTEZOMIB_ID, PROTEASOME_INHIBITOR_ID, 'Is a')
    _link(LENALIDOMIDE_ID, IMID_ID, 'Is a')
    _link(CARFILZOMIB_ID, PROTEASOME_INHIBITOR_ID, 'Is a')

    drugs = []
    for n, comp in ((1, bort), (3, lena), (5, carf)):
        drugs.append(DrugExposureFactory(
            person=person, drug_concept=comp,
            drug_exposure_start_date=date(2024, n, 1), drug_exposure_end_date=date(2024, n, 21),
        ))
    lots = [
        SimpleNamespace(lot_number=i + 1, exposure_ids=[d.drug_exposure_id],
                        start=d.drug_exposure_start_date, end=d.drug_exposure_end_date)
        for i, d in enumerate(drugs)
    ]
    data = {}
    _apply_inferred_lots(data, lots)

    assert set(data['first_line_component_class_ids']) == {PROTEASOME_INHIBITOR_ID}
    assert set(data['second_line_component_class_ids']) == {IMID_ID}
    assert set(data['later_component_class_ids']) == {PROTEASOME_INHIBITOR_ID}
    assert set(data['therapy_component_class_ids']) == {PROTEASOME_INHIBITOR_ID, IMID_ID}


# ---------------------------------------------------------------------------
# Model persistence
# ---------------------------------------------------------------------------

def test_class_id_fields_persist_and_default():
    person = PersonFactory()
    record = PatientRecord.objects.create(
        person=person,
        first_line_component_class_ids=[PROTEASOME_INHIBITOR_ID, TARGETED_THERAPY_ID],
        therapy_component_class_ids=[PROTEASOME_INHIBITOR_ID, IMID_ID, TARGETED_THERAPY_ID],
    )
    record.refresh_from_db()
    assert record.first_line_component_class_ids == [PROTEASOME_INHIBITOR_ID, TARGETED_THERAPY_ID]
    assert record.therapy_component_class_ids == [PROTEASOME_INHIBITOR_ID, IMID_ID, TARGETED_THERAPY_ID]
    assert record.second_line_component_class_ids == []
    assert record.later_component_class_ids == []
