"""Contract/parity test for the in-process matcher adapter (CB ADR 0001, Slice 2).

De-risks `omop_core.services.patient_matching.MatchingPatient` against the REAL installed
`exact_matching` matcher: it enumerates every `USER_TO_TRIAL_ATTRS_MAPPING` attribute and
exercises the exact access modes the matcher uses (`get_value`, `is_attr_blank` — which reads
`__class__._meta.get_field`), plus the therapy concept-id methods and the pre-existing-
conditions related-manager shape. A presence-only check is insufficient; this asserts the
matcher can actually read every field off a wrapped `PatientRecord` without raising.

`exact_matching` is not a PROMOP runtime dependency, so the whole module is skipped when it is
not installed. Install it (the version CancerBot pins) to run this locally / in the convergence
CI:
    pip install "exact-matching @ git+https://github.com/healthkey-ai/exact@<sha>#subdirectory=matcher_app"
"""
import pytest

pytest.importorskip("exact_matching")

import exact_matching.user_to_trial_attrs_mapper  # noqa: F401  (loads the circular pair in order)
from exact_matching.patient_info.patient_info_attributes import PatientInfoAttributes
from exact_matching.patient_info.configs import USER_TO_TRIAL_ATTRS_MAPPING

from omop_core.models import PatientRecord
from omop_core.services.patient_matching import (
    MatchingPatient,
    _therapy_release_id,
    resolved_patient_for_matching,
)


def _record(**overrides):
    base = dict(
        disease="multiple myeloma",
        prior_therapy="Two lines",
        no_pre_existing_conditions=False,
        first_line_therapy="vrd",
        first_line_therapy_id=35806260,
        first_line_therapy_type_ids=[42904, 42905],
        therapy_component_ids=[1336825, 19026972, 1518254],
        therapy_type_ids=[42904, 42905],
        therapy_ids_provenance={
            "first_line_therapy_id": {"value": 35806260, "origin": "asserted", "release_id": "4"}
        },
        preexisting_conditions=["diabetes"],
    )
    base.update(overrides)
    return PatientRecord(**base)


def test_matcher_can_read_every_mapped_attribute():
    """The matcher must read every USER_TO_TRIAL_ATTRS_MAPPING attr off the adapter without
    raising — both get_value and is_attr_blank (which hits __class__._meta.get_field)."""
    pia = PatientInfoAttributes(MatchingPatient(_record()))
    failures = []
    for attr in USER_TO_TRIAL_ATTRS_MAPPING:
        try:
            pia.is_attr_blank(attr)
            pia.get_value(attr)
        except Exception as exc:  # noqa: BLE001 - the point is to catch any read failure
            failures.append((attr, type(exc).__name__, str(exc)[:80]))
    assert not failures, f"matcher could not read: {failures}"


def test_present_vs_absent_fields():
    pia = PatientInfoAttributes(MatchingPatient(_record()))
    assert pia.is_attr_blank("disease") is False           # present column
    # A CB-PatientInfo-only computed field PatientRecord has no column for -> unknown (blank).
    assert pia.is_attr_blank("bulky_disease_criteria") is True


def test_pre_existing_conditions_shape():
    pia = PatientInfoAttributes(MatchingPatient(_record()))
    assert pia.get_value("pre_existing_condition_categories") == ["diabetes"]
    assert pia.is_attr_blank("pre_existing_condition_categories") is False
    # matcher memoizes a private cache on the patient object; the read-only adapter allows it
    assert getattr(pia.patient_info, "_pre_existing_condition_codes_cache", None) == ["diabetes"]


def test_therapy_component_and_type_ids_flow_through():
    pia = PatientInfoAttributes(MatchingPatient(_record()))
    assert pia.get_user_therapy_component_ids() == ["1336825", "19026972", "1518254"]
    assert pia.get_user_therapy_type_ids() == ["42904", "42905"]


def test_therapy_release_id_unanimous_and_fail_closed():
    # unanimous single release across class-contributing lines -> that release
    assert _therapy_release_id(_record()) == "4"
    # a class-contributing line whose regimen did not resolve -> fail closed (None)
    assert _therapy_release_id(_record(first_line_therapy_id=None)) is None
    # class-contributing line with no release_id in provenance -> fail closed
    assert _therapy_release_id(_record(therapy_ids_provenance={})) is None


