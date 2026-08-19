"""
Tests for populate_patient_record management command — new field extraction methods.

Covers:
  - get_demographics: languages_skills population
  - get_treatment_data: later_therapies JSONField
  - get_cll_data: ALC, beta-2 microglobulin, QTcF, spleen/lymph node sizes,
                  clonal counts, booleans from observations/conditions,
                  Richter transformation, BTK/BCL-2 refractoriness
  - _compute_lymphocyte_doubling_time: pure-Python helper
  - get_lymphoma_data: FLIPI, GELF, tumor grade
  - _compute_derived_fields: measurable_disease_imwg, tp53_disruption
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from omop_core.management.commands.populate_patient_record import Command
from omop_core.models import PersonLanguageSkill, ProvenanceRecord
from omop_core.services.patient_record_service import _get_tumor_size_data, refresh_patient_record
from tests.factories import (
    ConceptFactory, PersonFactory, PatientRecordFactory,
    MeasurementFactory, ObservationFactory,
    ConditionOccurrenceFactory, DrugExposureFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cmd():
    return Command()


def _loinc_concept(code, name, vocab_id='LOINC'):
    vocab = VocabularyFactory(vocabulary_id=vocab_id)
    return ConceptFactory(concept_code=code, concept_name=name, vocabulary=vocab)


# ---------------------------------------------------------------------------
# get_demographics — languages_skills
# ---------------------------------------------------------------------------

class TestLanguagesSkills:

    def test_no_language_skills_omitted(self):
        person = PersonFactory()
        data = _cmd().get_demographics(person)
        assert 'languages_skills' not in data

    def test_single_language_skill_formatted(self):
        person = PersonFactory()
        lang_concept = ConceptFactory(concept_name='English')
        PersonLanguageSkill.objects.create(
            person=person, language_concept=lang_concept, skill_level='speak'
        )
        data = _cmd().get_demographics(person)
        assert data['languages_skills'] == 'English: speak'

    def test_multiple_languages_joined(self):
        person = PersonFactory()
        en = ConceptFactory(concept_name='English')
        es = ConceptFactory(concept_name='Spanish')
        PersonLanguageSkill.objects.create(person=person, language_concept=en, skill_level='both')
        PersonLanguageSkill.objects.create(person=person, language_concept=es, skill_level='speak')
        data = _cmd().get_demographics(person)
        assert 'English: both' in data['languages_skills']
        assert 'Spanish: speak' in data['languages_skills']


# ---------------------------------------------------------------------------
# get_treatment_data — later_therapies JSONField
# ---------------------------------------------------------------------------

class TestLaterTherapies:

    def test_no_drugs_no_later_therapies(self):
        person = PersonFactory()
        data = _cmd().get_treatment_data(person)
        assert 'later_therapies' not in data

    def test_two_drugs_no_later_therapies(self):
        person = PersonFactory()
        DrugExposureFactory(person=person, drug_concept=ConceptFactory(concept_name='DrugA'),
                            drug_exposure_start_date='2023-01-01')
        DrugExposureFactory(person=person, drug_concept=ConceptFactory(concept_name='DrugB'),
                            drug_exposure_start_date='2023-06-01')
        data = _cmd().get_treatment_data(person)
        assert 'later_therapies' not in data

    def test_three_or_more_drugs_populate_later_therapies(self):
        person = PersonFactory()
        # Four distinct single agents spaced a quarter apart so LOT inference
        # segments them into four separate lines (gaps exceed the 28-day
        # combination window), rather than merging them into one regimen.
        starts_ends = [
            ('DrugA', '2023-01-01', '2023-01-28'),
            ('DrugB', '2023-04-01', '2023-04-28'),
            ('DrugC', '2023-07-01', '2023-07-28'),
            ('DrugD', '2023-10-01', '2023-10-28'),
        ]
        for name, start, end in starts_ends:
            DrugExposureFactory(
                person=person,
                drug_concept=ConceptFactory(concept_name=name),
                drug_exposure_start_date=start,
                drug_exposure_end_date=end,
            )
        data = _cmd().get_treatment_data(person)
        assert 'later_therapies' in data
        assert isinstance(data['later_therapies'], list)
        for entry in data['later_therapies']:
            assert 'therapy' in entry
            assert 'startDate' in entry
            assert 'endDate' in entry


# ---------------------------------------------------------------------------
# get_cll_data — LOINC-based measurements
# ---------------------------------------------------------------------------

class TestCllMeasurements:

    def test_absolute_lymphocyte_count(self):
        person = PersonFactory()
        concept = _loinc_concept('731-0', 'Lymphocytes [#/volume] in Blood')
        MeasurementFactory(person=person, measurement_concept=concept, value_as_number=12.5)
        data = _cmd().get_cll_data(person)
        assert data['absolute_lymphocyte_count'] == pytest.approx(12.5)

    def test_serum_beta2_microglobulin_level(self):
        person = PersonFactory()
        concept = _loinc_concept('48094-6', 'Beta-2-microglobulin [Mass/volume] in Serum')
        MeasurementFactory(person=person, measurement_concept=concept, value_as_number=4.2)
        data = _cmd().get_cll_data(person)
        assert data['serum_beta2_microglobulin_level'] == pytest.approx(4.2)

    def test_qtcf_value(self):
        person = PersonFactory()
        concept = _loinc_concept('8632-1', 'QT interval Fridericia corrected')
        MeasurementFactory(person=person, measurement_concept=concept, value_as_number=430.0)
        data = _cmd().get_cll_data(person)
        assert data['qtcf_value'] == pytest.approx(430.0)

    def test_spleen_size(self):
        person = PersonFactory()
        concept = _loinc_concept('44996-6', 'Spleen Diameter US')
        MeasurementFactory(person=person, measurement_concept=concept, value_as_number=14.5)
        data = _cmd().get_cll_data(person)
        assert data['spleen_size'] == pytest.approx(14.5)

    def test_largest_lymph_node_size(self):
        person = PersonFactory()
        concept = _loinc_concept('21889-1', 'Lymph node greatest dimension')
        MeasurementFactory(
            person=person,
            measurement_concept=concept,
            value_as_number=3.2,
            qualifier_source_value='lymph-node',
        )
        data = _cmd().get_cll_data(person)
        assert data['largest_lymph_node_size'] == pytest.approx(3.2)

    def test_code_only_21889_is_not_treated_as_lymph_node(self):
        person = PersonFactory()
        concept = _loinc_concept('21889-1', 'Size Tumor')
        MeasurementFactory(person=person, measurement_concept=concept, value_as_number=4.1)
        data = _cmd().get_cll_data(person)
        assert 'largest_lymph_node_size' not in data

    def test_tumor_and_lymph_node_contexts_derive_separately(self):
        person = PersonFactory()
        concept = _loinc_concept('21889-1', 'Size Tumor')
        tumor_measurement = MeasurementFactory(
            person=person,
            measurement_concept=concept,
            measurement_date='2024-01-01',
            value_as_number=4.1,
            unit_source_value='cm',
        )
        ProvenanceRecord.objects.create(
            source='EHR_SYNC',
            source_user_id='tumor-fixture',
            content_type=ContentType.objects.get_for_model(tumor_measurement),
            object_id=tumor_measurement.pk,
        )
        MeasurementFactory(
            person=person,
            measurement_concept=concept,
            measurement_date='2024-01-02',
            value_as_number=2.2,
            unit_source_value='cm',
            qualifier_source_value='lymph-node',
        )
        assert _get_tumor_size_data(person) == {'tumor_size': pytest.approx(4.1)}
        record = refresh_patient_record(person)
        assert record.tumor_size == pytest.approx(4.1)
        assert record.largest_lymph_node_size == pytest.approx(2.2)
        assert ProvenanceRecord.objects.filter(
            source='EHR_SYNC', source_user_id='tumor-fixture',
            object_id=tumor_measurement.pk,
        ).exists()

        # Removing the source facts must clear both projections on refresh.
        MeasurementFactory._meta.model.objects.filter(person=person).delete()
        record = refresh_patient_record(person)
        assert record.tumor_size is None
        assert record.largest_lymph_node_size is None

    def test_missing_measurement_not_in_data(self):
        person = PersonFactory()
        data = _cmd().get_cll_data(person)
        for field in ('absolute_lymphocyte_count', 'serum_beta2_microglobulin_level',
                      'qtcf_value', 'spleen_size', 'largest_lymph_node_size'):
            assert field not in data


# ---------------------------------------------------------------------------
# get_cll_data — observation-based booleans
# ---------------------------------------------------------------------------

class TestCllObservations:

    def test_binet_stage_from_value_as_string(self):
        person = PersonFactory()
        concept = ConceptFactory(concept_name='Binet Stage')
        ObservationFactory(person=person, observation_concept=concept, value_as_string='B')
        data = _cmd().get_cll_data(person)
        assert data['binet_stage'] == 'B'

    def test_tumor_burden(self):
        person = PersonFactory()
        concept = ConceptFactory(concept_name='Tumor burden criteria')
        ObservationFactory(person=person, observation_concept=concept,
                           value_as_string='High tumor burden')
        data = _cmd().get_cll_data(person)
        assert data['tumor_burden'] == 'High tumor burden'

    def test_hepatomegaly_from_observation(self):
        person = PersonFactory()
        concept = ConceptFactory(concept_name='Hepatomegaly')
        ObservationFactory(person=person, observation_concept=concept, value_as_string='yes')
        data = _cmd().get_cll_data(person)
        assert data['hepatomegaly'] is True

    def test_splenomegaly_from_observation(self):
        person = PersonFactory()
        concept = ConceptFactory(concept_name='Splenomegaly')
        ObservationFactory(person=person, observation_concept=concept, value_as_string='present')
        data = _cmd().get_cll_data(person)
        assert data['splenomegaly'] is True

    def test_lymphadenopathy_from_observation(self):
        person = PersonFactory()
        concept = ConceptFactory(concept_name='Lymphadenopathy status')
        ObservationFactory(person=person, observation_concept=concept, value_as_number=1)
        data = _cmd().get_cll_data(person)
        assert data['lymphadenopathy'] is True

    def test_bone_marrow_involvement(self):
        person = PersonFactory()
        concept = ConceptFactory(concept_name='Bone marrow involvement')
        ObservationFactory(person=person, observation_concept=concept, value_as_string='1')
        data = _cmd().get_cll_data(person)
        assert data['bone_marrow_involvement'] is True

    def test_autoimmune_cytopenias_refractory(self):
        person = PersonFactory()
        concept = ConceptFactory(concept_name='Autoimmune cytopenia refractory to steroids')
        ObservationFactory(person=person, observation_concept=concept, value_as_string='refractory')
        data = _cmd().get_cll_data(person)
        assert data['autoimmune_cytopenias_refractory_to_steroids'] is True


# ---------------------------------------------------------------------------
# get_cll_data — condition-based fields
# ---------------------------------------------------------------------------

class TestCllConditions:

    def test_richter_transformation_from_condition(self):
        person = PersonFactory()
        concept = ConceptFactory(concept_name="Richter's transformation")
        ConditionOccurrenceFactory(person=person, condition_concept=concept)
        data = _cmd().get_cll_data(person)
        assert data['richter_transformation'] == "Richter's transformation"

    def test_splenomegaly_from_condition(self):
        person = PersonFactory()
        concept = ConceptFactory(concept_name='Splenomegaly')
        ConditionOccurrenceFactory(person=person, condition_concept=concept)
        data = _cmd().get_cll_data(person)
        assert data['splenomegaly'] is True

    def test_hepatomegaly_from_condition(self):
        person = PersonFactory()
        concept = ConceptFactory(concept_name='Hepatomegaly finding')
        ConditionOccurrenceFactory(person=person, condition_concept=concept)
        data = _cmd().get_cll_data(person)
        assert data['hepatomegaly'] is True


# ---------------------------------------------------------------------------
# get_cll_data — drug-based refractoriness
# ---------------------------------------------------------------------------

class TestCllRefractoriness:

    def _progression_obs(self, person):
        """Create a progressive disease observation (SNOMED 182842009)."""
        concept = ConceptFactory(concept_name='Progressive disease', concept_code='182842009')
        ObservationFactory(person=person, observation_concept=concept)

    def test_btk_refractory_with_ibrutinib_and_progression(self):
        person = PersonFactory()
        DrugExposureFactory(person=person,
                            drug_concept=ConceptFactory(concept_name='Ibrutinib 140 mg'))
        self._progression_obs(person)
        data = _cmd().get_cll_data(person)
        assert data['btk_inhibitor_refractory'] is True

    def test_btk_not_refractory_without_progression(self):
        person = PersonFactory()
        DrugExposureFactory(person=person,
                            drug_concept=ConceptFactory(concept_name='Zanubrutinib'))
        # No progressive disease observation
        data = _cmd().get_cll_data(person)
        assert data['btk_inhibitor_refractory'] is False

    def test_bcl2_refractory_with_venetoclax_and_progression(self):
        person = PersonFactory()
        DrugExposureFactory(person=person,
                            drug_concept=ConceptFactory(concept_name='Venetoclax'))
        self._progression_obs(person)
        data = _cmd().get_cll_data(person)
        assert data['bcl2_inhibitor_refractory'] is True

    def test_no_btk_or_bcl2_drugs_no_refractoriness_keys(self):
        person = PersonFactory()
        DrugExposureFactory(person=person,
                            drug_concept=ConceptFactory(concept_name='Rituximab'))
        data = _cmd().get_cll_data(person)
        assert 'btk_inhibitor_refractory' not in data
        assert 'bcl2_inhibitor_refractory' not in data


# ---------------------------------------------------------------------------
# _compute_lymphocyte_doubling_time — pure Python
# ---------------------------------------------------------------------------

class TestLymphocyteDoublingTime:
    from datetime import date as _date

    def test_rising_alc_computes_ldt(self):
        from datetime import date
        pts = [(date(2023, 1, 1), 5), (date(2023, 7, 1), 10)]
        ldt = Command._compute_lymphocyte_doubling_time(pts)
        assert ldt is not None
        assert 5 <= ldt <= 8  # ~6 months to double

    def test_declining_alc_returns_none(self):
        from datetime import date
        pts = [(date(2023, 1, 1), 10), (date(2023, 7, 1), 5)]
        ldt = Command._compute_lymphocyte_doubling_time(pts)
        assert ldt is None

    def test_flat_alc_returns_none(self):
        from datetime import date
        pts = [(date(2023, 1, 1), 8), (date(2023, 7, 1), 8)]
        ldt = Command._compute_lymphocyte_doubling_time(pts)
        assert ldt is None

    def test_fewer_than_two_points_returns_none(self):
        from datetime import date
        assert Command._compute_lymphocyte_doubling_time([]) is None
        assert Command._compute_lymphocyte_doubling_time([(date(2023, 1, 1), 5)]) is None

    def test_minimum_ldt_is_one(self):
        from datetime import date
        # Extremely rapid doubling
        pts = [(date(2023, 1, 1), 5), (date(2023, 1, 8), 1000)]
        ldt = Command._compute_lymphocyte_doubling_time(pts)
        assert ldt >= 1


# ---------------------------------------------------------------------------
# get_lymphoma_data
# ---------------------------------------------------------------------------

class TestLymphomaData:

    def test_flipi_score_from_numeric_observation(self):
        person = PersonFactory()
        concept = ConceptFactory(concept_name='FLIPI score')
        ObservationFactory(person=person, observation_concept=concept, value_as_number=3)
        data = _cmd().get_lymphoma_data(person)
        assert data['flipi_score'] == 3

    def test_flipi_score_options_from_string_observation(self):
        person = PersonFactory()
        concept = ConceptFactory(concept_name='FLIPI criteria')
        ObservationFactory(person=person, observation_concept=concept,
                           value_as_string='Age > 60, Stage III-IV, Hgb < 12')
        data = _cmd().get_lymphoma_data(person)
        assert 'flipi_score_options' in data

    def test_gelf_criteria(self):
        person = PersonFactory()
        concept = ConceptFactory(concept_name='GELF criteria met')
        ObservationFactory(person=person, observation_concept=concept,
                           value_as_string='Yes')
        data = _cmd().get_lymphoma_data(person)
        assert data['gelf_criteria_status'] == 'Yes'

    def test_tumor_grade_from_measurement(self):
        person = PersonFactory()
        concept = ConceptFactory(concept_name='Lymphoma grade')
        MeasurementFactory(person=person, measurement_concept=concept, value_as_number=2)
        data = _cmd().get_lymphoma_data(person)
        assert data['tumor_grade'] == 2

    def test_no_lymphoma_data_empty_dict(self):
        person = PersonFactory()
        data = _cmd().get_lymphoma_data(person)
        # No transformation evidence → explicitly False (analytics denominator).
        assert data == {'transformed_to_dlbcl': False}


# ---------------------------------------------------------------------------
# get_lymphoma_data — FL → DLBCL transformation
# ---------------------------------------------------------------------------

class TestLymphomaTransformation:

    def test_dlbcl_condition_sets_flag_and_date(self):
        person = PersonFactory()
        dlbcl = ConceptFactory(concept_name='Diffuse large B-cell lymphoma')
        ConditionOccurrenceFactory(person=person, condition_concept=dlbcl,
                                   condition_start_date='2023-05-10')
        data = _cmd().get_lymphoma_data(person)
        assert data['transformed_to_dlbcl'] is True
        assert str(data['dlbcl_transformation_date']) == '2023-05-10'

    def test_earliest_dlbcl_condition_wins(self):
        person = PersonFactory()
        dlbcl = ConceptFactory(concept_name='Diffuse large B-cell lymphoma')
        ConditionOccurrenceFactory(person=person, condition_concept=dlbcl,
                                   condition_start_date='2024-01-01')
        ConditionOccurrenceFactory(person=person, condition_concept=dlbcl,
                                   condition_start_date='2023-06-15')
        data = _cmd().get_lymphoma_data(person)
        assert str(data['dlbcl_transformation_date']) == '2023-06-15'

    def test_transformation_observation_fallback(self):
        """No DLBCL condition, but a transformation observation exists."""
        person = PersonFactory()
        concept = ConceptFactory(concept_name='Histologic transformation to DLBCL')
        ObservationFactory(person=person, observation_concept=concept,
                           observation_date='2023-11-20')
        data = _cmd().get_lymphoma_data(person)
        assert data['transformed_to_dlbcl'] is True
        assert str(data['dlbcl_transformation_date']) == '2023-11-20'

    def test_transformation_observation_fallback_by_source_value(self):
        """Observation matched via observation_source_value when concept is generic."""
        person = PersonFactory()
        concept = ConceptFactory(concept_name='Generic Lab Measurement')
        ObservationFactory(person=person, observation_concept=concept,
                           observation_source_value='fl-transformed-dlbcl',
                           observation_date='2023-08-01')
        data = _cmd().get_lymphoma_data(person)
        assert data['transformed_to_dlbcl'] is True
        assert str(data['dlbcl_transformation_date']) == '2023-08-01'

    def test_no_evidence_means_not_transformed(self):
        person = PersonFactory()
        fl = ConceptFactory(concept_name='Follicular lymphoma')
        ConditionOccurrenceFactory(person=person, condition_concept=fl,
                                   condition_start_date='2020-01-01')
        data = _cmd().get_lymphoma_data(person)
        assert data['transformed_to_dlbcl'] is False
        assert 'dlbcl_transformation_date' not in data
        assert 'post_transformation_outcome' not in data

    def test_death_after_transformation_outcome_deceased(self):
        from omop_core.models import Death
        person = PersonFactory()
        dlbcl = ConceptFactory(concept_name='Diffuse large B-cell lymphoma')
        ConditionOccurrenceFactory(person=person, condition_concept=dlbcl,
                                   condition_start_date='2023-05-10')
        Death.objects.create(person=person, death_date='2024-02-01',
                             death_type_concept=ConceptFactory(concept_name='EHR'))
        data = _cmd().get_lymphoma_data(person)
        assert data['post_transformation_outcome'] == 'Deceased'

    def test_post_transformation_line_outcome(self):
        """Latest LOT-outcome observation on/after transformation sets the outcome."""
        person = PersonFactory()
        dlbcl = ConceptFactory(concept_name='Diffuse large B-cell lymphoma')
        ConditionOccurrenceFactory(person=person, condition_concept=dlbcl,
                                   condition_start_date='2023-05-10')
        concept = ConceptFactory(concept_name='Response to cancer treatment')
        # pre-transformation outcome — must be ignored
        ObservationFactory(person=person, observation_concept=concept,
                           observation_source_value='LOT-1-outcome',
                           observation_date='2022-01-01', value_as_string='Progressive Disease')
        ObservationFactory(person=person, observation_concept=concept,
                           observation_source_value='LOT-2-outcome',
                           observation_date='2023-09-01', value_as_string='Complete Response')
        data = _cmd().get_lymphoma_data(person)
        assert data['post_transformation_outcome'] == 'Complete Response'

    def test_death_takes_precedence_over_line_outcome(self):
        from omop_core.models import Death
        person = PersonFactory()
        dlbcl = ConceptFactory(concept_name='Diffuse large B-cell lymphoma')
        ConditionOccurrenceFactory(person=person, condition_concept=dlbcl,
                                   condition_start_date='2023-05-10')
        ObservationFactory(person=person, observation_concept=ConceptFactory(concept_name='Response'),
                           observation_source_value='LOT-2-outcome',
                           observation_date='2023-09-01', value_as_string='Complete Response')
        Death.objects.create(person=person, death_date='2024-01-01',
                             death_type_concept=ConceptFactory(concept_name='EHR'))
        data = _cmd().get_lymphoma_data(person)
        assert data['post_transformation_outcome'] == 'Deceased'

    def test_transformed_without_outcome_leaves_outcome_unset(self):
        person = PersonFactory()
        dlbcl = ConceptFactory(concept_name='Diffuse large B-cell lymphoma')
        ConditionOccurrenceFactory(person=person, condition_concept=dlbcl,
                                   condition_start_date='2023-05-10')
        data = _cmd().get_lymphoma_data(person)
        assert data['transformed_to_dlbcl'] is True
        assert 'post_transformation_outcome' not in data


# ---------------------------------------------------------------------------
# _compute_derived_fields — measurable_disease_imwg
# ---------------------------------------------------------------------------

class TestMeasurableDiseaseImwg:

    def _pi(self, **kwargs):
        """PatientRecord with controlled lab fields; skip save() side effects."""
        pi = PatientRecordFactory.build(**kwargs)
        return pi

    def test_serum_m_protein_high_sets_true(self):
        pi = self._pi(monoclonal_protein_serum=0.6)
        _cmd()._compute_derived_fields(pi)
        assert pi.measurable_disease_imwg is True

    def test_serum_m_protein_low_sets_false(self):
        pi = self._pi(monoclonal_protein_serum=0.3, monoclonal_protein_urine=None,
                      kappa_flc=None, lambda_flc=None)
        _cmd()._compute_derived_fields(pi)
        assert pi.measurable_disease_imwg is False

    def test_urine_m_protein_high_sets_true(self):
        pi = self._pi(monoclonal_protein_serum=None, monoclonal_protein_urine=250)
        _cmd()._compute_derived_fields(pi)
        assert pi.measurable_disease_imwg is True

    def test_flc_ratio_abnormal_sets_true(self):
        pi = self._pi(monoclonal_protein_serum=None, monoclonal_protein_urine=None,
                      kappa_flc=120, lambda_flc=1)  # ratio=120 >100, diff=119 >=10
        _cmd()._compute_derived_fields(pi)
        assert pi.measurable_disease_imwg is True

    def test_all_none_stays_none(self):
        pi = self._pi(monoclonal_protein_serum=None, monoclonal_protein_urine=None,
                      kappa_flc=None, lambda_flc=None)
        _cmd()._compute_derived_fields(pi)
        assert pi.measurable_disease_imwg is None


# ---------------------------------------------------------------------------
# _compute_derived_fields — tp53_disruption
# ---------------------------------------------------------------------------

class TestTp53Disruption:

    def _pi_with_mutations(self, mutations):
        pi = PatientRecordFactory.build()
        pi.genetic_mutations = mutations
        pi.monoclonal_protein_serum = None
        pi.monoclonal_protein_urine = None
        pi.kappa_flc = None
        pi.lambda_flc = None
        return pi

    def test_tp53_pathogenic_sets_true(self):
        pi = self._pi_with_mutations([
            {'gene': 'tp53', 'interpretation': 'pathogenic'},
        ])
        _cmd()._compute_derived_fields(pi)
        assert pi.tp53_disruption is True

    def test_tp53_benign_stays_false(self):
        pi = self._pi_with_mutations([
            {'gene': 'tp53', 'interpretation': 'benign'},
        ])
        _cmd()._compute_derived_fields(pi)
        assert pi.tp53_disruption is False

    def test_no_mutations_false(self):
        pi = self._pi_with_mutations([])
        _cmd()._compute_derived_fields(pi)
        assert pi.tp53_disruption is False

    def test_other_gene_pathogenic_does_not_set_tp53(self):
        pi = self._pi_with_mutations([
            {'gene': 'brca1', 'interpretation': 'pathogenic'},
        ])
        _cmd()._compute_derived_fields(pi)
        assert pi.tp53_disruption is False
