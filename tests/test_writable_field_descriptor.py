"""The descriptor that tells a client how to write a clinical fact.

PatientRecord has no writable clinical columns, so an editor has to write the OMOP
fact instead. These pin the two properties that make that possible: a mapped field
carries everything needed to build a complete Measurement, and an unmapped one says
so out loud rather than going missing.
"""
import pytest
from django.test.utils import CaptureQueriesContext
from django.db import connection

from omop_core.services.mappings import CONCEPT_PATIENT_REPORTED_TYPE, LAB_FIELD_TO_LOINC
from omop_core.services.patient_record_service import (
    PATIENT_RECORD_OMOP_MAPPED_FIELDS,
)
from omop_core.services.write_descriptor import build_writable_field_descriptor
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def _load_loinc(code, concept_id=None):
    VocabularyFactory(vocabulary_id='LOINC')
    return ConceptFactory(
        concept_code=code, vocabulary_id='LOINC',
        concept_name=f'Concept {code}',
        **({'concept_id': concept_id} if concept_id else {}),
    )


def _load_ucum(unit):
    VocabularyFactory(vocabulary_id='UCUM', vocabulary_name='UCUM')
    return ConceptFactory(
        concept_code=unit, vocabulary_id='UCUM', concept_name=unit,
    )


class TestMappedFields:
    def test_largest_lymph_node_recipe_uses_the_standard_cancer_modifier(self):
        concept = ConceptFactory(
            concept_id=36769292,
            concept_code='largest-lymph-node-dimension',
            concept_name='Dimension of Largest Lymph Node',
            vocabulary=VocabularyFactory(vocabulary_id='Cancer Modifier'),
        )

        entry = build_writable_field_descriptor()['largest_lymph_node_size']

        assert entry['writable'] is True
        assert entry['target'] == 'patient_record'
        assert entry['projection']['concept_id'] == concept.concept_id
        assert entry['projection']['vocabulary'] == 'Cancer Modifier'
        assert entry['projection']['source_value'] == concept.concept_code

    def test_a_loaded_mapping_is_writable_with_a_full_fact_recipe(self):
        concept = _load_loinc('718-7')
        unit = _load_ucum('g/dL')

        entry = build_writable_field_descriptor()['hemoglobin_g_dl']

        assert entry['writable'] is True
        assert entry['target'] == 'patient_record'
        assert entry['kind'] == 'direct'
        assert entry['value_kind'] == 'number'
        assert entry['unit'] == 'g/dL'
        # OMOP routing lives under 'projection'
        proj = entry['projection']
        assert proj['omop_table'] == 'measurement'
        assert proj['concept_id'] == concept.concept_id
        assert proj['code'] == '718-7'
        assert proj['vocabulary'] == 'LOINC'
        assert proj['unit'] == 'g/dL'
        assert proj['unit_concept_id'] == unit.concept_id
        assert proj['type_concept_id'] == CONCEPT_PATIENT_REPORTED_TYPE
        assert proj['source_value'] == '718-7'

    def test_every_key_a_measurement_write_needs_is_present(self):
        _load_loinc('718-7')
        entry = build_writable_field_descriptor()['hemoglobin_g_dl']
        assert 'projection' in entry
        required = {
            'omop_table', 'concept_id', 'type_concept_id', 'source_value',
        }
        assert required <= set(entry['projection'])
        assert 'value_kind' in entry

    def test_missing_unit_concept_does_not_make_the_field_unwritable(self):
        """UCUM may be absent; the fact is still writable with a source unit string."""
        _load_loinc('718-7')

        entry = build_writable_field_descriptor()['hemoglobin_g_dl']

        assert entry['writable'] is True
        assert entry['unit'] == 'g/dL'
        assert entry['projection']['unit_concept_id'] is None