def test_supportive_therapies_never_iterates_a_raw_string():
    """PatientRecord.supportive_therapies is a legacy free-text TextField; the matcher
    iterates it as {'therapy': code} dicts. A non-empty raw string must NOT reach the
    matcher as a str (it would iterate characters and crash on str.get)."""
    # free text -> normalized to [] (blank: no supportive requirement)
    mp = MatchingPatient(_record(supportive_therapies="patient tolerated G-CSF well"))
    pia = PatientInfoAttributes(mp)
    assert mp.supportive_therapies == []
    assert pia.is_attr_blank("supportive_therapies") is True
    assert pia.get_supportive_therapy_codes() == []
    # a stored JSON list of dicts passes through untouched
    mp2 = MatchingPatient(_record(supportive_therapies='[{"therapy": "bisphosphonate"}]'))
    assert MatchingPatient(_record(supportive_therapies=None)).supportive_therapies == []
    assert PatientInfoAttributes(mp2).get_supportive_therapy_codes() == ["bisphosphonate"]
    # a JSON array of PRIMITIVE codes (not dicts) must NOT reach the matcher as strings
    # (it would crash on str.get); such entries are dropped -> blank.
    mp3 = MatchingPatient(_record(supportive_therapies='["bisphosphonate", "G-CSF"]'))
    assert mp3.supportive_therapies == []
    assert PatientInfoAttributes(mp3).get_supportive_therapy_codes() == []
    # mixed list: keep the dict, drop the primitive
    mp4 = MatchingPatient(_record(supportive_therapies=[{"therapy": "zoledronic"}, "loose"]))
    assert PatientInfoAttributes(mp4).get_supportive_therapy_codes() == ["zoledronic"]


def test_therapy_release_id_fails_closed_on_partial_3L_resolution():
    """3L+ smear (#393): every later line carries the aggregate later_therapy_type_ids as its
    class contribution, so ONE unresolved later line leaves the later block uncertified. The
    aggregate later_therapy_ids (resolved subset only) must NOT fail open here."""
    prov = {
        "first_line_therapy_id": {"release_id": "4"},
        "later_therapy_ids": {"release_id": "4"},
    }
    # two later lines, only the first resolved (new per-line shape carries concept_id)
    partial = _record(
        first_line_therapy_id=35806260,
        first_line_therapy_type_ids=[42904],
        later_therapy_ids=[900001],                     # resolved subset (one id)
        later_therapy_type_ids=[43001],                 # aggregate later classes present
        later_therapies=[
            {"therapy": "regA", "concept_id": 900001},
            {"therapy": "regB", "concept_id": None},    # unresolved sibling
        ],
        therapy_type_ids=[42904, 43001],
        therapy_ids_provenance=prov,
    )
    assert _therapy_release_id(partial) is None          # fail-closed, not "4"
    # when BOTH later lines resolve, the block is certified and the release stands
    full = _record(
        first_line_therapy_id=35806260,
        first_line_therapy_type_ids=[42904],
        later_therapy_ids=[900001, 900002],
        later_therapy_type_ids=[43001],
        later_therapies=[
            {"therapy": "regA", "concept_id": 900001},
            {"therapy": "regB", "concept_id": 900002},
        ],
        therapy_type_ids=[42904, 43001],
        therapy_ids_provenance=prov,
    )
    assert _therapy_release_id(full) == "4"


def test_therapy_release_id_fails_closed_on_malformed_provenance():
    """Untrusted persisted JSON: a non-dict provenance (legacy/hand-edited row) must fail
    closed (None), not raise mid-search on `.get`."""
    assert _therapy_release_id(_record(therapy_ids_provenance=[])) is None
    assert _therapy_release_id(_record(therapy_ids_provenance=["garbage"])) is None
    assert _therapy_release_id(_record(therapy_ids_provenance=None)) is None


def test_interim_tristate_no_drugexposure_is_unknown_never_asserted_none():
    """A person with no OMOP DrugExposure leaves therapy columns at defaults (prior_therapy
    None), which the matcher reads as unknown -- NOT the CB 'None' asserted-negative."""
    pia = PatientInfoAttributes(
        MatchingPatient(
            _record(
                prior_therapy=None,
                first_line_therapy=None,
                first_line_therapy_id=None,
                first_line_therapy_type_ids=[],
                therapy_component_ids=[],
                therapy_type_ids=[],
                therapy_ids_provenance={},
            )
        )
    )
    assert pia.get_value("prior_therapy") != "None"   # unknown, not asserted-none
    assert pia.get_user_therapy_release_id() is None   # fail-closed


def test_adapter_is_read_only_for_data_fields():
    mp = MatchingPatient(_record())
    with pytest.raises(AttributeError):
        mp.disease = "breast cancer"
    # private matcher caches ARE allowed (memoization), without touching the record
    mp._some_cache = 123
    assert mp._some_cache == 123
    assert mp._record.disease == "multiple myeloma"


@pytest.mark.django_db
def test_resolved_patient_for_matching_is_read_only_and_none_when_absent(django_user_model):
    from omop_core.models import Person
    # Person.person_id is an explicit IntegerField PK (not AutoField) -> set it.
    person = Person.objects.create(person_id=9001)
    # no PatientRecord yet -> None (surfaced, never silently derived on the read path)
    assert resolved_patient_for_matching(person) is None
    PatientRecord.objects.create(person=person, disease="multiple myeloma")
    mp = resolved_patient_for_matching(person)
    assert mp is not None and mp.disease == "multiple myeloma"
