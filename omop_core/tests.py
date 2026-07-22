"""
omop_core tests — TEST-01, TEST-02, TEST-03, TEST-04

TEST-01: PatientRecord model-level tests
TEST-02: refresh_patient_record service unit tests
TEST-03: Signal integration tests at omop_core level
TEST-04: FLBundleGenerator unit tests
"""

from datetime import date
from unittest.mock import patch

from django.test import TestCase

from omop_core.models import (
    Concept, ConceptClass, Domain, Vocabulary,
    Person, PatientRecord, ConditionOccurrence, DrugExposure, Measurement, Observation,
)
from omop_core.services.patient_record_service import refresh_patient_record


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
# TEST-01: PatientRecord model-level tests
# ===========================================================================

class PatientRecordModelTest(_OmopBase):
    """PatientRecord field persistence, nullability, and OneToOne constraint."""

    PERSON_ID = 90100

    def test_create_patient_info_with_basic_fields(self):
        """PatientRecord can be created and fields persist to the DB."""
        pi = PatientRecord.objects.create(
            person=self.person,
            disease='Breast Cancer',
            hemoglobin_g_dl=11.5,
            wbc_count_thousand_per_ul=4.2,
        )
        fetched = PatientRecord.objects.get(pk=pi.pk)
        self.assertEqual(fetched.disease, 'Breast Cancer')
        self.assertAlmostEqual(float(fetched.hemoglobin_g_dl), 11.5, places=1)
        self.assertAlmostEqual(float(fetched.wbc_count_thousand_per_ul), 4.2, places=1)

    def test_all_lab_fields_nullable(self):
        """All new UI lab fields allow NULL."""
        pi = PatientRecord.objects.create(person=self.person)
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
                f'{field} should be NULL on a freshly created PatientRecord',
            )

    def test_one_to_one_constraint(self):
        """Two PatientRecord rows for the same Person are rejected."""
        from django.db import IntegrityError
        PatientRecord.objects.create(person=self.person)
        with self.assertRaises(IntegrityError):
            PatientRecord.objects.create(person=self.person)

    def test_cbc_fields_persist_with_correct_precision(self):
        """CBC decimal fields store at the declared precision."""
        pi = PatientRecord.objects.create(
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
        pi = PatientRecord.objects.create(
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
# TEST-02: refresh_patient_record service unit tests
# ===========================================================================

class RefreshPatientRecordNewRecordTest(_OmopBase):
    """refresh_patient_record creates a PatientRecord when one does not exist."""

    PERSON_ID = 90200

    def test_creates_patient_info_when_absent(self):
        self.assertFalse(PatientRecord.objects.filter(person=self.person).exists())
        pi = refresh_patient_record(self.person)
        self.assertIsNotNone(pi)
        self.assertTrue(PatientRecord.objects.filter(person=self.person).exists())

    def test_returns_patient_info_instance(self):
        pi = refresh_patient_record(self.person)
        self.assertIsInstance(pi, PatientRecord)

    def test_idempotent_on_second_call(self):
        refresh_patient_record(self.person)
        refresh_patient_record(self.person)
        self.assertEqual(PatientRecord.objects.filter(person=self.person).count(), 1)


class RefreshPatientRecordDemographicsTest(_OmopBase):
    """Demographics section of refresh_patient_record."""

    PERSON_ID = 90210

    def test_age_derived_from_year_of_birth(self):
        pi = refresh_patient_record(self.person)
        expected_age = date.today().year - self.person.year_of_birth
        self.assertEqual(pi.patient_age, expected_age)


class RefreshPatientRecordDiseaseTest(_OmopBase):
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
        pi = refresh_patient_record(self.person)
        self.assertEqual(pi.disease, 'Breast Cancer')

    def test_diagnosis_date_from_condition(self):
        ConditionOccurrence.objects.create(
            condition_occurrence_id=92202,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2021, 6, 15),
            condition_type_concept=self.type_concept,
        )
        pi = refresh_patient_record(self.person)
        self.assertEqual(pi.diagnosis_date, date(2021, 6, 15))

    def test_disease_slug_generated(self):
        ConditionOccurrence.objects.create(
            condition_occurrence_id=92203,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2022, 1, 1),
            condition_type_concept=self.type_concept,
        )
        pi = refresh_patient_record(self.person)
        self.assertIsNotNone(pi.disease_slug)
        self.assertNotIn(' ', pi.disease_slug)