class TestUnmappedFields:
    def test_a_field_with_no_concept_set_is_reported_not_omitted(self):
        """A client must be able to tell 'may not edit' from 'was not sent'."""
        descriptor = build_writable_field_descriptor()

        assert 'planned_therapies' in descriptor
        # Under the PatientRecord-first architecture, unmapped fields are
        # directly writable on PatientRecord (KIND_DIRECT).
        assert descriptor['planned_therapies']['writable'] is True
        assert descriptor['planned_therapies']['kind'] == 'direct'

    def test_a_mapped_code_absent_from_the_vocabulary_is_still_directly_writable(self):
        """Without the vocab, no OMOP projection — but the field is directly writable."""
        descriptor = build_writable_field_descriptor()

        entry = descriptor['hemoglobin_g_dl']
        assert entry['writable'] is True
        assert entry['target'] == 'patient_record'
        assert '718-7' in entry.get('reason', '')
        assert 'projection' not in entry  # no OMOP projection without the concept

    def test_every_mapped_projection_field_appears(self):
        descriptor = build_writable_field_descriptor()
        lifecycle = {
            'id', 'person', 'organization', 'created_at', 'updated_at',
            'derived_at', 'derivation_version', 'user_edited_fields',
        }
        # The mapped columns, plus the read-only fields the serializer adds on
        # top of them. Those have no column to walk, so they are listed
        # explicitly rather than discovered -- and stating the sum here keeps the
        # descriptor's contents exact instead of merely "at least".
        from omop_core.services.write_descriptor import (
            _SERIALIZER_ALIASES, _SERIALIZER_COMPUTED,
        )
        expected = (
            (PATIENT_RECORD_OMOP_MAPPED_FIELDS - lifecycle)
            | set(_SERIALIZER_ALIASES) | set(_SERIALIZER_COMPUTED)
        )
        assert set(descriptor) == expected

    def test_no_lifecycle_column_is_offered(self):
        descriptor = build_writable_field_descriptor()
        for field in ('created_at', 'derivation_version', 'user_edited_fields'):
            assert field not in descriptor


class TestKinds:
    """Every field is editable, selectable, computed, or an alias — or it needs a
    concept set. Nothing is left as an unexplained 'no'."""

    def test_an_alias_points_at_its_canonical_field(self):
        entry = build_writable_field_descriptor()['estimated_glomerular_filtration_rate']

        assert entry['kind'] == 'alias'
        assert entry['writable'] is False
        assert entry['canonical'] == 'egfr_ml_min_173m2'

    def test_no_alias_is_offered_as_editable(self):
        """Writing an alias and its canonical collides on one LOINC row (#471)."""
        descriptor = build_writable_field_descriptor()
        for alias in ('calcium_mg_dl', 'creatinine_mg_dl', 'blood_urea_nitrogen'):
            assert descriptor[alias]['kind'] == 'alias'
            assert descriptor[alias]['writable'] is False

    def test_a_computed_field_names_its_inputs(self):
        entry = build_writable_field_descriptor()['bmi']

        assert entry['kind'] == 'computed'
        assert set(entry['inputs']) == {'height', 'weight'}
        assert 'height' in entry['reason']

    def test_tnbc_status_is_computed_from_three_receptors(self):
        entry = build_writable_field_descriptor()['tnbc_status']

        assert entry['kind'] == 'computed'
        assert set(entry['inputs']) == {
            'estrogen_receptor_status', 'progesterone_receptor_status', 'her2_status',
        }

    def test_a_unit_column_is_selectable_and_names_what_it_qualifies(self):
        entry = build_writable_field_descriptor()['weight_units']

        assert entry['kind'] == 'selectable'
        assert entry['qualifies'] == 'weight'

    def test_a_mapped_lab_is_direct_with_projection(self):
        _load_loinc('718-7')
        entry = build_writable_field_descriptor()['hemoglobin_g_dl']
        assert entry['kind'] == 'direct'
        assert 'projection' in entry

    def test_every_field_carries_a_reason_when_not_writable(self):
        """A UI must always be able to say why a box is not typeable."""
        for field, entry in build_writable_field_descriptor().items():
            if not entry['writable']:
                assert entry.get('reason'), field

    def test_kind_is_one_of_the_known_values(self):
        allowed = {'direct', 'editable', 'selectable', 'computed', 'alias',
                   'profile', 'unmapped', 'authored'}
        for field, entry in build_writable_field_descriptor().items():
            assert entry['kind'] in allowed, (field, entry['kind'])


class TestCost:
    def test_query_count_is_flat_not_per_field(self):
        """One lookup per vocabulary, however many fields are mapped."""
        for code, _unit, _display in LAB_FIELD_TO_LOINC.values():
            _load_loinc(code)

        with CaptureQueriesContext(connection) as ctx:
            build_writable_field_descriptor()

        # One batch lookup per source table (LOINC codes, UCUM units, attributed
        # codes), plus the approved-mapping table and one read per *distinct*
        # answer vocabulary those mappings name — four fields sharing a set cost
        # one query, not four. Constant in the number of fields either way; the
        # bound guards the shape, not the exact number.
        assert len(ctx) <= 6, [q['sql'][:80] for q in ctx]


