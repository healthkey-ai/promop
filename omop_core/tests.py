"""
omop_core tests — TEST-01, TEST-02, TEST-03, TEST-04

TEST-01: PatientInfo model-level tests
TEST-02: refresh_patient_info service unit tests
TEST-03: Signal integration tests at omop_core level
TEST-04: FLBundleGenerator unit tests
"""

from datetime import date
from unittest.mock import patch

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


class CanonicalizeDiseaseTest(_OmopBase):
    """Raw OMOP concept names are mapped to EXACT's canonical disease titles."""

    PERSON_ID = 90225

    def test_canonicalize_helper_maps_known_aliases(self):
        from omop_core.services.patient_info_service import _canonicalize_disease
        self.assertEqual(_canonicalize_disease('myeloma'), 'multiple myeloma')
        self.assertEqual(_canonicalize_disease('Myeloma'), 'multiple myeloma')
        self.assertEqual(_canonicalize_disease('  MYELOMA  '), 'multiple myeloma')

    def test_canonicalize_helper_passes_through_unknown(self):
        from omop_core.services.patient_info_service import _canonicalize_disease
        self.assertEqual(_canonicalize_disease('breast cancer'), 'breast cancer')
        self.assertEqual(_canonicalize_disease(''), '')
        self.assertIsNone(_canonicalize_disease(None))

    def test_refresh_canonicalizes_bare_myeloma_condition(self):
        myeloma_concept = _concept(90002, 'myeloma', self.dom_cond, self.vocab, self.cc)
        ConditionOccurrence.objects.create(
            condition_occurrence_id=92204,
            person=self.person,
            condition_concept=myeloma_concept,
            condition_start_date=date(2022, 3, 1),
            condition_type_concept=self.type_concept,
        )
        pi = refresh_patient_info(self.person)
        self.assertEqual(pi.disease, 'multiple myeloma')
        self.assertEqual(pi.disease_slug, 'multiple-myeloma')


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


# ---------------------------------------------------------------------------
# TEST-04: get_visible_orgs access helper
# ---------------------------------------------------------------------------

from django.utils import timezone
from datetime import timedelta
from omop_core.models import Organization, PatientGroup, GroupAccess
from omop_core.services.access import get_visible_orgs
from patient_portal.models import Identity


class GetVisibleOrgsTest(TestCase):
    def setUp(self):
        self.org_a = Organization.objects.create(name='Org A', slug='org-a')
        self.org_b = Organization.objects.create(name='Org B', slug='org-b')
        self.group_a = PatientGroup.objects.create(
            organization=self.org_a, name='Group A', slug='group-a'
        )
        self.staff_user = Identity.objects.create_user(
            email='staff@test.com', password='x', is_staff=True
        )
        self.org_admin = Identity.objects.create_user(
            email='orgadmin@test.com', password='x'
        )
        self.doctor = Identity.objects.create_user(
            email='doctor@test.com', password='x'
        )
        self.nobody = Identity.objects.create_user(
            email='nobody@test.com', password='x'
        )
        GroupAccess.objects.create(
            identity=self.org_admin, org=self.org_a, role='org_admin'
        )
        GroupAccess.objects.create(
            identity=self.doctor, group=self.group_a, role='doctor'
        )

    def test_staff_sees_all_orgs(self):
        orgs = get_visible_orgs(self.staff_user)
        self.assertIn(self.org_a, orgs)
        self.assertIn(self.org_b, orgs)

    def test_org_admin_sees_their_org_only(self):
        orgs = list(get_visible_orgs(self.org_admin))
        self.assertIn(self.org_a, orgs)
        self.assertNotIn(self.org_b, orgs)

    def test_doctor_sees_org_of_their_group(self):
        orgs = list(get_visible_orgs(self.doctor))
        self.assertIn(self.org_a, orgs)
        self.assertNotIn(self.org_b, orgs)

    def test_user_with_no_grants_sees_nothing(self):
        orgs = list(get_visible_orgs(self.nobody))
        self.assertEqual(orgs, [])

    def test_expired_grant_excluded(self):
        expired = Identity.objects.create_user(email='expired@test.com', password='x')
        GroupAccess.objects.create(
            identity=expired, org=self.org_a, role='org_admin',
            expires_at=timezone.now() - timedelta(hours=1),
        )
        orgs = list(get_visible_orgs(expired))
        self.assertEqual(orgs, [])

    def test_active_grant_with_future_expiry_included(self):
        future = Identity.objects.create_user(email='future@test.com', password='x')
        GroupAccess.objects.create(
            identity=future, org=self.org_b, role='org_admin',
            expires_at=timezone.now() + timedelta(days=30),
        )
        orgs = list(get_visible_orgs(future))
        self.assertIn(self.org_b, orgs)

    def test_xor_constraint_prevents_both_org_and_group_set(self):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GroupAccess.objects.create(
                    identity=self.nobody, org=self.org_a, group=self.group_a, role='org_admin'
                )