class CanonicalizeDiseaseTest(_OmopBase):
    """Raw OMOP concept names are mapped to EXACT's canonical disease titles."""

    PERSON_ID = 90225

    def test_canonicalize_helper_maps_known_aliases(self):
        from omop_core.services.patient_record_service import _canonicalize_disease
        self.assertEqual(_canonicalize_disease('myeloma'), 'multiple myeloma')
        self.assertEqual(_canonicalize_disease('Myeloma'), 'multiple myeloma')
        self.assertEqual(_canonicalize_disease('  MYELOMA  '), 'multiple myeloma')
        self.assertEqual(_canonicalize_disease('breast cancer'), 'Breast Cancer')
        self.assertEqual(_canonicalize_disease('Breast cancer'), 'Breast Cancer')
        self.assertEqual(_canonicalize_disease('Breast Cancer (disorder)'), 'Breast Cancer')
        self.assertEqual(_canonicalize_disease('ER|ERBB2 Breast cancer'), 'Breast Cancer')
        self.assertEqual(_canonicalize_disease('ER|ERBB2 Breast cancer (disorder)'), 'Breast Cancer')

    def test_canonicalize_helper_passes_through_unknown(self):
        from omop_core.services.patient_record_service import _canonicalize_disease
        self.assertEqual(_canonicalize_disease('pancreatic cancer'), 'pancreatic cancer')
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
        pi = refresh_patient_record(self.person)
        self.assertEqual(pi.disease, 'multiple myeloma')
        self.assertEqual(pi.disease_slug, 'multiple-myeloma')


class RefreshPatientRecordLabsFromMeasurementTest(_OmopBase):
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
        pi = refresh_patient_record(self.person)
        self.assertIsNotNone(pi.hemoglobin_g_dl)
        self.assertAlmostEqual(float(pi.hemoglobin_g_dl), 11.2, places=1)

    def test_wbc_derived_from_measurement_source_value(self):
        self._make_measurement(92302, 'Leukocytes [#/volume] in Blood', 4.5)
        pi = refresh_patient_record(self.person)
        self.assertIsNotNone(pi.wbc_count_thousand_per_ul)
        self.assertAlmostEqual(float(pi.wbc_count_thousand_per_ul), 4.5, places=1)

    def test_creatinine_derived_from_measurement_source_value(self):
        self._make_measurement(92303, 'Creatinine [Mass/volume] in Serum or Plasma', 0.9)
        pi = refresh_patient_record(self.person)
        self.assertIsNotNone(pi.serum_creatinine_mg_dl)
        self.assertAlmostEqual(float(pi.serum_creatinine_mg_dl), 0.9, places=1)

    def test_alt_derived_from_measurement_source_value(self):
        self._make_measurement(92304, 'Alanine aminotransferase [Enzymatic activity/volum', 55)
        pi = refresh_patient_record(self.person)
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
        pi = refresh_patient_record(self.person)
        self.assertAlmostEqual(float(pi.hemoglobin_g_dl), 13.5, places=1)

    def test_cleared_measurement_clears_lab_field(self):
        """Deleting the only Measurement clears the derived field."""
        m = self._make_measurement(92320, 'Hemoglobin [Mass/volume] in Blood', 11.0)
        pi = refresh_patient_record(self.person)
        self.assertIsNotNone(pi.hemoglobin_g_dl)

        m.delete()
        pi = refresh_patient_record(self.person)
        # hemoglobin_g_dl is in _OMOP_DERIVED_FIELDS so it should be cleared
        self.assertIsNone(pi.hemoglobin_g_dl)