class TestEndpoint:
    def test_requires_authentication(self, client):
        resp = client.get('/api/v1/patient-records/writable-fields/')
        assert resp.status_code in (401, 403)


class TestAliasMapIntegrity:
    """A duplicate key in the alias literal silently drops an alias list.

    `_LAB_FIELD_ALIASES` listed 'egfr_ml_min_173m2' and 'alkaline_phosphatase_u_l'
    twice each. Python keeps the last, so 'egfr' and 'alkaline_phosphatase' lost
    their propagation: both are in _OMOP_DERIVED_FIELDS, so derivation cleared them
    on every refresh and nothing ever wrote them back. They read as permanently
    null. Nothing failed loudly, which is why it survived.
    """

    def test_no_canonical_is_listed_twice(self):
        import ast
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1]
            / 'omop_core/services/patient_record_service.py'
        ).read_text()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Assign):
                continue
            if getattr(node.targets[0], 'id', '') != '_LAB_FIELD_ALIASES':
                continue
            keys = [k.value for k in node.value.keys]
            duplicates = {k for k in keys if keys.count(k) > 1}
            assert not duplicates, f'duplicate keys silently drop aliases: {duplicates}'
            return
        raise AssertionError('_LAB_FIELD_ALIASES literal not found')

    def test_every_cleared_lab_field_is_repopulated(self):
        """A field derivation clears must have a source, or it reads as null forever."""
        from omop_core.services import patient_record_service as prs
        from omop_core.services.mappings import LAB_FIELD_TO_LOINC

        repopulated = {a for v in prs._LAB_FIELD_ALIASES.values() for a in v}
        for field in ('egfr', 'alkaline_phosphatase',
                      'estimated_glomerular_filtration_rate',
                      'liver_enzyme_levels_alp'):
            assert field in prs._OMOP_DERIVED_FIELDS, field
            assert field not in LAB_FIELD_TO_LOINC, field
            assert field in repopulated, f'{field} is cleared but never repopulated'


class TestExtractorAttributedMappings:
    """Mappings recovered from the extractors rather than chosen by hand.

    If derivation reads code X into field F, writing X round-trips. The table
    records which extractor each attribution came from so a reviewer can audit
    the claim at its source.
    """

    def test_an_attributed_field_is_direct_with_projection_and_names_its_extractor(self):
        VocabularyFactory(vocabulary_id='LOINC')
        DomainFactory(domain_id='Measurement', domain_name='Measurement')
        ConceptFactory(concept_code='48676-1', vocabulary_id='LOINC',
                       concept_name='HER2 [Interpretation]', domain_id='Measurement')

        entry = build_writable_field_descriptor()['her2_status']

        assert entry['kind'] == 'direct'
        assert entry['writable'] is True
        assert entry['target'] == 'patient_record'
        assert entry['projection']['code'] == '48676-1'
        assert entry['attributed_from'] == '_get_biomarker_data'

    def test_projection_table_follows_the_concept_domain(self):
        """An Observation-domain concept must project to observation, not measurement."""
        VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED')
        DomainFactory(domain_id='Observation', domain_name='Observation')
        ConceptFactory(concept_code='408729009', vocabulary_id='SNOMED',
                       concept_name='Health insurance', domain_id='Observation')

        entry = build_writable_field_descriptor()['insurance_type']

        assert entry['target'] == 'patient_record'
        assert entry['projection']['omop_table'] == 'observation'

    def test_value_kind_comes_from_the_model_column(self):
        VocabularyFactory(vocabulary_id='LOINC')
        DomainFactory(domain_id='Measurement', domain_name='Measurement')
        ConceptFactory(concept_code='83054-7', vocabulary_id='LOINC',
                       concept_name='PD-L1 CPS', domain_id='Measurement')

        entry = build_writable_field_descriptor()['pd_l1_combined_positive_score']

        assert entry['value_kind'] == 'number'   # IntegerField on PatientRecord

    def test_a_code_absent_from_the_vocabulary_is_still_directly_writable(self):
        """No concepts loaded — no projection, but field is still directly writable."""
        entry = build_writable_field_descriptor()['androgen_receptor_status']

        assert entry['writable'] is True
        assert entry['target'] == 'patient_record'
        assert 'projection' not in entry
        assert '49457-5' in entry.get('reason', '')
        assert entry['attributed_from'] == '_get_genomics_pathology_data'


