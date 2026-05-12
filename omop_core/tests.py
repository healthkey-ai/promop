"""
omop_core tests — TEST-01, TEST-02, TEST-03

TEST-01: PatientInfo model-level tests
TEST-02: refresh_patient_info service unit tests
TEST-03: Signal integration tests at omop_core level
"""

from datetime import date

from django.test import TestCase

from omop_core.models import (
    Concept, ConceptClass, Domain, Vocabulary,
    Person, PatientInfo, ConditionOccurrence, DrugExposure, Measurement, Observation,
)
from omop_core.services.patient_info_service import refresh_patient_info


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_vocab():
    """Return (vocab, domain_condition, domain_measurement, domain_drug, cc)."""
    vocab, _ = Vocabulary.objects.get_or_create(
        vocabulary_id='OMOP_TEST',
        defaults={'vocabulary_name': 'OMOP Test', 'vocabulary_concept_id': 0},
    )
    dom_cond, _ = Domain.objects.get_or_create(
        domain_id='Condition', defaults={'domain_name': 'Condition', 'domain_concept_id': 19}
    )
    dom_meas, _ = Domain.objects.get_or_create(
        domain_id='Measurement', defaults={'domain_name': 'Measurement', 'domain_concept_id': 21}
    )
    dom_drug, _ = Domain.objects.get_or_create(
        domain_id='Drug', defaults={'domain_name': 'Drug', 'domain_concept_id': 13}
    )
    dom_type, _ = Domain.objects.get_or_create(
        domain_id='Type Concept', defaults={'domain_name': 'Type Concept', 'domain_concept_id': 58}
    )
    dom_obs, _ = Domain.objects.get_or_create(
        domain_id='Observation', defaults={'domain_name': 'Observation', 'domain_concept_id': 27}
    )
    cc, _ = ConceptClass.objects.get_or_create(
        concept_class_id='Clinical Finding',
        defaults={'concept_class_name': 'Clinical Finding', 'concept_class_concept_id': 0},
    )
    return vocab, dom_cond, dom_meas, dom_drug, dom_type, dom_obs, cc


def _concept(cid, name, domain, vocab, cc, code=None):
    obj, _ = Concept.objects.get_or_create(
        concept_id=cid,
        defaults={
            'concept_name': name,
            'domain': domain,
            'vocabulary': vocab,
            'concept_class': cc,
            'concept_code': code or str(cid),
            'valid_start_date': date.today(),
            'valid_end_date': date(2099, 12, 31),
        },
    )
    return obj


class _OmopBase(TestCase):
    """Shared setup for omop_core tests."""

    PERSON_ID = 90000  # override per class

    @classmethod
    def setUpTestData(cls):
        vocab, dom_cond, dom_meas, dom_drug, dom_type, dom_obs, cc = _make_vocab()
        cls.vocab = vocab
        cls.dom_cond = dom_cond
        cls.dom_meas = dom_meas
        cls.dom_drug = dom_drug
        cls.dom_obs = dom_obs
        cls.cc = cc

        cls.type_concept = _concept(90099, 'EHR', dom_type, vocab, cc)
        cls.cancer_concept = _concept(90001, 'Malignant neoplasm of breast', dom_cond, vocab, cc)
        cls.drug_concept = _concept(90010, 'Doxorubicin', dom_drug, vocab, cc)

        cls.person = Person.objects.create(
            person_id=cls.PERSON_ID,
            year_of_birth=1980,
            gender_source_value='female',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )


# ===========================================================================
# TEST-01: PatientInfo model-level tests
# ===========================================================================

