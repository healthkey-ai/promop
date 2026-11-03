"""Reverse-sync therapy-line concept resolution (CB profile-write → OMOP).

When the CB reverse-sync resolves a therapy slug to an OMOP concept and passes it as
`{prefix}_therapy_concept_id`, `_sync_therapy_line` must stamp it onto both the episode's
object and source concept slots so the derivation can read the regimen id back. Without an
id it falls back to the prior name-only episode (object/source = "no match").
"""
from datetime import date
from types import SimpleNamespace

import pytest

from omop_core.models import Concept
from omop_core.services.mappings import (
    CONCEPT_DRUG_EXPOSURE_FIELD,
    CONCEPT_EHR_TYPE,
    CONCEPT_TREATMENT_REGIMEN,
)
from omop_core.services.omop_write_service import sync_to_omop
from omop_oncology.models import Episode
from tests.factories import ConceptFactory, PersonFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

REGIMEN_CONCEPT_ID = 35806260   # a stand-in HemOnc Regimen concept
REGIMEN_CONCEPT_ID_B = 35806261  # a second, different regimen (for the A→B edit)


def _seed_writer_concepts():
    omop = VocabularyFactory(vocabulary_id='OMOP', vocabulary_name='OMOP')
    ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='0', vocabulary=omop)
    for cid in (CONCEPT_TREATMENT_REGIMEN, CONCEPT_EHR_TYPE, CONCEPT_DRUG_EXPOSURE_FIELD):
        ConceptFactory(concept_id=cid, concept_name=str(cid), concept_code=str(cid), vocabulary=omop)
    # The package writer only needs the Concept to exist (it stamps the id onto the episode). The
    # derivation's HemOnc-Regimen class check is exercised end-to-end in the CB-side profile-write test.
    hemonc = VocabularyFactory(vocabulary_id='HemOnc', vocabulary_name='HemOnc')
    ConceptFactory(concept_id=REGIMEN_CONCEPT_ID_B, concept_name='Lenalidomide & Dexamethasone',
                   concept_code=str(REGIMEN_CONCEPT_ID_B), vocabulary=hemonc)
    return ConceptFactory(
        concept_id=REGIMEN_CONCEPT_ID,
        concept_name='Bortezomib & Dexamethasone',
        concept_code=str(REGIMEN_CONCEPT_ID),
        vocabulary=hemonc,
    )


def _adapter(person, **fields):
    return SimpleNamespace(person=person, **fields)


def test_resolved_concept_id_lands_on_object_and_source_slots():
    _seed_writer_concepts()
    person = PersonFactory()

    pi = _adapter(
        person,
        first_line_therapy='Bortezomib & Dexamethasone',
        first_line_start_date=date(2023, 3, 1),
        first_line_therapy_concept_id=REGIMEN_CONCEPT_ID,
    )
    sync_to_omop(pi, {'first_line_therapy', 'first_line_start_date'}, today=date(2023, 3, 1))

    ep = Episode.objects.get(person=person, episode_number=1)
    assert ep.episode_object_concept_id == REGIMEN_CONCEPT_ID
    assert ep.episode_source_concept_id == REGIMEN_CONCEPT_ID
    assert ep.episode_source_value == 'Bortezomib & Dexamethasone'


def test_editing_a_resolved_line_to_a_different_regimen_updates_both_concepts():
    """The A→B edit: a line already resolved to regimen A, changed to a different resolved regimen B, must
    move BOTH object AND source concept to B — the derivation reads the source slot first, so leaving it on
    A would keep reporting the old regimen. This is the codex/fresh-review P2 the overwrite flag fixes."""
    _seed_writer_concepts()
    person = PersonFactory()

    sync_to_omop(
        _adapter(person, first_line_therapy='Bortezomib & Dexamethasone',
                 first_line_therapy_concept_id=REGIMEN_CONCEPT_ID),
        {'first_line_therapy'}, today=date(2023, 3, 1),
    )
    ep = Episode.objects.get(person=person, episode_number=1)
    assert ep.episode_source_concept_id == REGIMEN_CONCEPT_ID

    # Correct the line to a different resolved regimen.
    sync_to_omop(
        _adapter(person, first_line_therapy='Lenalidomide & Dexamethasone',
                 first_line_therapy_concept_id=REGIMEN_CONCEPT_ID_B),
        {'first_line_therapy'}, today=date(2023, 3, 1),
    )
    ep.refresh_from_db()
    assert ep.episode_object_concept_id == REGIMEN_CONCEPT_ID_B
    assert ep.episode_source_concept_id == REGIMEN_CONCEPT_ID_B   # source moved too, not stale on A
    assert ep.episode_source_value == 'Lenalidomide & Dexamethasone'