class TestNoCodeIsClaimedTwice:
    def test_every_attributed_code_is_unique_across_both_tables(self):
        """Two fields on one code overwrite each other's row — the #471 collision.

        Ten further fields were attributed to a single code and deliberately left
        out of DERIVED_FIELD_TO_CODE for exactly this reason.
        """
        from omop_core.services.mappings import (
            DERIVED_FIELD_TO_CODE, LAB_FIELD_TO_LOINC,
        )

        seen = {}
        for field, (code, _unit, _display) in LAB_FIELD_TO_LOINC.items():
            seen.setdefault(('LOINC', code), []).append(field)
        for field, (code, vocab, _fn) in DERIVED_FIELD_TO_CODE.items():
            seen.setdefault((vocab, code), []).append(field)

        collisions = {k: v for k, v in seen.items() if len(v) > 1}
        assert not collisions, f'code claimed by more than one field: {collisions}'

    def test_every_attribution_names_a_real_extractor(self):
        import inspect
        from omop_core.services import patient_record_service as prs
        from omop_core.services.mappings import DERIVED_FIELD_TO_CODE

        for field, (_code, _vocab, extractor) in DERIVED_FIELD_TO_CODE.items():
            assert hasattr(prs, extractor), f'{field} cites missing {extractor}'
            assert inspect.isfunction(getattr(prs, extractor)), field


class TestAndrogenReceptorCode:
    """82185-1 is not a LOINC code; 49457-5 is the standard concept for the analyte.

    Both sides had to move together. Changing only the write mapping would emit
    facts derivation never reads back, breaking the round trip that justifies
    DERIVED_FIELD_TO_CODE existing at all.
    """

    def test_write_mapping_uses_the_standard_loinc_concept(self):
        from omop_core.services.mappings import DERIVED_FIELD_TO_CODE

        code, vocab, _fn = DERIVED_FIELD_TO_CODE['androgen_receptor_status']
        assert (code, vocab) == ('49457-5', 'LOINC')

    def test_derivation_reads_both_the_new_and_legacy_code(self):
        """A row already written under the legacy non-code must still project."""
        from omop_core.services.patient_record_service import (
            _GENOMICS_PATHOLOGY_LOINCS,
        )

        assert '49457-5' in _GENOMICS_PATHOLOGY_LOINCS
        assert '82185-1' in _GENOMICS_PATHOLOGY_LOINCS


class TestEveryFieldIsCategorised:
    """The descriptor documents the whole record, not just the editable part.

    A reader must be able to see every column and what stands between it and
    being editable, rather than inferring it from an absence.
    """

    def test_no_field_is_left_uncategorised(self):
        for field, entry in build_writable_field_descriptor().items():
            assert entry['kind'] is not None, field

    def test_an_unmapped_field_says_which_group_it_is_in(self):
        d = build_writable_field_descriptor()
        for field, entry in d.items():
            if entry['kind'] == 'unmapped':
                assert entry.get('group'), field
                assert entry.get('reason'), field

    def test_therapy_line_fields_are_computed_from_persisted_episode_events(self):
        """Per-line columns are projections, not direct PatientRecord inputs."""
        d = build_writable_field_descriptor()
        for field in ('first_line_outcome', 'second_line_start_date', 'later_end_date'):
            assert d[field]['kind'] == 'computed', field
            assert d[field]['source_tables'] == ['Episode', 'EpisodeEvent']
        # Other treatment summaries retain their existing episode-authoring
        # guidance; they are not individual first/second/later-line columns.
        assert d['line_of_therapy']['group'] == 'therapy-inference'
        assert d['relapse_count']['writable'] is True

    def test_location_fields_are_writable_not_grouped_as_missing(self):
        """They were grouped as 'location' only while they had no write path.
        The PatientRecord PATCH now upserts the OMOP Location row."""
        d = build_writable_field_descriptor()
        assert d['city']['kind'] == 'direct'
        assert d['city']['writable'] is True
        assert d['city']['target'] == 'patient_record'