# ---------------------------------------------------------------------------
# TEST-04: FLBundleGenerator
# ---------------------------------------------------------------------------

_FL_MOCK_CATALOG = [
    {
        'concept_id': 35804570,
        'concept_name': 'Bendamustine and Rituximab (BR)',
        'drugs': ['bendamustine', 'rituximab'],
    },
    {
        'concept_id': 35805028,
        'concept_name': 'R-CHOP',
        'drugs': ['cyclophosphamide', 'doxorubicin', 'prednisone', 'rituximab', 'vincristine'],
    },
    {
        'concept_id': 35805630,
        'concept_name': 'R-CVP',
        'drugs': ['cyclophosphamide', 'prednisone', 'rituximab', 'vincristine'],
    },
    {
        'concept_id': 35805634,
        'concept_name': 'G-CHOP',
        'drugs': ['cyclophosphamide', 'doxorubicin', 'obinutuzumab', 'prednisone', 'vincristine'],
    },
    {
        'concept_id': 35803432,
        'concept_name': 'Rituximab monotherapy',
        'drugs': ['rituximab'],
    },
    {
        'concept_id': 35804583,
        'concept_name': 'Obinutuzumab monotherapy',
        'drugs': ['obinutuzumab'],
    },
    {
        'concept_id': 35804591,
        'concept_name': 'Lenalidomide and Rituximab (R2)',
        'drugs': ['lenalidomide', 'rituximab'],
    },
    {
        'concept_id': 42542442,
        'concept_name': 'Tazemetostat monotherapy',
        'drugs': ['tazemetostat'],
    },
    {
        'concept_id': 37557146,
        'concept_name': 'Mosunetuzumab monotherapy',
        'drugs': ['mosunetuzumab'],
    },
    {
        'concept_id': 35805074,
        'concept_name': 'Axicabtagene ciloleucel monotherapy',
        'drugs': ['axicabtagene ciloleucel'],
    },
    {
        'concept_id': 37557451,
        'concept_name': 'Glofitamab monotherapy',
        'drugs': ['glofitamab'],
    },
    {
        'concept_id': 37557299,
        'concept_name': 'Epcoritamab monotherapy',
        'drugs': ['epcoritamab'],
    },
    {
        'concept_id': 35805647,
        'concept_name': 'Copanlisib monotherapy',
        'drugs': ['copanlisib'],
    },
    {
        'concept_id': 35804066,
        'concept_name': 'Tisagenlecleucel monotherapy',
        'drugs': ['tisagenlecleucel'],
    },
    {
        'concept_id': 35805062,
        'concept_name': 'R-GDP',
        'drugs': ['cisplatin', 'dexamethasone', 'gemcitabine', 'rituximab'],
    },
    {
        'concept_id': 35805082,
        'concept_name': 'R-GemOx',
        'drugs': ['gemcitabine', 'oxaliplatin', 'rituximab'],
    },
]

_MOCK_TARGET = 'omop_core.management.commands._fl_generator.load_hemonc_regimens_for_disease'