def test_unmapped_re_send_never_clears_an_existing_source_concept():
    """The whole-line re-send carries a null concept for every unmapped slug. An unrelated edit (e.g. an
    outcome-only PATCH) that re-sends an unmapped therapy name must NOT wipe a concept already asserted on
    the episode (e.g. from FHIR import) — overwrite is 'replace with another resolved concept', never clear."""
    _seed_writer_concepts()
    person = PersonFactory()

    sync_to_omop(
        _adapter(person, first_line_therapy='Bortezomib & Dexamethasone',
                 first_line_therapy_concept_id=REGIMEN_CONCEPT_ID),
        {'first_line_therapy'}, today=date(2023, 3, 1),
    )
    # A later edit that re-sends the line with NO resolved concept (unmapped slug) + an outcome change.
    sync_to_omop(
        _adapter(person, first_line_therapy='Bortezomib & Dexamethasone',
                 first_line_therapy_concept_id=None, first_line_outcome='Complete Response'),
        {'first_line_outcome'}, today=date(2023, 3, 1),
    )
    ep = Episode.objects.get(person=person, episode_number=1)
    assert ep.episode_source_concept_id == REGIMEN_CONCEPT_ID   # preserved, not cleared
    assert ep.episode_object_concept_id == REGIMEN_CONCEPT_ID


def test_absent_concept_id_keeps_the_no_match_fallback():
    _seed_writer_concepts()
    person = PersonFactory()

    pi = _adapter(
        person,
        first_line_therapy='Some unmapped regimen',
        first_line_start_date=date(2023, 3, 1),
        first_line_therapy_concept_id=None,  # slug had a null omop_concept_id
    )
    sync_to_omop(pi, {'first_line_therapy', 'first_line_start_date'}, today=date(2023, 3, 1))

    ep = Episode.objects.get(person=person, episode_number=1)
    assert ep.episode_object_concept_id == 0        # "no matching concept" sentinel
    assert ep.episode_source_concept_id is None     # nothing asserted
    assert ep.episode_source_value == 'Some unmapped regimen'


def test_later_edit_upgrades_an_existing_episode_from_no_match_to_resolved():
    """A name-only episode written before the crosswalk was seeded gets its concept
    backfilled when a later edit carries the resolved id (upsert, not append)."""
    _seed_writer_concepts()
    person = PersonFactory()

    # First write: no concept id yet (object stays the 0 sentinel).
    sync_to_omop(
        _adapter(person, first_line_therapy='Bortezomib & Dexamethasone',
                 first_line_start_date=date(2023, 3, 1), first_line_therapy_concept_id=None),
        {'first_line_therapy'}, today=date(2023, 3, 1),
    )
    assert Episode.objects.get(person=person, episode_number=1).episode_object_concept_id == 0

    # Second write: same line, now resolved.
    sync_to_omop(
        _adapter(person, first_line_therapy='Bortezomib & Dexamethasone',
                 first_line_start_date=date(2023, 3, 1), first_line_therapy_concept_id=REGIMEN_CONCEPT_ID),
        {'first_line_therapy'}, today=date(2023, 3, 1),
    )
    ep = Episode.objects.get(person=person, episode_number=1)
    assert ep.episode_object_concept_id == REGIMEN_CONCEPT_ID
    assert ep.episode_source_concept_id == REGIMEN_CONCEPT_ID