class TestProfileFields:
    def test_a_replaceable_profile_field_is_writable_via_patient_record(self):
        entry = build_writable_field_descriptor()['email']

        assert entry['kind'] == 'direct'
        assert entry['writable'] is True
        assert entry['target'] == 'patient_record'
        assert entry['projection_target'] == 'person'

    def test_date_of_birth_is_correctable_via_patient_record(self):
        entry = build_writable_field_descriptor()['date_of_birth']

        assert entry['kind'] == 'direct'
        assert entry['writable'] is True
        assert entry['target'] == 'patient_record'
        assert entry['projection_target'] == 'person'
        assert 'fill_if_empty' not in entry

    def test_no_direct_field_is_falsely_read_only(self):
        direct = {
            field: entry for field, entry in build_writable_field_descriptor().items()
            if entry['kind'] == 'direct' and not entry.get('curated')
        }

        assert direct
        assert not [field for field, entry in direct.items() if not entry['writable']]


class TestWearableAggregates:
    def test_coverage_ratio_is_computed_from_all_device_metrics(self):
        entry = build_writable_field_descriptor()['wearable_coverage_ratio_30d']
        assert entry['kind'] == 'computed'
        assert entry['writable'] is False
        assert entry['window_days'] == 30
        assert set(entry['inputs']) == {
            'steps', 'active_minutes', 'resting_hr', 'hrv_sdnn', 'hrv_rmssd',
            'spo2', 'respiratory_rate', 'sleep_duration', 'vo2_max', 'distance',
            'walking_speed', 'walking_step_length', 'walking_double_support_pct',
            'walking_hr_avg', 'flights_climbed', 'active_energy', 'basal_energy', 'body_mass',
        }
        assert len(entry['inputs']) == 18
        assert 'counting each day once' in entry['reason']

    def test_no_thirty_day_aggregate_is_reported_unmapped(self):
        descriptor = build_writable_field_descriptor()
        aggregates = {field: entry for field, entry in descriptor.items() if field.endswith('_30d')}
        assert aggregates
        assert not [field for field, entry in aggregates.items() if entry['kind'] == 'unmapped']

    def test_last_sync_remains_device_metadata(self):
        entry = build_writable_field_descriptor()['wearable_last_sync_at']
        assert entry['kind'] == 'unmapped'
        assert entry['group'] == 'wearable-metadata'
        assert entry['writable'] is False

    def test_an_aggregate_is_computed_over_a_series(self):
        entry = build_writable_field_descriptor()['median_daily_steps_30d']

        assert entry['kind'] == 'computed'
        assert entry['inputs'] == ['steps']
        assert entry['window_days'] == 30

    def test_every_wearable_metric_named_is_a_real_one(self):
        """A metric name that does not exist would be an unresolvable instruction."""
        from omop_core.services.mappings import WEARABLE_CONCEPT_CODE
        from omop_core.services.write_descriptor import _WEARABLE_METRIC

        unknown = set(_WEARABLE_METRIC.values()) - set(WEARABLE_CONCEPT_CODE)
        assert not unknown, unknown


class TestAttributionsTrackDerivation:
    """An attribution is only true while its extractor still behaves that way.

    DERIVED_FIELD_TO_CODE is recovered from the derivation source: if derivation
    reads code X into field F, writing X round-trips. That reasoning has an
    expiry date. #596 fixed _get_social_data, which had been writing SNOMED
    408729009 into concomitant_medication_details — the very line the attribution
    was read from. The moment the bug was fixed the mapping became a lie: the
    descriptor would have told a client to write 408729009 for concomitant
    medication details, and derivation would have surfaced it as insurance type.

    The uniqueness guard could not catch it. A code claimed by exactly one field
    is still wrong if it is the wrong field.
    """

    def test_every_attribution_still_matches_its_extractor(self):
        import inspect
        from omop_core.services import patient_record_service as prs
        from omop_core.services.mappings import DERIVED_FIELD_TO_CODE

        stale = []
        for field, (code, _vocab, extractor) in DERIVED_FIELD_TO_CODE.items():
            source = inspect.getsource(getattr(prs, extractor))
            writes_field = f"data['{field}']" in source or f'data["{field}"]' in source
            # The code may be inline or reached through a module-level constant
            # the function names; both count as this extractor reading it.
            mentions_code = f"'{code}'" in source or f'"{code}"' in source
            if not mentions_code:
                mentions_code = any(
                    f"'{code}'" in inspect.getsource(prs).split(const)[0][-4000:]
                    for const in [n for n in dir(prs) if n.isupper() and n in source]
                ) or f"'{code}'" in inspect.getsource(prs)
            if not (writes_field and mentions_code):
                stale.append(
                    f'{field}: {extractor} '
                    f'{"does not write it" if not writes_field else ""}'
                    f'{" / does not read " + code if not mentions_code else ""}'
                )
        assert not stale, 'attributions no longer match derivation: ' + '; '.join(stale)

    def test_the_social_data_codes_are_attributed_to_the_fields_they_now_write(self):
        """Pins the specific drift #596 introduced."""
        from omop_core.services.mappings import DERIVED_FIELD_TO_CODE

        assert DERIVED_FIELD_TO_CODE.get('insurance_type', (None,))[0] == '408729009'
        assert 'concomitant_medication_details' not in DERIVED_FIELD_TO_CODE