class PatientInfoModelTest(_OmopBase):
    """PatientInfo field persistence, nullability, and OneToOne constraint."""

    PERSON_ID = 90100

    def test_create_patient_info_with_basic_fields(self):
        """PatientInfo can be created and fields persist to the DB."""
        pi = PatientInfo.objects.create(
            person=self.person,
            disease='Breast Cancer',
            hemoglobin_g_dl=11.5,
            wbc_count_thousand_per_ul=4.2,
        )
        fetched = PatientInfo.objects.get(pk=pi.pk)
        self.assertEqual(fetched.disease, 'Breast Cancer')
        self.assertAlmostEqual(float(fetched.hemoglobin_g_dl), 11.5, places=1)
        self.assertAlmostEqual(float(fetched.wbc_count_thousand_per_ul), 4.2, places=1)

    def test_all_lab_fields_nullable(self):
        """All new UI lab fields allow NULL."""
        pi = PatientInfo.objects.create(person=self.person)
        for field in (
            'hemoglobin_g_dl', 'hematocrit_percent', 'wbc_count_thousand_per_ul',
            'rbc_million_per_ul', 'platelet_count_thousand_per_ul',
            'anc_thousand_per_ul', 'alc_thousand_per_ul', 'amc_thousand_per_ul',
            'serum_calcium_mg_dl', 'serum_creatinine_mg_dl', 'creatinine_clearance_ml_min',
            'egfr_ml_min_173m2', 'bun_mg_dl', 'sodium_meq_l', 'potassium_meq_l',
            'magnesium_mg_dl', 'bilirubin_total_mg_dl', 'alt_u_l', 'ast_u_l',
            'alkaline_phosphatase_u_l', 'albumin_g_dl', 'total_protein',
            'troponin_ng_ml', 'bnp_pg_ml', 'glucose_mg_dl', 'hba1c_percent', 'ldh_u_l',
            'beta2_microglobulin', 'c_reactive_protein', 'esr',
        ):
            self.assertIsNone(
                getattr(pi, field),
                f'{field} should be NULL on a freshly created PatientInfo',
            )

    def test_one_to_one_constraint(self):
        """Two PatientInfo rows for the same Person are rejected."""
        from django.db import IntegrityError
        PatientInfo.objects.create(person=self.person)
        with self.assertRaises(IntegrityError):
            PatientInfo.objects.create(person=self.person)

    def test_cbc_fields_persist_with_correct_precision(self):
        """CBC decimal fields store at the declared precision."""
        pi = PatientInfo.objects.create(
            person=self.person,
            hemoglobin_g_dl=12.3,
            platelet_count_thousand_per_ul=250.5,
            anc_thousand_per_ul=3.7,
        )
        pi.refresh_from_db()
        self.assertAlmostEqual(float(pi.hemoglobin_g_dl), 12.3, places=1)
        self.assertAlmostEqual(float(pi.platelet_count_thousand_per_ul), 250.5, places=1)
        self.assertAlmostEqual(float(pi.anc_thousand_per_ul), 3.7, places=1)

    def test_lft_integer_fields_persist(self):
        """LFT integer fields (alt_u_l, ast_u_l, etc.) store correctly."""
        pi = PatientInfo.objects.create(
            person=self.person,
            alt_u_l=42,
            ast_u_l=38,
            alkaline_phosphatase_u_l=95,
            ldh_u_l=180,
        )
        pi.refresh_from_db()
        self.assertEqual(pi.alt_u_l, 42)
        self.assertEqual(pi.ast_u_l, 38)
        self.assertEqual(pi.alkaline_phosphatase_u_l, 95)
        self.assertEqual(pi.ldh_u_l, 180)


# ===========================================================================
# TEST-02: refresh_patient_info service unit tests
# ===========================================================================

class RefreshPatientInfoNewRecordTest(_OmopBase):
    """refresh_patient_info creates a PatientInfo when one does not exist."""

    PERSON_ID = 90200

    def test_creates_patient_info_when_absent(self):
        self.assertFalse(PatientInfo.objects.filter(person=self.person).exists())
        pi = refresh_patient_info(self.person)
        self.assertIsNotNone(pi)
        self.assertTrue(PatientInfo.objects.filter(person=self.person).exists())

    def test_returns_patient_info_instance(self):
        pi = refresh_patient_info(self.person)
        self.assertIsInstance(pi, PatientInfo)

    def test_idempotent_on_second_call(self):
        refresh_patient_info(self.person)
        refresh_patient_info(self.person)
        self.assertEqual(PatientInfo.objects.filter(person=self.person).count(), 1)


class RefreshPatientInfoDemographicsTest(_OmopBase):
    """Demographics section of refresh_patient_info."""

    PERSON_ID = 90210

    def test_age_derived_from_year_of_birth(self):
        pi = refresh_patient_info(self.person)
        expected_age = date.today().year - self.person.year_of_birth
        self.assertEqual(pi.patient_age, expected_age)