class RefreshPatientRecordReceptorStatusTest(_OmopBase):
    """HER2/ER/PR receptor status derivation from Measurement rows (issue #220)."""

    PERSON_ID = 90240

    def _make_her2(self, mid, *, value_as_string=None, value_source_value=None,
                   value_as_concept=None):
        her2_concept = _concept(
            9048676, 'HER2 [Interpretation] in Tissue',
            self.dom_meas, self.vocab, self.cc, code='48676-1',
        )
        return Measurement.objects.create(
            measurement_id=mid,
            person=self.person,
            measurement_concept=her2_concept,
            measurement_date=date(2023, 5, 1),
            measurement_type_concept=self.type_concept,
            value_as_string=value_as_string,
            value_source_value=value_source_value,
            value_as_concept=value_as_concept,
            measurement_source_value='48676-1',
        )

    def test_her2_positive(self):
        self._make_her2(92401, value_as_string='Positive')
        pi = refresh_patient_record(self.person)
        self.assertEqual(pi.her2_status, 'POSITIVE')

    def test_her2_negative(self):
        self._make_her2(92402, value_as_string='Negative')
        pi = refresh_patient_record(self.person)
        self.assertEqual(pi.her2_status, 'NEGATIVE')

    def test_her2_equivocal_is_preserved_not_dropped(self):
        """Regression for #220: an 'Equivocal' HER2 result must not be dropped."""
        self._make_her2(92403, value_as_string='Equivocal')
        pi = refresh_patient_record(self.person)
        self.assertEqual(pi.her2_status, 'EQUIVOCAL')

    def test_her2_value_in_source_value_is_read(self):
        """HER2 result stored only in value_source_value is still derived."""
        self._make_her2(92404, value_source_value='Equivocal')
        pi = refresh_patient_record(self.person)
        self.assertEqual(pi.her2_status, 'EQUIVOCAL')

    def test_her2_missing_value_yields_none(self):
        """No value at all still yields None (no spurious status)."""
        self._make_her2(92405)
        pi = refresh_patient_record(self.person)
        self.assertIsNone(pi.her2_status)

    def test_her2_value_as_concept_is_read(self):
        """HER2 result carried in value_as_concept is derived (concept-first branch)."""
        pos_concept = _concept(9000201, 'Positive', self.dom_meas, self.vocab, self.cc)
        self._make_her2(92406, value_as_concept=pos_concept)
        pi = refresh_patient_record(self.person)
        self.assertEqual(pi.her2_status, 'POSITIVE')

    def test_her2_nonstandard_value_preserved(self):
        """A non-standard receptor value is preserved (upper-cased), not dropped."""
        self._make_her2(92407, value_as_string='Indeterminate')
        pi = refresh_patient_record(self.person)
        self.assertEqual(pi.her2_status, 'INDETERMINATE')