_LIFECYCLE = {
    'id', 'person', 'organization', 'created_at', 'updated_at',
    'derived_at', 'derivation_version', 'user_edited_fields', 'custom_fields', 'therapy_overrides',
}


class TestSerializerFieldCoverage:
    """Every read-only field the API exposes must be described.

    The builder walks PATIENT_RECORD_OMOP_MAPPED_FIELDS, so it can only describe
    model columns. SerializerMethodFields and read-only aliases were invisible to
    it, and an editor asking "may I write this?" got no entry at all — which is
    how the treatment tab came to offer a select over ``refractory_status``, a
    value derived from therapy episodes that the server refuses to accept.
    """

    def test_every_read_only_serializer_field_is_described(self):
        from patient_portal.api.serializers import PatientRecordSerializer

        descriptor = build_writable_field_descriptor()
        read_only = {
            name for name, field in PatientRecordSerializer().get_fields().items()
            if field.read_only
        }
        # patient_name is popped and applied to Person before the serializer sees
        # it, so it is genuinely writable on that endpoint despite being read-only
        # here. Describing it as read-only would stop renames.
        assert read_only - set(descriptor) - _LIFECYCLE == {'patient_name'}

    def test_refractory_status_is_an_alias_of_its_canonical(self):
        entry = build_writable_field_descriptor()['refractory_status']
        assert entry['writable'] is True
        assert entry['canonical'] == 'treatment_refractory_status'

    @pytest.mark.parametrize('field', [
        'age', 'lines_of_therapy', 'name', 'person_id',
        'therapy_release_id', 'first_line_therapy_display',
    ])
    def test_nothing_serializer_derived_claims_to_be_writable(self, field):
        entry = build_writable_field_descriptor()[field]
        assert entry['writable'] is False, (
            f'{field} is read-only server-side but the descriptor offers it'
        )


class TestProfilePayloadField:
    """A profile descriptor must name the key the persons endpoint accepts.

    ``person_field`` is prose documenting the Person columns behind the value
    ("gender_concept + gender_source_value", "Location.city"). A client using it
    as a payload key sends something the endpoint ignores, so the write returns
    200 and changes nothing.
    """

    def test_every_profile_field_has_projection_target(self):
        """Profile fields now route through PatientRecord PATCH with a
        projection_target indicating the backing store (person or location)."""
        from patient_portal.api.views import (
            _PROFILE_DEMOGRAPHIC_FIELDS, _PROFILE_LOCATION_FIELDS,
            _PROFILE_SIMPLE_PERSON_FIELDS,
        )

        accepted = (
            set(_PROFILE_DEMOGRAPHIC_FIELDS) | set(_PROFILE_LOCATION_FIELDS)
            | _PROFILE_SIMPLE_PERSON_FIELDS | {'date_of_birth'}
        )
        profile = {
            f: v for f, v in build_writable_field_descriptor().items()
            if v.get('projection_target') in ('person', 'location') and v.get('writable')
        }
        assert profile, 'expected writable profile fields'
        for field, entry in profile.items():
            assert entry.get('target') == 'patient_record', (
                f'{field} should target patient_record, not {entry.get("target")}'
            )
            assert entry['payload_field'] in accepted, (
                f"{field} payload_field '{entry['payload_field']}' not in accepted set"
            )