class RefreshPatientInfoDiseaseTest(_OmopBase):
    """Disease / condition section."""

    PERSON_ID = 90220

    def test_disease_derived_from_cancer_condition(self):
        ConditionOccurrence.objects.create(
            condition_occurrence_id=92201,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2022, 1, 1),
            condition_type_concept=self.type_concept,
        )
        pi = refresh_patient_info(self.person)
        self.assertIn('neoplasm', pi.disease.lower())

    def test_diagnosis_date_from_condition(self):
        ConditionOccurrence.objects.create(
            condition_occurrence_id=92202,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2021, 6, 15),
            condition_type_concept=self.type_concept,
        )
        pi = refresh_patient_info(self.person)
        self.assertEqual(pi.diagnosis_date, date(2021, 6, 15))

    def test_disease_slug_generated(self):
        ConditionOccurrence.objects.create(
            condition_occurrence_id=92203,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2022, 1, 1),
            condition_type_concept=self.type_concept,
        )
        pi = refresh_patient_info(self.person)
        self.assertIsNotNone(pi.disease_slug)
        self.assertNotIn(' ', pi.disease_slug)


class RefreshPatientInfoLabsFromMeasurementTest(_OmopBase):
    """Labs are derived from Measurement records using source_value fallback."""

    PERSON_ID = 90230

    def _make_measurement(self, mid, source_value, value):
        generic_concept = _concept(3000963, 'Laboratory test result', self.dom_meas, self.vocab, self.cc)
        return Measurement.objects.create(
            measurement_id=mid,
            person=self.person,
            measurement_concept=generic_concept,
            measurement_date=date(2023, 5, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=value,
            measurement_source_value=source_value,
        )

    def test_hemoglobin_derived_from_measurement_source_value(self):
        self._make_measurement(92301, 'Hemoglobin [Mass/volume] in Blood', 11.2)
        pi = refresh_patient_info(self.person)
        self.assertIsNotNone(pi.hemoglobin_g_dl)
        self.assertAlmostEqual(float(pi.hemoglobin_g_dl), 11.2, places=1)

    def test_wbc_derived_from_measurement_source_value(self):
        self._make_measurement(92302, 'Leukocytes [#/volume] in Blood', 4.5)
        pi = refresh_patient_info(self.person)
        self.assertIsNotNone(pi.wbc_count_thousand_per_ul)
        self.assertAlmostEqual(float(pi.wbc_count_thousand_per_ul), 4.5, places=1)

    def test_creatinine_derived_from_measurement_source_value(self):
        self._make_measurement(92303, 'Creatinine [Mass/volume] in Serum or Plasma', 0.9)
        pi = refresh_patient_info(self.person)
        self.assertIsNotNone(pi.serum_creatinine_mg_dl)
        self.assertAlmostEqual(float(pi.serum_creatinine_mg_dl), 0.9, places=1)

    def test_alt_derived_from_measurement_source_value(self):
        self._make_measurement(92304, 'Alanine aminotransferase [Enzymatic activity/volum', 55)
        pi = refresh_patient_info(self.person)
        self.assertIsNotNone(pi.alt_u_l)
        self.assertEqual(pi.alt_u_l, 55)

    def test_more_recent_measurement_wins(self):
        """Most-recent measurement_date should be used."""
        generic = _concept(3000963, 'Laboratory test result', self.dom_meas, self.vocab, self.cc)
        Measurement.objects.create(
            measurement_id=92310,
            person=self.person,
            measurement_concept=generic,
            measurement_date=date(2023, 1, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=9.0,
            measurement_source_value='Hemoglobin [Mass/volume] in Blood',
        )
        Measurement.objects.create(
            measurement_id=92311,
            person=self.person,
            measurement_concept=generic,
            measurement_date=date(2023, 6, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=13.5,
            measurement_source_value='Hemoglobin [Mass/volume] in Blood',
        )
        pi = refresh_patient_info(self.person)
        self.assertAlmostEqual(float(pi.hemoglobin_g_dl), 13.5, places=1)

    def test_cleared_measurement_clears_lab_field(self):
        """Deleting the only Measurement clears the derived field."""
        m = self._make_measurement(92320, 'Hemoglobin [Mass/volume] in Blood', 11.0)
        pi = refresh_patient_info(self.person)
        self.assertIsNotNone(pi.hemoglobin_g_dl)

        m.delete()
        pi = refresh_patient_info(self.person)
        # hemoglobin_g_dl is in _OMOP_DERIVED_FIELDS so it should be cleared
        self.assertIsNone(pi.hemoglobin_g_dl)


class RefreshPatientInfoComputedFieldsTest(_OmopBase):
    """_compute_derived_fields section."""

    PERSON_ID = 90240

    def test_measurable_disease_imwg_true_with_high_serum_mp(self):
        pi = PatientInfo.objects.create(
            person=self.person,
            monoclonal_protein_serum=1.5,
        )
        from omop_core.services.patient_info_service import _compute_derived_fields
        _compute_derived_fields(pi)
        self.assertTrue(pi.measurable_disease_imwg)

    def test_measurable_disease_imwg_false_with_low_values(self):
        pi = PatientInfo.objects.create(
            person=self.person,
            monoclonal_protein_serum=0.1,
            monoclonal_protein_urine=50,
        )
        from omop_core.services.patient_info_service import _compute_derived_fields
        _compute_derived_fields(pi)
        self.assertFalse(pi.measurable_disease_imwg)

    def test_measurable_disease_imwg_none_when_no_data(self):
        pi = PatientInfo.objects.create(person=self.person)
        from omop_core.services.patient_info_service import _compute_derived_fields
        _compute_derived_fields(pi)
        self.assertIsNone(pi.measurable_disease_imwg)


# ===========================================================================
# TEST-03: Signal integration tests at omop_core level
# ===========================================================================

class MeasurementSignalLabFieldTest(_OmopBase):
    """Saving a Measurement triggers refresh_patient_info and populates lab fields."""

    PERSON_ID = 90300

    def _measurement_concept(self):
        return _concept(3000963, 'Laboratory test result', self.dom_meas, self.vocab, self.cc)

    def test_measurement_save_updates_hemoglobin_g_dl(self):
        """Saving a Measurement with the right source_value updates hemoglobin_g_dl."""
        PatientInfo.objects.create(person=self.person)
        Measurement.objects.create(
            measurement_id=93001,
            person=self.person,
            measurement_concept=self._measurement_concept(),
            measurement_date=date(2023, 3, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=10.8,
            measurement_source_value='Hemoglobin [Mass/volume] in Blood',
        )
        pi = PatientInfo.objects.get(person=self.person)
        self.assertIsNotNone(pi.hemoglobin_g_dl)
        self.assertAlmostEqual(float(pi.hemoglobin_g_dl), 10.8, places=1)

    def test_measurement_delete_clears_hemoglobin_g_dl(self):
        """Deleting the Measurement clears the derived field."""
        PatientInfo.objects.create(person=self.person)
        m = Measurement.objects.create(
            measurement_id=93010,
            person=self.person,
            measurement_concept=self._measurement_concept(),
            measurement_date=date(2023, 3, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=10.8,
            measurement_source_value='Hemoglobin [Mass/volume] in Blood',
        )
        pi = PatientInfo.objects.get(person=self.person)
        self.assertIsNotNone(pi.hemoglobin_g_dl)

        m.delete()
        pi.refresh_from_db()
        self.assertIsNone(pi.hemoglobin_g_dl)

    def test_skip_flag_suppresses_refresh(self):
        """_skip_patient_info_refresh=True prevents refresh_patient_info from running."""
        PatientInfo.objects.create(person=self.person, hemoglobin_g_dl=99.0)
        m = Measurement(
            measurement_id=93020,
            person=self.person,
            measurement_concept=self._measurement_concept(),
            measurement_date=date(2023, 3, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=5.0,
            measurement_source_value='Hemoglobin [Mass/volume] in Blood',
        )
        m._skip_patient_info_refresh = True
        m.save()
        # PatientInfo should NOT have been updated
        pi = PatientInfo.objects.get(person=self.person)
        self.assertAlmostEqual(float(pi.hemoglobin_g_dl), 99.0, places=0)


class ConditionSignalTest(_OmopBase):
    """Saving a ConditionOccurrence triggers refresh_patient_info."""

    PERSON_ID = 90310

    def test_condition_save_updates_disease(self):
        PatientInfo.objects.create(person=self.person)
        ConditionOccurrence.objects.create(
            condition_occurrence_id=93101,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2022, 1, 1),
            condition_type_concept=self.type_concept,
        )
        pi = PatientInfo.objects.get(person=self.person)
        self.assertIsNotNone(pi.disease)
        self.assertIn('neoplasm', pi.disease.lower())

    def test_condition_delete_clears_disease(self):
        PatientInfo.objects.create(person=self.person)
        co = ConditionOccurrence.objects.create(
            condition_occurrence_id=93110,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2022, 1, 1),
            condition_type_concept=self.type_concept,
        )
        pi = PatientInfo.objects.get(person=self.person)
        self.assertIsNotNone(pi.disease)

        co.delete()
        pi.refresh_from_db()
        self.assertIsNone(pi.disease)