class CdmComplianceTablesTest(_OmopBase):
    """Standard OMOP CDM 5.4 tables added for CDM-compliance (cdm-compliance branch)."""

    PERSON_ID = 90260

    def test_cdm_source_seeded_as_54(self):
        """The seed migration self-describes the instance as CDM 5.4."""
        from omop_core.models import CdmSource
        row = CdmSource.objects.filter(cdm_source_abbreviation='PRomop').first()
        self.assertIsNotNone(row)
        self.assertEqual(row.cdm_version, '5.4')

    def test_vocabulary_tables_writable(self):
        """drug_strength / concept_synonym / source_to_concept_map accept rows."""
        from omop_core.models import ConceptSynonym, DrugStrength, SourceToConceptMap
        lang = _concept(90261, 'English language', self.dom_meas, self.vocab, self.cc)
        DrugStrength.objects.create(
            drug_concept=self.drug_concept, ingredient_concept=self.drug_concept,
            amount_value=10.0, valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        ConceptSynonym.objects.create(
            concept=self.drug_concept, concept_synonym_name='Adriamycin', language_concept=lang,
        )
        SourceToConceptMap.objects.create(
            source_code='X', source_concept=self.drug_concept, source_vocabulary_id='LOCAL',
            target_concept=self.drug_concept, target_vocabulary_id='RxNorm',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.assertEqual(DrugStrength.objects.count(), 1)
        self.assertEqual(ConceptSynonym.objects.count(), 1)
        self.assertEqual(SourceToConceptMap.objects.count(), 1)


class LoadAthenaVocabExtraTablesTest(_OmopBase):
    """load_athena_vocabularies loaders for concept_synonym and drug_strength (#223)."""

    PERSON_ID = 90280

    def _run_loader(self, method_name, filename, header, rows):
        import os
        import tempfile
        from io import StringIO
        from omop_core.management.commands.load_athena_vocabularies import Command
        d = tempfile.mkdtemp()
        with open(os.path.join(d, filename), 'w', encoding='utf-8', newline='') as f:
            f.write('\t'.join(header) + '\n')
            for r in rows:
                f.write('\t'.join(str(x) for x in r) + '\n')
        cmd = Command(stdout=StringIO())
        cmd._base = d
        cmd._gcs_bucket = None
        cmd._direct = False
        return getattr(cmd, method_name)(False)

    def test_concept_synonym_loads_and_filters_unloaded_refs(self):
        from omop_core.models import ConceptSynonym
        _concept(4180186, 'English language', self.dom_meas, self.vocab, self.cc)
        drug = _concept(950001, 'Doxorubicin', self.dom_drug, self.vocab, self.cc, code='1790')
        self._run_loader(
            '_load_concept_synonym', 'CONCEPT_SYNONYM.csv',
            ['concept_id', 'concept_synonym_name', 'language_concept_id'],
            [
                (950001, 'Adriamycin', 4180186),   # valid
                (999999, 'Ghost', 4180186),         # concept not loaded -> skip
                (950001, 'BadLang', 888888),        # language not loaded -> skip
            ],
        )
        names = list(ConceptSynonym.objects.values_list('concept_synonym_name', flat=True))
        self.assertEqual(names, ['Adriamycin'])

    def test_drug_strength_loads_and_nulls_unloaded_unit(self):
        from omop_core.models import DrugStrength
        _concept(950001, 'Doxorubicin', self.dom_drug, self.vocab, self.cc, code='1790')
        _concept(950002, 'Doxorubicin 2 MG/ML', self.dom_drug, self.vocab, self.cc, code='1791')
        _concept(8576, 'milligram', self.dom_meas, self.vocab, self.cc)
        header = ['drug_concept_id', 'ingredient_concept_id', 'amount_value',
                  'amount_unit_concept_id', 'numerator_value', 'numerator_unit_concept_id',
                  'denominator_value', 'denominator_unit_concept_id', 'box_size',
                  'valid_start_date', 'valid_end_date', 'invalid_reason']
        self._run_loader(
            '_load_drug_strength', 'DRUG_STRENGTH.csv', header,
            [
                (950001, 950001, 10, 8576, '', '', '', '', '', '19700101', '20991231', ''),
                (999999, 950001, 5, 8576, '', '', '', '', '', '19700101', '20991231', ''),   # drug not loaded -> skip
                (950002, 950001, 20, 777777, '', '', '', '', '', '19700101', '20991231', ''),  # unit not loaded -> NULL
            ],
        )
        self.assertEqual(DrugStrength.objects.count(), 2)
        loaded_unit = DrugStrength.objects.get(amount_value=10.0)
        self.assertEqual(loaded_unit.amount_unit_concept_id, 8576)
        nulled_unit = DrugStrength.objects.get(amount_value=20.0)
        self.assertIsNone(nulled_unit.amount_unit_concept_id)

    def test_loader_rerun_is_idempotent(self):
        """Re-running the loaders without --replace must not duplicate rows."""
        from omop_core.models import ConceptSynonym, DrugStrength
        _concept(4180186, 'English language', self.dom_meas, self.vocab, self.cc)
        _concept(950001, 'Doxorubicin', self.dom_drug, self.vocab, self.cc, code='1790')
        self._run_loader(
            '_load_concept_synonym', 'CONCEPT_SYNONYM.csv',
            ['concept_id', 'concept_synonym_name', 'language_concept_id'],
            [(950001, 'Adriamycin', 4180186)],
        )
        self._run_loader(
            '_load_concept_synonym', 'CONCEPT_SYNONYM.csv',
            ['concept_id', 'concept_synonym_name', 'language_concept_id'],
            [(950001, 'Adriamycin', 4180186)],
        )
        self.assertEqual(ConceptSynonym.objects.count(), 1)
        ds_header = ['drug_concept_id', 'ingredient_concept_id', 'amount_value',
                     'amount_unit_concept_id', 'numerator_value', 'numerator_unit_concept_id',
                     'denominator_value', 'denominator_unit_concept_id', 'box_size',
                     'valid_start_date', 'valid_end_date', 'invalid_reason']
        ds_rows = [(950001, 950001, 10, '', '', '', '', '', '', '19700101', '20991231', '')]
        self._run_loader('_load_drug_strength', 'DRUG_STRENGTH.csv', ds_header, ds_rows)
        self._run_loader('_load_drug_strength', 'DRUG_STRENGTH.csv', ds_header, ds_rows)
        self.assertEqual(DrugStrength.objects.count(), 1)

    def test_vocabulary_none_row_loaded_for_cdm_version(self):
        """The out-of-scope 'None' VOCABULARY.csv row is kept so cdm_source gets a version."""
        from omop_core.models import Vocabulary
        self._run_loader(
            '_load_vocabularies', 'VOCABULARY.csv',
            ['vocabulary_id', 'vocabulary_name', 'vocabulary_reference',
             'vocabulary_version', 'vocabulary_concept_id'],
            [
                ('None', 'OMOP CDM vocabulary', 'https://athena.ohdsi.org', 'v5.4 01-JAN-26', 756265),
                ('NotInScope', 'Some vocab', '', 'v1', 1),
            ],
        )
        row = Vocabulary.objects.get(vocabulary_id='None')
        self.assertEqual(row.vocabulary_version, 'v5.4 01-JAN-26')
        self.assertFalse(Vocabulary.objects.filter(vocabulary_id='NotInScope').exists())

    def test_sync_cdm_source_recreates_missing_row(self):
        """_sync_cdm_source_metadata re-seeds the row wiped by --replace TRUNCATE CASCADE."""
        from io import StringIO
        from omop_core.management.commands.load_athena_vocabularies import Command
        from omop_core.models import CdmSource
        CdmSource.objects.filter(cdm_source_abbreviation='PRomop').delete()
        self.assertFalse(CdmSource.objects.filter(cdm_source_abbreviation='PRomop').exists())
        cmd = Command(stdout=StringIO())
        cmd._sync_cdm_source_metadata()
        row = CdmSource.objects.get(cdm_source_abbreviation='PRomop')
        self.assertEqual(row.cdm_version, '5.4')


class PopulateObservationPeriodTest(_OmopBase):
    """observation_period derivation from clinical-event spans."""

    PERSON_ID = 90270

    def test_period_spans_earliest_to_latest_event(self):
        from django.core.management import call_command
        from omop_core.models import Measurement, Observation, ObservationPeriod
        # ensure the EHR type concept exists so the command links it
        _concept(32817, 'EHR', self.type_concept.domain, self.vocab, self.cc)
        generic = _concept(3000963, 'Laboratory test result', self.dom_meas, self.vocab, self.cc)
        Measurement.objects.create(
            measurement_id=93001, person=self.person, measurement_concept=generic,
            measurement_date=date(2021, 3, 1), measurement_type_concept=self.type_concept,
            value_as_number=10, measurement_source_value='Hemoglobin',
        )
        Observation.objects.create(
            observation_id=93002, person=self.person, observation_concept=generic,
            observation_date=date(2023, 9, 15), observation_type_concept=self.type_concept,
        )
        call_command('populate_observation_period')
        periods = ObservationPeriod.objects.filter(person=self.person)
        self.assertEqual(periods.count(), 1)
        p = periods.first()
        self.assertEqual(p.observation_period_start_date, date(2021, 3, 1))
        self.assertEqual(p.observation_period_end_date, date(2023, 9, 15))
        self.assertEqual(p.period_type_concept_id, 32817)

    def test_rerun_without_overwrite_is_idempotent(self):
        from django.core.management import call_command
        from omop_core.models import Measurement, ObservationPeriod
        generic = _concept(3000963, 'Laboratory test result', self.dom_meas, self.vocab, self.cc)
        Measurement.objects.create(
            measurement_id=93010, person=self.person, measurement_concept=generic,
            measurement_date=date(2022, 1, 1), measurement_type_concept=self.type_concept,
            value_as_number=1, measurement_source_value='Hemoglobin',
        )
        call_command('populate_observation_period')
        call_command('populate_observation_period')
        self.assertEqual(ObservationPeriod.objects.filter(person=self.person).count(), 1)


class RefreshPatientRecordComputedFieldsTest(_OmopBase):
    """_compute_derived_fields section."""

    PERSON_ID = 90240

    def test_measurable_disease_imwg_true_with_high_serum_mp(self):
        pi = PatientRecord.objects.create(
            person=self.person,
            monoclonal_protein_serum=1.5,
        )
        from omop_core.services.patient_record_service import _compute_derived_fields
        _compute_derived_fields(pi)
        self.assertTrue(pi.measurable_disease_imwg)

    def test_measurable_disease_imwg_false_with_low_values(self):
        pi = PatientRecord.objects.create(
            person=self.person,
            monoclonal_protein_serum=0.1,
            monoclonal_protein_urine=50,
        )
        from omop_core.services.patient_record_service import _compute_derived_fields
        _compute_derived_fields(pi)
        self.assertFalse(pi.measurable_disease_imwg)

    def test_measurable_disease_imwg_none_when_no_data(self):
        pi = PatientRecord.objects.create(person=self.person)
        from omop_core.services.patient_record_service import _compute_derived_fields
        _compute_derived_fields(pi)
        self.assertIsNone(pi.measurable_disease_imwg)


# ===========================================================================
# TEST-03: Signal integration tests at omop_core level
# ===========================================================================

class MeasurementSignalLabFieldTest(_OmopBase):
    """Saving a Measurement triggers refresh_patient_record and populates lab fields."""

    PERSON_ID = 90300

    def _measurement_concept(self):
        return _concept(3000963, 'Laboratory test result', self.dom_meas, self.vocab, self.cc)

    def test_measurement_save_updates_hemoglobin_g_dl(self):
        """Saving a Measurement with the right source_value updates hemoglobin_g_dl."""
        PatientRecord.objects.create(person=self.person)
        Measurement.objects.create(
            measurement_id=93001,
            person=self.person,
            measurement_concept=self._measurement_concept(),
            measurement_date=date(2023, 3, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=10.8,
            measurement_source_value='Hemoglobin [Mass/volume] in Blood',
        )
        pi = PatientRecord.objects.get(person=self.person)
        self.assertIsNotNone(pi.hemoglobin_g_dl)
        self.assertAlmostEqual(float(pi.hemoglobin_g_dl), 10.8, places=1)

    def test_measurement_delete_clears_hemoglobin_g_dl(self):
        """Deleting the Measurement clears the derived field."""
        PatientRecord.objects.create(person=self.person)
        m = Measurement.objects.create(
            measurement_id=93010,
            person=self.person,
            measurement_concept=self._measurement_concept(),
            measurement_date=date(2023, 3, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=10.8,
            measurement_source_value='Hemoglobin [Mass/volume] in Blood',
        )
        pi = PatientRecord.objects.get(person=self.person)
        self.assertIsNotNone(pi.hemoglobin_g_dl)

        m.delete()
        pi.refresh_from_db()
        self.assertIsNone(pi.hemoglobin_g_dl)

    def test_skip_flag_suppresses_refresh(self):
        """_skip_patient_record_refresh=True prevents refresh_patient_record from running."""
        PatientRecord.objects.create(person=self.person, hemoglobin_g_dl=99.0)
        m = Measurement(
            measurement_id=93020,
            person=self.person,
            measurement_concept=self._measurement_concept(),
            measurement_date=date(2023, 3, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=5.0,
            measurement_source_value='Hemoglobin [Mass/volume] in Blood',
        )
        m._skip_patient_record_refresh = True
        m.save()
        # PatientRecord should NOT have been updated
        pi = PatientRecord.objects.get(person=self.person)
        self.assertAlmostEqual(float(pi.hemoglobin_g_dl), 99.0, places=0)


class ConditionSignalTest(_OmopBase):
    """Saving a ConditionOccurrence triggers refresh_patient_record."""

    PERSON_ID = 90310

    def test_condition_save_updates_disease(self):
        PatientRecord.objects.create(person=self.person)
        ConditionOccurrence.objects.create(
            condition_occurrence_id=93101,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2022, 1, 1),
            condition_type_concept=self.type_concept,
        )
        pi = PatientRecord.objects.get(person=self.person)
        self.assertIsNotNone(pi.disease)
        self.assertEqual(pi.disease, 'Breast Cancer')

    def test_condition_delete_clears_disease(self):
        PatientRecord.objects.create(person=self.person)
        co = ConditionOccurrence.objects.create(
            condition_occurrence_id=93110,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2022, 1, 1),
            condition_type_concept=self.type_concept,
        )
        pi = PatientRecord.objects.get(person=self.person)
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

    def test_direct_org_doctor_sees_their_org(self):
        direct_doctor = Identity.objects.create_user(email='directdoc@test.com', password='x')
        GroupAccess.objects.create(
            identity=direct_doctor, org=self.org_b, role='doctor',
        )
        orgs = list(get_visible_orgs(direct_doctor))
        self.assertIn(self.org_b, orgs)
        self.assertNotIn(self.org_a, orgs)

    def test_direct_org_analyst_sees_their_org(self):
        analyst = Identity.objects.create_user(email='analyst@test.com', password='x')
        GroupAccess.objects.create(
            identity=analyst, org=self.org_b, role='analyst',
        )
        orgs = list(get_visible_orgs(analyst))
        self.assertIn(self.org_b, orgs)
        self.assertNotIn(self.org_a, orgs)

    def test_user_with_no_grants_sees_nothing(self):
        orgs = list(get_visible_orgs(self.nobody))
        self.assertEqual(orgs, [])

    def test_user_with_no_grants_sees_public_aggregated_org(self):
        self.org_b.allows_public_aggregated_data = True
        self.org_b.save(update_fields=['allows_public_aggregated_data'])
        orgs = list(get_visible_orgs(self.nobody))
        self.assertIn(self.org_b, orgs)
        self.assertNotIn(self.org_a, orgs)

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
            and any(c.get('system') == 'https://healthkey.ai/fhir/fl-regimen'
                    for c in e['resource']['medicationCodeableConcept']['coding'])
        ]
        self.assertGreater(len(regimen_stmts), 0, "Expected at least one regimen MedicationStatement")
        for stmt in regimen_stmts:
            systems = {c['system'] for c in stmt['medicationCodeableConcept']['coding']}
            # HemOnc coding should be present for DB-sourced regimens (not radiation)
            has_hemonc = 'http://ohdsi.org/omop/HemOnc' in systems
            is_radiation_only = systems == {'https://healthkey.ai/fhir/fl-regimen'}
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

    # ------------------------------------------------------------------
    # Realistic timelines / mortality (PRism FLF Section-4 charts)
    # ------------------------------------------------------------------

    @staticmethod
    def _line_periods(bundle):
        """{patient_id: {line_num: (start_date, end_date, outcome)}} for regimen statements."""
        from datetime import date as _date
        lines = {}
        for entry in bundle['entry']:
            r = entry['resource']
            if r['resourceType'] != 'MedicationStatement':
                continue
            ext = {x['url'].split('/')[-1]: x for x in r.get('extension', [])}
            if 'therapy-outcome' not in ext:
                continue
            pid = r['subject']['reference'].split('/')[-1]
            num = ext['therapy-line']['valueInteger']
            period = r['effectivePeriod']
            lines.setdefault(pid, {})[num] = (
                _date.fromisoformat(period['start']),
                _date.fromisoformat(period['end']),
                ext['therapy-outcome']['valueString'],
            )
        return lines

    @patch(_MOCK_TARGET, return_value=_FL_MOCK_CATALOG)
    def test_therapy_lines_chronological_and_bounded(self, _mock):
        """Line n+1 starts after line n ends; durations are months, not years;
        nothing is dated in the future."""
        from datetime import date as _date, timedelta as _td
        from omop_core.management.commands._fl_generator import FLBundleGenerator
        gen = FLBundleGenerator(watch_wait_ratio=0.0)
        bundle = gen.generate_bundle(100)
        today = _date.today()
        saw_multi_line = False
        for pid, lines in self._line_periods(bundle).items():
            nums = sorted(lines)
            for num in nums:
                start, end, _ = lines[num]
                self.assertLessEqual(start, end, f'{pid} line {num}: start after end')
                self.assertLessEqual(end, today + _td(days=1), f'{pid} line {num} in the future')
                self.assertLessEqual((end - start).days, 250,
                                     f'{pid} line {num}: treatment duration unrealistically long')
            for prev, nxt in zip(nums, nums[1:]):
                self.assertGreater(lines[nxt][0], lines[prev][1],
                                   f'{pid}: line {nxt} starts before line {prev} ends')
                saw_multi_line = True
        self.assertTrue(saw_multi_line, 'expected at least one multi-line patient in 100')

    @patch(_MOCK_TARGET, return_value=_FL_MOCK_CATALOG)
    def test_pod24_only_a_minority(self, _mock):
        """Early progression (2L within 24 months of 1L) must be a minority —
        not every multi-line patient is POD24."""
        from omop_core.management.commands._fl_generator import FLBundleGenerator
        gen = FLBundleGenerator(watch_wait_ratio=0.0)
        bundle = gen.generate_bundle(200)
        lines = self._line_periods(bundle)
        multi = {pid: ls for pid, ls in lines.items() if 1 in ls and 2 in ls}
        self.assertGreater(len(multi), 20, 'need a decent multi-line sample')
        early = sum(1 for ls in multi.values()
                    if (ls[2][0] - ls[1][0]).days <= 730)
        frac = early / len(multi)
        self.assertLess(frac, 0.50, f'{frac:.0%} of multi-line patients progress ≤24mo — too many')
        self.assertGreater(frac, 0.02, f'{frac:.0%} POD24 — too few to be useful')

    @patch(_MOCK_TARGET, return_value=_FL_MOCK_CATALOG)
    def test_some_patients_deceased_and_death_after_last_event(self, _mock):
        """A fraction of the cohort has deceasedDateTime, always after the
        last therapy line end."""
        from datetime import date as _date
        from omop_core.management.commands._fl_generator import FLBundleGenerator
        gen = FLBundleGenerator(watch_wait_ratio=0.0)
        bundle = gen.generate_bundle(100)
        lines = self._line_periods(bundle)
        deaths = 0
        for entry in bundle['entry']:
            r = entry['resource']
            if r['resourceType'] != 'Patient' or 'deceasedDateTime' not in r:
                continue
            deaths += 1
            death = _date.fromisoformat(r['deceasedDateTime'])
            self.assertLessEqual(death, _date.today(), 'death date in the future')
            patient_lines = lines.get(r['id'])
            if patient_lines:
                last_end = max(end for _, end, _ in patient_lines.values())
                self.assertGreaterEqual(death, last_end,
                                        'death before the end of the last therapy line')
        self.assertGreater(deaths, 0, 'no deceased patients in 100')
        self.assertLess(deaths, 60, f'{deaths}/100 deceased — implausibly high')

    @patch(_MOCK_TARGET, return_value=_FL_MOCK_CATALOG)
    def test_first_line_cr_is_common(self, _mock):
        """1L CR rate should be high (~55% by weights) so CR30 landmarks have data."""
        from collections import Counter
        from omop_core.management.commands._fl_generator import FLBundleGenerator
        gen = FLBundleGenerator(watch_wait_ratio=0.0)
        bundle = gen.generate_bundle(200)
        outcomes = Counter(
            ls[1][2] for ls in self._line_periods(bundle).values() if 1 in ls
        )
        total = sum(outcomes.values())
        cr_frac = outcomes['Complete Response'] / total
        self.assertGreater(cr_frac, 0.35, f'1L CR rate {cr_frac:.0%} too low')
        self.assertLess(cr_frac, 0.75, f'1L CR rate {cr_frac:.0%} implausibly high')


# ---------------------------------------------------------------------------
# TEST-05: seed_omop_concepts management command
# ---------------------------------------------------------------------------

class SeedOmopConceptsTest(TestCase):
    """Verify that seed_omop_concepts creates all expected concepts."""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command
        call_command('seed_omop_concepts', verbosity=0)

    def _concept(self, concept_id):
        return Concept.objects.filter(concept_id=concept_id).first()

    def test_gender_concepts_created(self):
        for cid, name in [(8507, 'MALE'), (8532, 'FEMALE'), (8551, 'UNKNOWN')]:
            c = self._concept(cid)
            self.assertIsNotNone(c, f'Concept {cid} ({name}) not seeded')
            self.assertEqual(c.vocabulary_id, 'Gender')

    def test_loinc_concepts_have_loinc_vocabulary(self):
        loinc_codes = [
            (3016723, '2160-0'),   # Creatinine serum
            (3051825, '38483-4'),  # Creatinine blood (mCODE)
            (3006923, '1742-6'),   # ALT
            (3013721, '1920-8'),   # AST
        ]
        for cid, code in loinc_codes:
            c = self._concept(cid)
            self.assertIsNotNone(c, f'Concept {cid} (LOINC {code}) not seeded')
            self.assertEqual(c.vocabulary_id, 'LOINC', f'Concept {cid} should have vocabulary_id=LOINC')
            self.assertEqual(c.concept_code, code)

    def test_mcode_cmp_concepts_created(self):
        for cid, code, label in [
            (3032503, '49765-1', 'Calcium Blood'),
            (3000285, '2947-0',  'Sodium Blood'),
            (3005456, '6298-4',  'Potassium Blood'),
            (3004295, '6299-2',  'BUN Blood'),
            (3030354, '33914-3', 'eGFR'),
            (3000483, '2339-0',  'Glucose Blood'),
        ]:
            c = self._concept(cid)
            self.assertIsNotNone(c, f'mCODE CMP concept {cid} ({label}) not seeded')
            self.assertEqual(c.concept_code, code)

    def test_seed_is_idempotent(self):
        from django.core.management import call_command
        call_command('seed_omop_concepts', verbosity=0)
        self.assertEqual(Concept.objects.filter(concept_id=8532).count(), 1,
                         'Duplicate concept created on second seed_omop_concepts run')