class FLBundleGeneratorTest(TestCase):
    """TEST-04: FLBundleGenerator — unit tests with mocked DB catalog."""

    @patch(_MOCK_TARGET, return_value=_FL_MOCK_CATALOG)
    def test_generate_bundle_structure(self, _mock):
        from omop_core.management.commands._fl_generator import FLBundleGenerator
        gen = FLBundleGenerator(watch_wait_ratio=0.0)
        bundle = gen.generate_bundle(5)
        self.assertEqual(bundle['resourceType'], 'Bundle')
        self.assertEqual(bundle['type'], 'collection')
        # Each patient contributes at minimum: Patient + Condition + labs + therapy resources
        self.assertGreater(len(bundle['entry']), 5)

    @patch(_MOCK_TARGET, return_value=_FL_MOCK_CATALOG)
    def test_every_entry_has_resource_type(self, _mock):
        from omop_core.management.commands._fl_generator import FLBundleGenerator
        gen = FLBundleGenerator(watch_wait_ratio=0.0)
        bundle = gen.generate_bundle(3)
        for entry in bundle['entry']:
            self.assertIn('resourceType', entry['resource'])

    @patch(_MOCK_TARGET, return_value=_FL_MOCK_CATALOG)
    def test_watch_and_wait_patients_have_no_therapy(self, _mock):
        """Patients in watch-and-wait should not produce MedicationStatement resources."""
        from omop_core.management.commands._fl_generator import FLBundleGenerator
        gen = FLBundleGenerator(watch_wait_ratio=1.0)  # force all eligible to watch-and-wait
        bundle = gen.generate_bundle(20)
        # Some patients may not be eligible for W&W (high FLIPI / B symptoms), so filter by extension
        waw_patient_ids = set()
        for entry in bundle['entry']:
            r = entry['resource']
            if r['resourceType'] != 'Patient':
                continue
            for ext in r.get('extension', []):
                if ext.get('url', '').endswith('fl-watch-and-wait') and ext.get('valueBoolean'):
                    waw_patient_ids.add(r['id'])
        med_patient_ids = {
            entry['resource']['subject']['reference'].split('/')[-1]
            for entry in bundle['entry']
            if entry['resource']['resourceType'] == 'MedicationStatement'
        }
        self.assertTrue(waw_patient_ids.isdisjoint(med_patient_ids),
                        "Watch-and-wait patients should have no MedicationStatements")

    @patch(_MOCK_TARGET, return_value=_FL_MOCK_CATALOG)
    def test_hemonc_concept_id_in_regimen_coding(self, _mock):
        """Regimen-level MedicationStatements must carry a HemOnc system coding."""
        from omop_core.management.commands._fl_generator import FLBundleGenerator
        gen = FLBundleGenerator(watch_wait_ratio=0.0)
        bundle = gen.generate_bundle(10)
        regimen_stmts = [
            e['resource'] for e in bundle['entry']
            if e['resource']['resourceType'] == 'MedicationStatement'
            and any(c.get('system') == 'http://ctomop.io/fhir/fl-regimen'
                    for c in e['resource']['medicationCodeableConcept']['coding'])
        ]
        self.assertGreater(len(regimen_stmts), 0, "Expected at least one regimen MedicationStatement")
        for stmt in regimen_stmts:
            systems = {c['system'] for c in stmt['medicationCodeableConcept']['coding']}
            # HemOnc coding should be present for DB-sourced regimens (not radiation)
            has_hemonc = 'http://ohdsi.org/omop/HemOnc' in systems
            is_radiation_only = systems == {'http://ctomop.io/fhir/fl-regimen'}
            self.assertTrue(has_hemonc or is_radiation_only,
                            f"Unexpected coding systems: {systems}")

    @patch(_MOCK_TARGET, return_value=[])
    def test_empty_catalog_raises_runtime_error(self, _mock):
        """Empty HemOnc catalog must raise RuntimeError with a clear message."""
        from omop_core.management.commands._fl_generator import FLBundleGenerator
        with self.assertRaisesRegex(RuntimeError, 'empty'):
            FLBundleGenerator()

    @patch(_MOCK_TARGET, return_value=_FL_MOCK_CATALOG)
    def test_lot_weights_produce_both_line_lists(self, _mock):
        """Both first_line and later_line regimen lists must be non-empty."""
        from omop_core.management.commands._fl_generator import FLBundleGenerator
        gen = FLBundleGenerator()
        self.assertGreater(len(gen._first_line_regimens), 0)
        self.assertGreater(len(gen._later_line_regimens), 0)

    @patch(_MOCK_TARGET, return_value=_FL_MOCK_CATALOG)
    def test_birth_year_is_current(self, _mock):
        """Generated Patient resources must have birth years close to today's year."""
        from datetime import date as _date
        from omop_core.management.commands._fl_generator import FLBundleGenerator
        gen = FLBundleGenerator(watch_wait_ratio=0.0)
        bundle = gen.generate_bundle(5)
        current_year = _date.today().year
        for entry in bundle['entry']:
            r = entry['resource']
            if r['resourceType'] != 'Patient':
                continue
            birth_year = int(r['birthDate'][:4])
            self.assertLessEqual(birth_year, current_year,
                                 f"Birth year {birth_year} is in the future")
            self.assertGreater(birth_year, current_year - 100,
                               f"Birth year {birth_year} seems too far in the past")
