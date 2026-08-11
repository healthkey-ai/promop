"""
omop_core tests — TEST-01, TEST-02, TEST-03, TEST-04

TEST-01: PatientRecord model-level tests
TEST-02: refresh_patient_record service unit tests
TEST-03: Signal integration tests at omop_core level
TEST-04: FLBundleGenerator unit tests
"""

import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import ProgrammingError, connection, transaction
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


# ===========================================================================
# Issue #236 P0b — regimen resolution namespace hygiene
# ===========================================================================

class RegimenResolutionTest(TestCase):
    """Unit tests for omop_core.services.regimen_resolution."""

    def setUp(self):
        from omop_core.models import RegimenMappingGap  # noqa: F401
        from omop_core.services.concept_cache import concept_cache_clear
        concept_cache_clear()
        self.addCleanup(concept_cache_clear)

        self.hemonc_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='HemOnc',
            defaults={'vocabulary_name': 'HemOnc', 'vocabulary_concept_id': 0},
        )
        self.regimen_cc, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Regimen',
            defaults={'concept_class_name': 'Regimen', 'concept_class_concept_id': 0},
        )
        self.drug_domain, _ = Domain.objects.get_or_create(
            domain_id='Drug',
            defaults={'domain_name': 'Drug', 'domain_concept_id': 13},
        )
        self.concept0, _ = Concept.objects.get_or_create(
            concept_id=0,
            defaults={
                'concept_name': 'No matching concept',
                'domain': self.drug_domain,
                'vocabulary': self.hemonc_vocab,
                'concept_class': self.regimen_cc,
                'concept_code': '0',
                'valid_start_date': date(1970, 1, 1),
                'valid_end_date': date(2099, 12, 31),
            },
        )

        def _regimen(cid, name, **kwargs):
            defaults = {
                'concept_name': name,
                'domain': self.drug_domain,
                'vocabulary': self.hemonc_vocab,
                'concept_class': self.regimen_cc,
                'standard_concept': 'S',
                'concept_code': str(cid),
                'valid_start_date': date(1970, 1, 1),
                'valid_end_date': date(2099, 12, 31),
            }
            defaults.update(kwargs)
            obj, _ = Concept.objects.get_or_create(concept_id=cid, defaults=defaults)
            return obj

        self.valid_regimen = _regimen(9770001, 'RVD')
        self.deprecated_regimen = _regimen(9770002, 'OldRegimen', invalid_reason='D')
        self.nonstandard_regimen = _regimen(9770003, 'NonStdRegimen', standard_concept=None)

        from omop_core.models import ConceptSynonym
        ConceptSynonym.objects.get_or_create(
            concept=self.valid_regimen,
            concept_synonym_name='Revlimid-Velcade-Dex',
            language_concept=self.concept0,
        )

    # -- validate_hemonc_regimen -------------------------------------------

    def test_validate_accepts_valid_regimen(self):
        from omop_core.services.regimen_resolution import validate_hemonc_regimen
        self.assertTrue(validate_hemonc_regimen(self.valid_regimen))

    def test_validate_rejects_none_and_bad_rows(self):
        from omop_core.services.regimen_resolution import validate_hemonc_regimen
        self.assertFalse(validate_hemonc_regimen(None))
        self.assertFalse(validate_hemonc_regimen(self.deprecated_regimen))
        self.assertFalse(validate_hemonc_regimen(self.nonstandard_regimen))

    def test_validate_rejects_wrong_vocabulary(self):
        from omop_core.services.regimen_resolution import validate_hemonc_regimen
        rxnorm_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='RxNorm',
            defaults={'vocabulary_name': 'RxNorm', 'vocabulary_concept_id': 0},
        )
        foreign = Concept.objects.create(
            concept_id=9770010, concept_name='RVD', domain=self.drug_domain,
            vocabulary=rxnorm_vocab, concept_class=self.regimen_cc,
            standard_concept='S', concept_code='9770010',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.assertFalse(validate_hemonc_regimen(foreign))

    # -- match_hemonc_regimen_by_name --------------------------------------

    def test_match_by_name_case_insensitive(self):
        from omop_core.services.regimen_resolution import match_hemonc_regimen_by_name
        self.assertEqual(
            match_hemonc_regimen_by_name('rvd').concept_id,
            self.valid_regimen.concept_id,
        )

    def test_match_by_synonym(self):
        from omop_core.services.regimen_resolution import match_hemonc_regimen_by_name
        self.assertEqual(
            match_hemonc_regimen_by_name('revlimid-velcade-dex').concept_id,
            self.valid_regimen.concept_id,
        )

    def test_match_skips_deprecated_and_nonstandard(self):
        from omop_core.services.regimen_resolution import match_hemonc_regimen_by_name
        self.assertIsNone(match_hemonc_regimen_by_name('OldRegimen'))
        self.assertIsNone(match_hemonc_regimen_by_name('NonStdRegimen'))

    def test_match_unknown_returns_none(self):
        from omop_core.services.regimen_resolution import match_hemonc_regimen_by_name
        self.assertIsNone(match_hemonc_regimen_by_name('ZZ-Does-Not-Exist'))
        self.assertIsNone(match_hemonc_regimen_by_name(''))

    # -- get_or_create_quarantine_regimen -----------------------------------

    def test_quarantine_mints_under_hk_regimen_never_hemonc(self):
        from omop_core.services.regimen_resolution import get_or_create_quarantine_regimen
        hemonc_before = Concept.objects.filter(vocabulary_id='HemOnc').count()
        concept = get_or_create_quarantine_regimen('ZZ-Novel-Regimen')

        self.assertEqual(concept.vocabulary_id, 'HK-Regimen')
        self.assertEqual(concept.concept_class_id, 'Regimen')
        self.assertEqual(concept.concept_code, 'hkr:zz-novel-regimen')
        self.assertIsNone(concept.standard_concept)
        self.assertEqual(concept.source, 'HealthKey')
        self.assertEqual(
            Concept.objects.filter(vocabulary_id='HemOnc').count(), hemonc_before,
            'Quarantine path wrote to the HemOnc vocabulary',
        )

    def test_quarantine_records_mapping_gap_and_bumps_count(self):
        from omop_core.models import RegimenMappingGap
        from omop_core.services.regimen_resolution import get_or_create_quarantine_regimen
        first = get_or_create_quarantine_regimen('ZZ-Novel-Regimen')
        second = get_or_create_quarantine_regimen('ZZ-Novel-Regimen')

        self.assertEqual(first.concept_id, second.concept_id)
        gap = RegimenMappingGap.objects.get(
            source_system='fhir-upload', normalized_name='zz-novel-regimen',
        )
        self.assertEqual(gap.quarantine_concept_id, first.concept_id)
        self.assertEqual(gap.status, RegimenMappingGap.STATUS_UNMATCHED)
        self.assertEqual(gap.occurrence_count, 2)

    def test_quarantine_normalizes_gap_key(self):
        from omop_core.models import RegimenMappingGap
        from omop_core.services.regimen_resolution import get_or_create_quarantine_regimen
        get_or_create_quarantine_regimen('  ZZ-Novel   Regimen ')
        self.assertTrue(RegimenMappingGap.objects.filter(
            normalized_name='zz-novel regimen',
        ).exists())

    def test_quarantine_blank_name_returns_none(self):
        from omop_core.services.regimen_resolution import get_or_create_quarantine_regimen
        self.assertIsNone(get_or_create_quarantine_regimen('   '))

    # -- get_or_create_quarantine_drug --------------------------------------

    def test_quarantine_drug_mints_under_hk_drug(self):
        from omop_core.services.regimen_resolution import get_or_create_quarantine_drug
        concept = get_or_create_quarantine_drug(
            source_vocabulary_id='RxNorm', concept_code='99999999',
            concept_name='mystery drug',
        )
        self.assertEqual(concept.vocabulary_id, 'HK-Drug')
        self.assertEqual(concept.concept_code, 'hkd:rxnorm-99999999')
        self.assertIsNone(concept.standard_concept)
        self.assertEqual(concept.source, 'HealthKey')
        self.assertFalse(Concept.objects.filter(
            vocabulary_id='RxNorm', concept_code='99999999',
        ).exists(), 'Quarantine path minted a row under the licensed vocabulary')

    def test_quarantine_observation_mints_under_hk_observation(self):
        from omop_core.models import RegimenMappingGap
        from omop_core.services.regimen_resolution import get_or_create_quarantine_observation
        concept = get_or_create_quarantine_observation(
            source_vocabulary_id='LOINC', concept_code='99999-9',
            concept_name='mystery report',
        )
        self.assertEqual(concept.vocabulary_id, 'HK-Observation')
        self.assertEqual(concept.domain_id, 'Observation')
        self.assertEqual(concept.concept_code, 'hko:loinc-99999-9')
        self.assertIsNone(concept.standard_concept)
        self.assertEqual(concept.source, 'HealthKey')
        self.assertFalse(Concept.objects.filter(
            vocabulary_id='LOINC', concept_code='99999-9',
        ).exists(), 'Quarantine path minted a row under the licensed vocabulary')
        self.assertTrue(RegimenMappingGap.objects.filter(
            normalized_name='mystery report',
        ).exists())

    def test_quarantine_procedure_mints_under_hk_procedure(self):
        from omop_core.models import RegimenMappingGap
        from omop_core.services.regimen_resolution import get_or_create_quarantine_procedure
        concept = get_or_create_quarantine_procedure(
            source_vocabulary_id='SNOMED', concept_code='999999999',
            concept_name='mystery procedure',
        )
        self.assertEqual(concept.vocabulary_id, 'HK-Procedure')
        self.assertEqual(concept.domain_id, 'Procedure')
        self.assertEqual(concept.concept_code, 'hkp:snomed-999999999')
        self.assertIsNone(concept.standard_concept)
        self.assertEqual(concept.source, 'HealthKey')
        self.assertFalse(Concept.objects.filter(
            vocabulary_id='SNOMED', concept_code='999999999',
        ).exists(), 'Quarantine path minted a row under the licensed vocabulary')
        self.assertTrue(RegimenMappingGap.objects.filter(
            normalized_name='mystery procedure',
        ).exists())

    def test_slug_collision_gets_disambiguated_concept(self):
        """Two distinct names that slugify identically must not share a row."""
        from omop_core.services.regimen_resolution import get_or_create_quarantine_regimen
        first = get_or_create_quarantine_regimen('ZZ Collision (A)')
        second = get_or_create_quarantine_regimen('ZZ Collision A?')
        # Both slugify to 'hkr:zz-collision-a' — the second must be disambiguated.
        self.assertNotEqual(first.concept_id, second.concept_id)
        self.assertEqual(first.concept_code, 'hkr:zz-collision-a')
        self.assertTrue(second.concept_code.startswith('hkr:zz-collision-a-'))
        self.assertEqual(len(second.concept_code), len('hkr:zz-collision-a-') + 8)
        # Repeat sightings remain idempotent per name.
        again = get_or_create_quarantine_regimen('ZZ Collision A?')
        self.assertEqual(again.concept_id, second.concept_id)

    def test_record_mapping_gap_truncates_overlong_names(self):
        from omop_core.models import RegimenMappingGap
        from omop_core.services.regimen_resolution import record_mapping_gap
        long_name = 'ZZ-' + ('x' * 400)
        gap = record_mapping_gap(source_system='fhir-upload', source_value=long_name)
        self.assertIsNotNone(gap)
        self.assertLessEqual(len(gap.normalized_name), 255)
        self.assertLessEqual(len(gap.source_value), 255)

    def test_quarantine_regimen_overlong_name_does_not_raise(self):
        from omop_core.services.regimen_resolution import get_or_create_quarantine_regimen
        concept = get_or_create_quarantine_regimen('ZZ-' + ('y' * 400))
        self.assertIsNotNone(concept)
        self.assertLessEqual(len(concept.concept_name), 255)


class ReportRegimenMappingGapsCommandTest(TestCase):
    """The report_regimen_mapping_gaps command prints counts and a table."""

    def setUp(self):
        from omop_core.services.concept_cache import concept_cache_clear
        concept_cache_clear()
        self.addCleanup(concept_cache_clear)
        Domain.objects.get_or_create(
            domain_id='Drug',
            defaults={'domain_name': 'Drug', 'domain_concept_id': 13},
        )

    def test_command_outputs_counts_and_rows(self):
        from io import StringIO
        from django.core.management import call_command
        from omop_core.services.regimen_resolution import get_or_create_quarantine_regimen

        get_or_create_quarantine_regimen('ZZ-Report-Me')
        get_or_create_quarantine_regimen('ZZ-Report-Me')

        out = StringIO()
        call_command('report_regimen_mapping_gaps', stdout=out)
        text = out.getvalue()
        self.assertIn('unmatched', text)
        self.assertIn('ZZ-Report-Me', text)
        self.assertIn('2', text)  # occurrence_count

    def test_command_empty_table(self):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command('report_regimen_mapping_gaps', stdout=out)
        self.assertIn('No mapping gaps recorded.', out.getvalue())


# ---------------------------------------------------------------------------
# TI.4.2 Terminology maintenance (promop#305)
#   - vocabulary deprecation (#05)
#   - append-only version change-history (#01/#09)
#   - concept-replacement resolution / embedded-term substitution (#07)
# ---------------------------------------------------------------------------

class TerminologyMaintenanceTest(TestCase):
    """Vocabulary deprecation, version history, concept-replacement resolution."""

    def setUp(self):
        from omop_core.models import Relationship, ConceptRelationship
        (self.vocab, self.dom_cond, self.dom_meas, self.dom_drug,
         self.dom_type, self.dom_obs, self.cc) = _make_vocab()
        # A deprecated concept that is 'Concept replaced by' an active successor,
        # via one intermediate hop to exercise chain-following.
        self.old = _concept(880001, 'Old ingredient', self.dom_drug, self.vocab, self.cc, code='OLD')
        self.old.invalid_reason = 'U'
        self.old.save(update_fields=['invalid_reason'])
        self.mid = _concept(880002, 'Interim ingredient', self.dom_drug, self.vocab, self.cc, code='MID')
        self.mid.invalid_reason = 'U'
        self.mid.save(update_fields=['invalid_reason'])
        self.new = _concept(880003, 'Current ingredient', self.dom_drug, self.vocab, self.cc, code='NEW')

        self.rel, _ = Relationship.objects.get_or_create(
            relationship_id='Concept replaced by',
            defaults={
                'relationship_name': 'Concept replaced by',
                'is_hierarchical': 0,
                'defines_ancestry': 0,
                'reverse_relationship_id': 'Concept replaces',
                'relationship_concept_id': 0,
            },
        )
        for a, b in ((self.old, self.mid), (self.mid, self.new)):
            ConceptRelationship.objects.get_or_create(
                concept_1=a, concept_2=b, relationship=self.rel,
                defaults={'valid_start_date': date(1970, 1, 1),
                          'valid_end_date': date(2099, 12, 31)},
            )

    # --- #05 vocabulary deprecation ---

    def test_vocabulary_deprecation_command_sets_flag(self):
        from io import StringIO
        from django.core.management import call_command
        from omop_core.models import Vocabulary

        call_command('deprecate_vocabulary', 'OMOP_TEST',
                     reason='Superseded', stdout=StringIO())
        v = Vocabulary.objects.get(vocabulary_id='OMOP_TEST')
        self.assertTrue(v.is_deprecated)
        self.assertEqual(v.deprecated_date, date.today())
        self.assertEqual(v.deprecated_reason, 'Superseded')

    def test_deprecate_command_records_history_and_undo(self):
        from io import StringIO
        from django.core.management import call_command
        from omop_core.models import Vocabulary, VocabularyVersionHistory

        call_command('deprecate_vocabulary', 'OMOP_TEST',
                     reason='Superseded', stdout=StringIO())
        hist = VocabularyVersionHistory.objects.filter(
            vocabulary_id='OMOP_TEST',
            action=VocabularyVersionHistory.ACTION_DEPRECATED,
        )
        self.assertEqual(hist.count(), 1)
        self.assertEqual(hist.first().note, 'Superseded')

        call_command('deprecate_vocabulary', 'OMOP_TEST', undo=True, stdout=StringIO())
        v = Vocabulary.objects.get(vocabulary_id='OMOP_TEST')
        self.assertFalse(v.is_deprecated)
        self.assertIsNone(v.deprecated_date)

    def test_deprecate_command_unknown_vocabulary_errors(self):
        from io import StringIO
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command('deprecate_vocabulary', 'NOPE', stdout=StringIO())

    # --- #01/#09 version change-history helper ---

    def test_version_history_helper_records_each_action(self):
        from omop_core.models import (
            record_vocabulary_version_history, VocabularyVersionHistory,
        )
        record_vocabulary_version_history(
            'LOINC', version='2.77', action=VocabularyVersionHistory.ACTION_LOADED,
            cdm_release_date=date(2026, 1, 1),
        )
        record_vocabulary_version_history(
            'LOINC', version='2.78', action=VocabularyVersionHistory.ACTION_REPLACED,
        )
        record_vocabulary_version_history(
            'LOINC', version='2.78', action=VocabularyVersionHistory.ACTION_DEPRECATED,
            note='retired',
        )
        rows = VocabularyVersionHistory.objects.filter(vocabulary_id='LOINC')
        self.assertEqual(rows.count(), 3)
        self.assertEqual(
            set(rows.values_list('action', flat=True)),
            {'loaded', 'replaced', 'deprecated'},
        )
        # Append-only: replaced does not overwrite the loaded row.
        self.assertEqual(rows.filter(version='2.77').count(), 1)

    # --- #07 concept-replacement resolution ---

    def test_resolve_concept_replacement_follows_chain_to_active(self):
        from omop_core.models import resolve_concept_replacement

        resolved, chain = resolve_concept_replacement(self.old.concept_id)
        self.assertEqual(resolved.concept_id, self.new.concept_id)
        self.assertIsNone(resolved.invalid_reason)
        self.assertEqual(chain, [880001, 880002, 880003])

    def test_resolve_concept_replacement_active_concept_is_identity(self):
        from omop_core.models import resolve_concept_replacement

        resolved, chain = resolve_concept_replacement(self.new.concept_id)
        self.assertEqual(resolved.concept_id, self.new.concept_id)
        self.assertEqual(chain, [self.new.concept_id])

    def test_resolve_concept_replacement_unknown_returns_none(self):
        from omop_core.models import resolve_concept_replacement

        resolved, chain = resolve_concept_replacement(999999999)
        self.assertIsNone(resolved)
        self.assertEqual(chain, [])


# ---------------------------------------------------------------------------
# TEST-07: VocabularyRelease model + service tests
# ---------------------------------------------------------------------------

class VocabularyReleaseModelTest(TestCase):
    """Test VocabularyRelease CRUD and field defaults."""

    def test_create_with_defaults(self):
        from omop_core.models import VocabularyRelease
        from django.utils import timezone

        now = timezone.now()
        vr = VocabularyRelease.objects.create(build_timestamp=now)
        self.assertEqual(vr.status, 'staged')
        self.assertEqual(vr.schema_version, '5.4')
        self.assertEqual(vr.scope, [])
        self.assertEqual(vr.vocab_versions, {})
        self.assertEqual(vr.row_counts, {})
        self.assertEqual(vr.checksums, {})
        self.assertIsNone(vr.published_at)
        self.assertIsNone(vr.athena_version)
        self.assertIsNone(vr.notes)

    def test_create_published(self):
        from omop_core.models import VocabularyRelease
        from django.utils import timezone

        now = timezone.now()
        vr = VocabularyRelease.objects.create(
            build_timestamp=now,
            scope=['SNOMED', 'LOINC'],
            athena_version='v5.0 2024-07-01',
            vocab_versions={'SNOMED': '20240701', 'LOINC': '2.77'},
            row_counts={'concept': 500000, 'concept_relationship': 1200000},
            checksums={'concept': {'count': 500000, 'max_id': 999999, 'min_id': 1}},
            status='published',
            published_at=now,
            notes='Initial load',
        )
        vr.refresh_from_db()
        self.assertEqual(vr.scope, ['SNOMED', 'LOINC'])
        self.assertEqual(vr.vocab_versions['LOINC'], '2.77')
        self.assertEqual(vr.row_counts['concept'], 500000)
        self.assertEqual(vr.status, 'published')
        self.assertIn('concept', vr.checksums)

    def test_str_representation(self):
        from omop_core.models import VocabularyRelease
        from django.utils import timezone

        now = timezone.now()
        vr = VocabularyRelease.objects.create(
            build_timestamp=now, status='published', published_at=now,
        )
        s = str(vr)
        self.assertIn('published', s)
        self.assertIn(str(vr.pk), s)

    def test_ordering_by_published_at(self):
        from omop_core.models import VocabularyRelease
        from django.utils import timezone
        from datetime import timedelta

        now = timezone.now()
        older = VocabularyRelease.objects.create(
            build_timestamp=now - timedelta(hours=2),
            status='published', published_at=now - timedelta(hours=2),
        )
        newer = VocabularyRelease.objects.create(
            build_timestamp=now,
            status='published', published_at=now,
        )
        releases = list(VocabularyRelease.objects.all())
        self.assertEqual(releases[0].pk, newer.pk)
        self.assertEqual(releases[1].pk, older.pk)


class VocabularyReleaseServiceTest(TestCase):
    """Test get_latest_release() and get_release_etag()."""

    def test_get_latest_release_empty(self):
        from omop_core.services.vocab_release import get_latest_release
        self.assertIsNone(get_latest_release())

    def test_get_latest_release_ignores_staged(self):
        from omop_core.models import VocabularyRelease
        from omop_core.services.vocab_release import get_latest_release
        from django.utils import timezone

        VocabularyRelease.objects.create(
            build_timestamp=timezone.now(), status='staged',
        )
        self.assertIsNone(get_latest_release())

    def test_get_latest_release_returns_most_recent_published(self):
        from omop_core.models import VocabularyRelease
        from omop_core.services.vocab_release import get_latest_release
        from django.utils import timezone
        from datetime import timedelta

        now = timezone.now()
        VocabularyRelease.objects.create(
            build_timestamp=now - timedelta(days=1),
            status='published', published_at=now - timedelta(days=1),
        )
        newer = VocabularyRelease.objects.create(
            build_timestamp=now,
            status='published', published_at=now,
        )
        VocabularyRelease.objects.create(
            build_timestamp=now, status='retired',
            published_at=now - timedelta(hours=1),
        )
        result = get_latest_release()
        self.assertEqual(result.pk, newer.pk)

    def test_get_release_etag_none(self):
        from omop_core.services.vocab_release import get_release_etag
        self.assertIsNone(get_release_etag(None))

    def test_get_release_etag_format(self):
        from omop_core.models import VocabularyRelease
        from omop_core.services.vocab_release import get_release_etag
        from django.utils import timezone

        now = timezone.now()
        vr = VocabularyRelease.objects.create(
            build_timestamp=now, status='published', published_at=now,
        )
        etag = get_release_etag(vr)
        self.assertTrue(etag.startswith('"vr-'))
        self.assertTrue(etag.endswith('"'))
        self.assertIn(str(vr.pk), etag)

    def test_get_release_etag_stable(self):
        from omop_core.models import VocabularyRelease
        from omop_core.services.vocab_release import get_release_etag
        from django.utils import timezone

        now = timezone.now()
        vr = VocabularyRelease.objects.create(
            build_timestamp=now, status='published', published_at=now,
        )
        self.assertEqual(get_release_etag(vr), get_release_etag(vr))

    def test_get_release_etag_unique_across_releases(self):
        from omop_core.models import VocabularyRelease
        from omop_core.services.vocab_release import get_release_etag
        from django.utils import timezone
        from datetime import timedelta

        now = timezone.now()
        vr1 = VocabularyRelease.objects.create(
            build_timestamp=now - timedelta(hours=1),
            status='published', published_at=now - timedelta(hours=1),
        )
        vr2 = VocabularyRelease.objects.create(
            build_timestamp=now,
            status='published', published_at=now,
        )
        self.assertNotEqual(get_release_etag(vr1), get_release_etag(vr2))


# ---------------------------------------------------------------------------
# TEST-08: STCM loader + VocabularyRelease publication from loader
# ---------------------------------------------------------------------------

class LoadSTCMTest(_OmopBase):
    """Test _load_source_to_concept_map in the Athena loader."""

    PERSON_ID = 90290

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

    def test_stcm_loads_and_filters_unloaded_refs(self):
        from omop_core.models import SourceToConceptMap
        src = _concept(960001, 'Source concept', self.dom_meas, self.vocab, self.cc, code='SRC1')
        tgt = _concept(960002, 'Target concept', self.dom_meas, self.vocab, self.cc, code='TGT1')
        header = [
            'source_code', 'source_concept_id', 'source_vocabulary_id',
            'source_code_description', 'target_concept_id', 'target_vocabulary_id',
            'valid_start_date', 'valid_end_date', 'invalid_reason',
        ]
        count = self._run_loader(
            '_load_source_to_concept_map', 'SOURCE_TO_CONCEPT_MAP.csv', header,
            [
                ('ABC', 960001, 'OMOP_TEST', 'desc', 960002, 'OMOP_TEST', '19700101', '20991231', ''),
                ('DEF', 999998, 'OMOP_TEST', '', 960002, 'OMOP_TEST', '19700101', '20991231', ''),  # src not loaded
                ('GHI', 960001, 'OMOP_TEST', '', 999997, 'OMOP_TEST', '19700101', '20991231', ''),  # tgt not loaded
            ],
        )
        self.assertEqual(count, 1)
        self.assertEqual(SourceToConceptMap.objects.count(), 1)
        row = SourceToConceptMap.objects.first()
        self.assertEqual(row.source_code, 'ABC')
        self.assertEqual(row.source_concept_id, 960001)
        self.assertEqual(row.target_concept_id, 960002)

    def test_stcm_dry_run_returns_count_without_writing(self):
        import os
        import tempfile
        from io import StringIO
        from omop_core.models import SourceToConceptMap
        from omop_core.management.commands.load_athena_vocabularies import Command
        _concept(960001, 'Source concept', self.dom_meas, self.vocab, self.cc, code='SRC1')
        _concept(960002, 'Target concept', self.dom_meas, self.vocab, self.cc, code='TGT1')
        header = [
            'source_code', 'source_concept_id', 'source_vocabulary_id',
            'source_code_description', 'target_concept_id', 'target_vocabulary_id',
            'valid_start_date', 'valid_end_date', 'invalid_reason',
        ]
        d = tempfile.mkdtemp()
        with open(os.path.join(d, 'SOURCE_TO_CONCEPT_MAP.csv'), 'w', encoding='utf-8', newline='') as f:
            f.write('\t'.join(header) + '\n')
            f.write('\t'.join(str(x) for x in ('ABC', 960001, 'OMOP_TEST', '', 960002, 'OMOP_TEST', '19700101', '20991231', '')) + '\n')
        cmd = Command(stdout=StringIO())
        cmd._base = d
        cmd._gcs_bucket = None
        cmd._direct = False
        count = cmd._load_source_to_concept_map(True)  # dry_run=True
        self.assertEqual(count, 1)
        self.assertEqual(SourceToConceptMap.objects.count(), 0)

    def test_stcm_missing_file_returns_zero(self):
        import tempfile
        from io import StringIO
        from omop_core.management.commands.load_athena_vocabularies import Command
        d = tempfile.mkdtemp()  # empty dir, no SOURCE_TO_CONCEPT_MAP.csv
        cmd = Command(stdout=StringIO())
        cmd._base = d
        cmd._gcs_bucket = None
        cmd._direct = False
        count = cmd._load_source_to_concept_map(False)
        self.assertEqual(count, 0)


class PublishReleaseTest(_OmopBase):
    """Test _publish_release creates a VocabularyRelease row."""

    PERSON_ID = 90291

    def test_publish_release_creates_row(self):
        import time as _time
        from io import StringIO
        from omop_core.management.commands.load_athena_vocabularies import Command
        from omop_core.models import VocabularyRelease

        cmd = Command(stdout=StringIO())
        cmd._build_start = _time.time()
        cmd._cdm_vocab_version = 'v5.0 2024-07-01'
        counts = {'concept': 100, 'vocabulary': 5}
        cmd._publish_release(counts)

        self.assertEqual(VocabularyRelease.objects.count(), 1)
        release = VocabularyRelease.objects.first()
        self.assertEqual(release.status, 'published')
        self.assertIsNotNone(release.published_at)
        self.assertEqual(release.athena_version, 'v5.0 2024-07-01')
        # row_counts reflects the ACTUAL table COUNT(*) the snapshot streams, NOT
        # the per-run load counts passed in — so a consumer cross-checking streamed
        # rows against the manifest matches. It therefore equals the count captured
        # in checksums for the same table.
        self.assertEqual(set(release.row_counts), {'concept', 'vocabulary'})
        self.assertEqual(release.row_counts['concept'], release.checksums['concept']['count'])
        self.assertEqual(release.row_counts['vocabulary'], release.checksums['vocabulary']['count'])
        self.assertIn('concept', release.checksums)
        self.assertIn('vocabulary', release.checksums)



class LoadLoincClassesArchiveTest(TestCase):
    """Loading the LOINC class tables straight from a loinc.org archive.

    The two CSVs are ~110MB unzipped and are kept zipped, so a deployment
    holding the archive should not also need them unpacked beside it.
    """

    def _archive(self, tmpdir, names=('LoincClass.csv', 'Loinc.csv'), prefix=''):
        import zipfile
        path = Path(tmpdir) / 'loinc.zip'
        with zipfile.ZipFile(path, 'w') as zf:
            if 'LoincClass.csv' in names:
                zf.writestr(prefix + 'LoincClass.csv', 'CLASS,DISPLAY_NAME\nCHEM,Chemistry\n')
            if 'Loinc.csv' in names:
                zf.writestr(
                    prefix + 'Loinc.csv',
                    'LOINC_NUM,CLASS,STATUS\n2160-0,CHEM,ACTIVE\n',
                )
        return path

    def test_loads_both_tables_from_archive(self):
        from omop_core.models import LoincClass, LoincCodeClass

        with tempfile.TemporaryDirectory() as tmp:
            call_command('load_loinc_classes', archive=str(self._archive(tmp)))

        self.assertEqual(LoincClass.objects.get(code='CHEM').display_name, 'Chemistry')
        self.assertEqual(LoincCodeClass.objects.get(loinc_num='2160-0').loinc_class_id, 'CHEM')

    def test_finds_files_nested_in_the_archive(self):
        """Archives built from a directory carry a path prefix."""
        from omop_core.models import LoincClass

        with tempfile.TemporaryDirectory() as tmp:
            call_command(
                'load_loinc_classes',
                archive=str(self._archive(tmp, prefix='loinc-codes-aliases/')),
            )

        self.assertTrue(LoincClass.objects.filter(code='CHEM').exists())

    def test_incomplete_archive_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = self._archive(tmp, names=('LoincClass.csv',))
            with self.assertRaisesMessage(CommandError, 'Loinc.csv'):
                call_command('load_loinc_classes', archive=str(archive))

    def test_missing_archive_is_reported(self):
        with self.assertRaisesMessage(CommandError, 'File not found'):
            call_command('load_loinc_classes', archive='/nonexistent/loinc.zip')

    def test_malformed_gcs_uri_is_reported(self):
        with self.assertRaisesMessage(CommandError, 'bucket and an object'):
            call_command('load_loinc_classes', archive='gs://bucket-only')


# ---------------------------------------------------------------------------
# Schema / model sync
# ---------------------------------------------------------------------------

class PatientRecordSchemaSyncTest(TestCase):
    """
    Guard against columns drifting away from the model definition.

    Migration 0036 created a ``status`` column with raw SQL and migration 0040
    removed the field from Django's state only, leaving an orphan column that
    no model field mapped to. Migration 0138 drops it. This test fails if that
    -- or any comparable drift -- is reintroduced by the migration chain.

    Scope: the test database is always built fresh from the migrations, so this
    catches chain-introduced drift only. It cannot see out-of-band schema
    changes made directly against a deployed database -- that is what made the
    ``patient_info`` view 297 columns in staging and production but 296 in CI.
    Use the sync check in CLAUDE.md against those environments for that.
    """

    def _db_columns(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s",
                [PatientRecord._meta.db_table],
            )
            return {row[0] for row in cursor.fetchall()}

    def _model_columns(self):
        return {
            f.column
            for f in PatientRecord._meta.get_fields()
            if hasattr(f, 'column')
        }

    def test_no_orphan_status_column(self):
        """The column dropped by migration 0138 stays dropped."""
        self.assertNotIn('status', self._db_columns())

    def test_db_and_model_columns_match(self):
        """patient_record has no extra columns and no missing ones."""
        db_cols = self._db_columns()
        model_cols = self._model_columns()
        self.assertEqual(
            sorted(model_cols - db_cols), [],
            "model fields with no matching database column",
        )
        self.assertEqual(
            sorted(db_cols - model_cols), [],
            "database columns with no matching model field",
        )


class PatientInfoCompatViewTest(TestCase):
    """
    The ``patient_info`` view is a read-only compatibility shim for external
    consumers of the pre-rename table name. Migration 0138 had to rebuild it in
    order to drop the orphan ``status`` column, so these tests pin the parts of
    its contract that the rebuild had to preserve.
    """

    def _view_columns(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'patient_info' "
                "ORDER BY ordinal_position"
            )
            return [row[0] for row in cursor.fetchall()]

    def test_view_exists(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.views "
                "WHERE table_schema = 'public' AND table_name = 'patient_info'"
            )
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_view_still_exposes_status_as_null(self):
        """
        `status` survives as a NULL literal even though the physical column is
        gone, so external `SELECT status FROM patient_info` keeps working.
        """
        self.assertIn('status', self._view_columns())
        person = Person.objects.create(person_id=880001, year_of_birth=1980)
        PatientRecord.objects.create(person=person)
        with connection.cursor() as cursor:
            cursor.execute("SELECT status FROM patient_info WHERE person_id = %s",
                           [person.person_id])
            self.assertIsNone(cursor.fetchone()[0])

    def test_view_mirrors_table_data(self):
        person = Person.objects.create(person_id=880002, year_of_birth=1980)
        PatientRecord.objects.create(person=person, disease='Multiple Myeloma')
        with connection.cursor() as cursor:
            cursor.execute("SELECT disease FROM patient_info WHERE person_id = %s",
                           [person.person_id])
            self.assertEqual(cursor.fetchone()[0], 'Multiple Myeloma')

    def test_view_columns_are_backed_by_the_table(self):
        """
        Every view column except the `status` placeholder maps to a real
        `patient_record` column -- i.e. the rebuild did not invent columns.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s",
                [PatientRecord._meta.db_table],
            )
            table_cols = {row[0] for row in cursor.fetchall()}
        unbacked = set(self._view_columns()) - table_cols - {'status'}
        self.assertEqual(sorted(unbacked), [])

    def test_view_exposes_death_date(self):
        """
        Migration 0139 adds `death_date` to the view. The column has existed on
        the table since 0110, but 0104 froze the view's column list before that,
        so chain-built databases were missing it while staging had it.
        """
        self.assertIn('death_date', self._view_columns())

    def test_death_date_readable_through_view(self):
        person = Person.objects.create(person_id=880004, year_of_birth=1980)
        PatientRecord.objects.create(person=person, death_date=date(2024, 3, 1))
        with connection.cursor() as cursor:
            cursor.execute("SELECT death_date FROM patient_info WHERE person_id = %s",
                           [person.person_id])
            self.assertEqual(cursor.fetchone()[0], date(2024, 3, 1))

    def test_death_date_ordered_after_race(self):
        """
        0139 inserts the column after `race` to reproduce staging's ordinal
        position rather than appending, so `SELECT *` consumers see the same
        column order in every environment.
        """
        cols = self._view_columns()
        self.assertIn('race', cols)
        self.assertEqual(cols[cols.index('race') + 1], 'death_date')

    def test_view_remains_read_only(self):
        """The INSTEAD OF trigger must survive the rebuild."""
        person = Person.objects.create(person_id=880003, year_of_birth=1980)
        PatientRecord.objects.create(person=person)
        # The failed statement aborts the surrounding transaction, so run it
        # inside a savepoint to keep the test's transaction usable.
        with self.assertRaisesMessage(ProgrammingError, 'read-only compatibility view'):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE patient_info SET disease = 'x' WHERE person_id = %s",
                        [person.person_id],
                    )


class WearableConceptMappingTest(TestCase):
    """Guards the concept-mapping invariants that #413 and #415 were caused by.

    These assert on the seed definitions rather than a loaded database, so they
    run everywhere and fail at the point a bad mapping is written rather than
    when an upload silently drops data.
    """

    def _seed_rows(self):
        from omop_core.management.commands.seed_omop_concepts import _CONCEPTS
        return _CONCEPTS

    def test_every_wearable_metric_is_seeded(self):
        """Each metric's (vocabulary, code) must exist in the seed set.

        Without this, upload_wearable resolves None and silently discards every
        sample for the metric while still reporting HTTP 200.
        """
        from omop_core.services.mappings import (
            WEARABLE_CONCEPT_CODE, WEARABLE_CONCEPT_VOCAB,
        )
        seeded = {(r['vocabulary_id'], r['concept_code']) for r in self._seed_rows()}
        missing = [
            (metric, WEARABLE_CONCEPT_VOCAB[metric], code)
            for metric, code in WEARABLE_CONCEPT_CODE.items()
            if (WEARABLE_CONCEPT_VOCAB[metric], code) not in seeded
        ]
        self.assertEqual(missing, [], f'wearable metrics absent from seed set: {missing}')

    def test_seeded_wearable_concept_names_match_their_metric(self):
        """A code that resolves is not necessarily the RIGHT code.

        The original mapping pointed walking_speed and walking_hr_avg at BMI
        concepts and basal_energy at body-fat percentage — all of which resolved
        fine. This asserts the concept_name is semantically consistent, which is
        what a mere code-exists check would have missed.
        """
        from omop_core.services.mappings import (
            WEARABLE_CONCEPT_CODE, WEARABLE_CONCEPT_VOCAB,
        )
        # Substrings that must appear (lowercased) in the seeded concept_name.
        expected_terms = {
            'steps': ['step'],
            'active_minutes': ['exercise', 'activity'],
            'resting_hr': ['heart rate'],
            # HRV terms must be the DISTINGUISHING part of each name, not the
            # shared 'r-r interval' / 'heart rate variability' wording. Both
            # concepts contain that wording, so matching on it would let RMSSD
            # be aliased onto the SDNN code — the #438 defect — and still pass.
            'hrv_sdnn': ['standard deviation'],
            'hrv_rmssd': ['rmssd'],
            'spo2': ['oxygen saturation'],
            'respiratory_rate': ['respiratory rate'],
            'sleep_duration': ['sleep'],
            'vo2_max': ['oxygen consumption'],
            'distance': ['distance'],
            'walking_speed': ['walking speed'],
            'walking_step_length': ['step length'],
            'walking_double_support_pct': ['double support'],
            'walking_hr_avg': ['heart rate'],
            'flights_climbed': ['flights'],
            'active_energy': ['calories', 'energy'],
            'basal_energy': ['basal energy'],
            'body_mass': ['body weight', 'body mass'],
        }
        by_key = {
            (r['vocabulary_id'], r['concept_code']): r['concept_name']
            for r in self._seed_rows()
        }
        for metric, code in WEARABLE_CONCEPT_CODE.items():
            name = by_key.get((WEARABLE_CONCEPT_VOCAB[metric], code))
            self.assertIsNotNone(name, f'{metric} ({code}) not seeded')
            lowered = name.lower()
            self.assertTrue(
                any(term in lowered for term in expected_terms[metric]),
                f'{metric} maps to {code} = "{name}", which does not look like '
                f'{expected_terms[metric]}',
            )

    def test_runtime_migration_matches_seed_definitions(self):
        """Migration 0143 duplicates concept rows; the copies must not drift.

        The migration deliberately hard-codes these rather than importing
        seed_omop_concepts, because a migration must stay frozen against the
        code as it was written. That trade buys correctness on replay and costs
        a consistency check, which is this test.
        """
        from importlib import import_module

        mig = import_module(
            'omop_core.migrations.0143_seed_wearable_runtime_concepts')
        seed_by_key = {
            (r['vocabulary_id'], r['concept_code']): r for r in self._seed_rows()
        }

        mig_rows = [mig._TYPE_CONCEPT] + [
            dict(r, vocabulary_id='HK-Wearable', domain_id='Measurement',
                 concept_class_id='Clinical Observation', source='HealthKey')
            for r in mig._HK_WEARABLE_CONCEPTS
        ]

        for row in mig_rows:
            key = (row['vocabulary_id'], row['concept_code'])
            seeded = seed_by_key.get(key)
            self.assertIsNotNone(
                seeded, f'migration 0143 seeds {key}, which seed_omop_concepts does not')
            for field in ('concept_id', 'concept_name', 'domain_id', 'concept_class_id'):
                self.assertEqual(
                    row[field], seeded[field],
                    f'{key} {field} differs between migration 0143 and seed_omop_concepts')
            self.assertEqual(row.get('source'), seeded.get('source'), f'{key} source differs')

    def test_locally_minted_wearable_concepts_are_installed_by_migration(self):
        """Athena can never supply a local mint, so a migration must.

        start.sh runs only `migrate`; seed_omop_concepts is manual. A metric
        whose concept is locally minted is silently discarded on any deployment
        that never ran the seed command.
        """
        from importlib import import_module
        from omop_core.services.mappings import (
            WEARABLE_CONCEPT_CODE, WEARABLE_CONCEPT_VOCAB,
        )

        mig = import_module(
            'omop_core.migrations.0143_seed_wearable_runtime_concepts')
        installed = {r['concept_code'] for r in mig._HK_WEARABLE_CONCEPTS}
        local_metrics = {
            code for metric, code in WEARABLE_CONCEPT_CODE.items()
            if WEARABLE_CONCEPT_VOCAB[metric] != 'LOINC'
        }
        self.assertEqual(
            local_metrics - installed, set(),
            'locally-minted wearable concepts not installed by any migration')

    def test_no_duplicate_vocabulary_code_pairs_in_seed(self):
        """Two seed rows must never claim the same (vocabulary_id, concept_code).

        A duplicate pair makes concept resolution nondeterministic, since
        concept_by_vocab does .first() with no ordering.
        """
        seen, dupes = set(), []
        for row in self._seed_rows():
            key = (row['vocabulary_id'], row['concept_code'])
            if key in seen:
                dupes.append(key)
            seen.add(key)
        self.assertEqual(dupes, [], f'duplicate (vocabulary_id, concept_code): {dupes}')

    def test_local_mints_are_quarantined(self):
        """source='HealthKey' <-> HK-* vocabulary <-> concept_id >= 2e9."""
        from omop_core.management.commands.seed_omop_concepts import (
            _assert_local_mint_convention, LOCAL_CONCEPT_ID_MIN,
        )
        _assert_local_mint_convention()  # raises CommandError on violation

        for row in self._seed_rows():
            if row['vocabulary_id'].startswith('HK-'):
                self.assertEqual(
                    row.get('source'), 'HealthKey',
                    f"{row['concept_code']} is HK-* but not tagged as a HealthKey mint")
                self.assertGreaterEqual(
                    row['concept_id'], LOCAL_CONCEPT_ID_MIN,
                    f"{row['concept_code']} is a local mint below the OHDSI custom range")
            else:
                self.assertIsNone(
                    row.get('source'),
                    f"{row['concept_code']} is in an external vocabulary but tagged local")

    def test_no_local_mint_shadows_an_external_code(self):
        """A local mint must not reuse a real LOINC/SNOMED/RxNorm code string.

        Reusing one asserts an externally-defined meaning for a locally-authored
        concept and makes code-only lookups ambiguous.
        """
        from omop_core.services.mappings import (
            WEARABLE_CONCEPT_CODE, WEARABLE_CONCEPT_VOCAB,
        )
        for metric, code in WEARABLE_CONCEPT_CODE.items():
            if WEARABLE_CONCEPT_VOCAB[metric] == 'HK-Wearable':
                self.assertTrue(
                    code.startswith('HK-'),
                    f'{metric} is locally minted but uses external-looking code {code}')

    def test_observation_metric_set_matches_seeded_domains(self):
        """WEARABLE_OBSERVATION_METRICS must agree with the seeded domain_ids."""
        from omop_core.services.mappings import (
            WEARABLE_CONCEPT_CODE, WEARABLE_CONCEPT_VOCAB, WEARABLE_OBSERVATION_METRICS,
        )
        by_key = {
            (r['vocabulary_id'], r['concept_code']): r['domain_id']
            for r in self._seed_rows()
        }
        for metric, code in WEARABLE_CONCEPT_CODE.items():
            domain = by_key.get((WEARABLE_CONCEPT_VOCAB[metric], code))
            expected = 'Observation' if metric in WEARABLE_OBSERVATION_METRICS else 'Measurement'
            self.assertEqual(
                domain, expected,
                f'{metric} ({code}) is seeded domain {domain} but the metric set says {expected}')

class PurgeBrokenWearableRowsCommandTest(TestCase):
    """Covers purge_broken_wearable_rows against real rows.

    The command this replaced accrued three defects that only surfaced when it
    was run against staging, all because its fixtures moved two rows against an
    otherwise empty database. These tests use enough rows to cross the chunk
    boundary and assert on signal behaviour, not just end state.
    """

    @classmethod
    def setUpTestData(cls):
        from omop_core.models import Person
        call_command('seed_omop_concepts', verbosity=0)
        cls.person = Person.objects.create(person_id=770001, year_of_birth=1970)

    def setUp(self):
        self._next_pk = 900001

    def _mint(self, concept_id, concept_code, domain_id='Measurement'):
        """Recreate a retired 900xxxx mint, as a pre-#413 database would have."""
        from omop_core.models import Concept, ConceptClass, Domain, Vocabulary
        return Concept.objects.create(
            concept_id=concept_id,
            concept_name=f'Retired mint {concept_code}',
            domain=Domain.objects.get(domain_id=domain_id),
            vocabulary=Vocabulary.objects.get(vocabulary_id='LOINC'),
            concept_class=ConceptClass.objects.get(concept_class_id='Clinical Observation'),
            standard_concept='S',
            concept_code=concept_code,
            valid_start_date=date(1970, 1, 1),
            valid_end_date=date(2099, 12, 31),
        )

    def _measurement(self, concept, value, day=1, source_value=None):
        """Create a Measurement with an explicitly allocated pk.

        Deriving the pk from a live row count collides once the command starts
        deleting rows, producing duplicate-key failures in unrelated tests.
        """
        from omop_core.models import Concept, Measurement
        self._next_pk += 1
        return Measurement.objects.create(
            measurement_id=self._next_pk,
            person=self.person,
            measurement_concept=concept,
            measurement_date=date(2024, 7, (day % 28) + 1),
            measurement_type_concept=Concept.objects.get(concept_id=32856),
            value_as_number=value,
            measurement_source_value=source_value or concept.concept_code,
        )

    def test_dry_run_deletes_nothing(self):
        from omop_core.models import Measurement

        mint = self._mint(9001019, '55423-8')
        self._measurement(mint, 7000)

        call_command('purge_broken_wearable_rows', '--dry-run', verbosity=0)

        self.assertEqual(Measurement.objects.count(), 1)
        self.assertTrue(Concept.objects.filter(concept_id=9001019).exists())

    def test_apply_deletes_rows_on_retired_mints(self):
        from omop_core.models import Measurement

        mint = self._mint(9001019, '55423-8')
        for day in range(1, 6):
            self._measurement(mint, 7000 + day, day=day)

        call_command('purge_broken_wearable_rows', '--apply', verbosity=0)

        self.assertEqual(
            Measurement.objects.filter(measurement_concept_id=9001019).count(), 0)

    def test_apply_deletes_rows_carrying_broken_codes(self):
        """Rows on the correct concept but a superseded code must also go."""
        from omop_core.models import Concept, Measurement

        good = Concept.objects.get(concept_id=3040891)  # resting HR, a valid concept
        self._measurement(good, 58, source_value='41909-3')  # walking_speed -> BMI

        call_command('purge_broken_wearable_rows', '--apply', verbosity=0)

        self.assertEqual(
            Measurement.objects.filter(measurement_source_value='41909-3').count(), 0)

    def test_shared_vitals_codes_are_never_touched(self):
        """body_mass and spo2 codes are written by the vitals path too.

        Matching them by source_value would delete non-wearable clinical data,
        so they are only removed when sitting on a retired mint.
        """
        from omop_core.models import Concept, Measurement

        weight = Concept.objects.get(concept_id=3025315)   # 29463-7 Body weight
        spo2 = Concept.objects.get(concept_id=40762499)    # 59408-5 SpO2
        self._measurement(weight, 68.4)
        self._measurement(spo2, 97.0)

        call_command('purge_broken_wearable_rows', '--apply', verbosity=0)

        self.assertEqual(Measurement.objects.filter(
            measurement_source_value='29463-7').count(), 1)
        self.assertEqual(Measurement.objects.filter(
            measurement_source_value='59408-5').count(), 1)

    def test_apply_does_not_trigger_patient_record_refresh(self):
        """Deletion must not fire per-row PatientRecord re-derivations.

        QuerySet.delete() sends post_delete per row and omop_core/signals.py
        turns each Measurement deletion into a full refresh_patient_record.
        Unsuppressed, purging ~127k staging rows means ~127k re-derivations.
        """
        mint = self._mint(9001019, '55423-8')
        for day in range(1, 11):
            self._measurement(mint, 7000 + day, day=day)

        with patch(
            'omop_core.services.patient_record_service.refresh_patient_record'
        ) as mock_refresh:
            call_command('purge_broken_wearable_rows', '--apply', verbosity=0)

        self.assertEqual(
            mock_refresh.call_count, 0,
            f'purge triggered {mock_refresh.call_count} PatientRecord refresh(es)')
        # Guard against the assertion passing simply because nothing happened.
        from omop_core.models import Measurement
        self.assertEqual(Measurement.objects.count(), 0, 'rows should have been deleted')

    def test_apply_deletes_more_rows_than_one_chunk(self):
        from omop_core.management.commands import purge_broken_wearable_rows as cmd
        from omop_core.models import Measurement

        mint = self._mint(9001019, '55423-8')
        for day in range(1, 16):
            self._measurement(mint, 7000 + day, day=day)

        with patch.object(cmd, 'CHUNK_SIZE', 4):
            call_command('purge_broken_wearable_rows', '--apply', verbosity=0)

        self.assertEqual(Measurement.objects.count(), 0,
                         'chunked deletion must drain every row')

    def test_apply_deletes_retired_mint_concepts(self):
        mint = self._mint(9001019, '55423-8')
        self._measurement(mint, 7000)

        call_command('purge_broken_wearable_rows', '--apply', verbosity=0)

        self.assertFalse(Concept.objects.filter(concept_id=9001019).exists())

    def test_keep_mints_leaves_the_concept_in_place(self):
        mint = self._mint(9001019, '55423-8')
        self._measurement(mint, 7000)

        call_command('purge_broken_wearable_rows', '--apply', '--keep-mints',
                     verbosity=0)

        self.assertTrue(Concept.objects.filter(concept_id=9001019).exists())

    def test_affected_patient_records_are_marked_stale(self):
        """Otherwise `backfill_patient_records` reports a false all-clear.

        The plain backfill selects on derivation_version, which a purge does not
        otherwise touch — so summaries derived from now-deleted rows would be
        served indefinitely unless the operator remembered --all.
        """
        from omop_core.models import PatientRecord

        PatientRecord.objects.filter(person=self.person).update(derivation_version=99)
        mint = self._mint(9001019, '55423-8')
        self._measurement(mint, 7000)

        call_command('purge_broken_wearable_rows', '--apply', verbosity=0)

        self.assertEqual(
            PatientRecord.objects.get(person=self.person).derivation_version, 0,
            'affected PatientRecords must be re-derivable by the ordinary backfill')


class ConceptZeroTest(TestCase):
    """concept 0 is OMOP's universal 'No matching concept' sentinel (see #427).

    It is written to any *_concept_id when source data cannot be mapped, so it
    is domain-agnostic by design. One database had it stored as
    (HK-Labs, Measurement, Lab Test), which made ~20,800 unmapped rows across
    every domain look like HealthKey lab tests to any query grouping by
    vocabulary or domain.
    """

    @classmethod
    def setUpTestData(cls):
        call_command('seed_omop_concepts', verbosity=0)

    def test_seeded_with_omop_specified_metadata(self):
        from omop_core.models import Concept

        c = Concept.objects.get(concept_id=0)
        self.assertEqual(c.concept_name, 'No matching concept')
        self.assertEqual(c.domain_id, 'Metadata',
                         'concept 0 is domain-agnostic; a real domain misattributes '
                         'every unmapped row of every other domain')
        self.assertEqual(c.vocabulary_id, 'None')
        self.assertEqual(c.concept_class_id, 'Undefined')
        self.assertIsNone(c.standard_concept)

    def test_not_marked_as_a_local_mint(self):
        """concept 0 is standard OMOP content, not HealthKey-authored.

        Tagging it 'HealthKey' pollutes the ?source=external vocabulary-mirror
        filter in the opposite direction from the rest of #415.
        """
        from omop_core.models import Concept

        self.assertIsNone(Concept.objects.get(concept_id=0).source)

    def test_placeholder_vocabulary_and_class_are_seeded(self):
        """seed_omop_concepts referenced vocabulary 'None' for the generic-lab
        fallback but never seeded it, so seeding an empty database raised
        IntegrityError — conftest.py documents working around exactly this."""
        from omop_core.models import ConceptClass, Vocabulary

        self.assertTrue(Vocabulary.objects.filter(vocabulary_id='None').exists())
        self.assertTrue(ConceptClass.objects.filter(concept_class_id='Undefined').exists())

    def test_migration_corrects_a_misfiled_concept_zero(self):
        """Reproduces the staging state and asserts the migration logic fixes it."""
        import importlib
        # Module name starts with a digit, so it cannot be imported normally.
        _0140 = importlib.import_module(
            'omop_core.migrations.0140_fix_concept_zero_metadata')
        from django.apps import apps as global_apps
        from omop_core.models import Concept, ConceptClass, Domain, Vocabulary

        hk, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='HK-Labs',
            defaults={'vocabulary_name': 'HK-Labs', 'vocabulary_concept_id': 0})
        c = Concept.objects.get(concept_id=0)
        c.vocabulary = hk
        c.domain = Domain.objects.get(domain_id='Measurement')
        c.concept_class = ConceptClass.objects.get(concept_class_id='Lab Test')
        c.source = 'HealthKey'
        c.save()

        _0140.fix_concept_zero(global_apps, None)

        c.refresh_from_db()
        self.assertEqual(c.domain_id, 'Metadata')
        self.assertEqual(c.vocabulary_id, 'None')
        self.assertEqual(c.concept_class_id, 'Undefined')
        self.assertIsNone(c.source)



class BackfillConceptSourceCommandTest(TestCase):
    """Covers backfill_concept_source (see #415).

    Concept.source separates locally-authored rows from vocabulary-release rows.
    It was added after most rows existed and never populated, so the column
    claims a guarantee it does not provide — on staging 19 of 1,979,422 rows
    were tagged while 224 were demonstrably local.
    """

    @classmethod
    def setUpTestData(cls):
        call_command('seed_omop_concepts', verbosity=0)

    def _concept(self, concept_id, code, vocabulary_id='LOINC', source=None):
        from omop_core.models import Concept, ConceptClass, Domain, Vocabulary
        vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id=vocabulary_id,
            defaults={'vocabulary_name': vocabulary_id, 'vocabulary_concept_id': 0})
        return Concept.objects.create(
            concept_id=concept_id,
            concept_name=f'Test {code}',
            domain=Domain.objects.get(domain_id='Measurement'),
            vocabulary=vocab,
            concept_class=ConceptClass.objects.get(concept_class_id='Clinical Observation'),
            standard_concept='S',
            concept_code=code,
            valid_start_date=date(1970, 1, 1),
            valid_end_date=date(2099, 12, 31),
            source=source,
        )

    def test_dry_run_writes_nothing(self):
        from omop_core.models import Concept

        self._concept(9001099, 'X-1')
        call_command('backfill_concept_source', '--dry-run', verbosity=0)
        self.assertIsNone(Concept.objects.get(concept_id=9001099).source)

    def test_tags_each_category_of_local_row(self):
        from omop_core.models import Concept

        self._concept(9001099, 'X-1')                                  # seed mint range
        self._concept(392021999, 'X-2', vocabulary_id='SNOMED')        # FHIR ingest block
        self._concept(2_100_000_001, 'X-3', vocabulary_id='HemOnc')    # OHDSI custom range
        self._concept(500001, 'X-4', vocabulary_id='LOCAL')            # local vocabulary
        self._concept(500002, 'X-5', vocabulary_id='HK-Labs')          # HK-* vocabulary

        call_command('backfill_concept_source', '--apply', verbosity=0)

        for cid in (9001099, 392021999, 2_100_000_001, 500001, 500002):
            self.assertEqual(
                Concept.objects.get(concept_id=cid).source, 'HealthKey',
                f'concept {cid} should have been tagged as locally minted')

    def test_genuine_vocabulary_rows_are_left_alone(self):
        """The overwhelming majority of rows are Athena content and must not be
        touched — a predicate that swept them up would relabel 1.98M rows as
        locally authored and destroy the column's meaning."""
        from omop_core.models import Concept

        genuine = self._concept(3025999, 'X-6', vocabulary_id='LOINC')
        call_command('backfill_concept_source', '--apply', verbosity=0)
        genuine.refresh_from_db()
        self.assertIsNone(genuine.source)

    def test_existing_source_values_are_not_overwritten(self):
        from omop_core.models import Concept

        self._concept(500003, 'X-7', vocabulary_id='HK-Labs', source='SomethingElse')
        call_command('backfill_concept_source', '--apply', verbosity=0)
        self.assertEqual(
            Concept.objects.get(concept_id=500003).source, 'SomethingElse',
            'the command must only fill NULLs, never relabel an existing value')

    def test_rule_flag_limits_scope(self):
        from omop_core.models import Concept

        self._concept(9001099, 'X-1')                            # seed mint
        self._concept(500001, 'X-4', vocabulary_id='LOCAL')      # local vocabulary

        call_command('backfill_concept_source', '--apply', '--rule', 'seed_mint',
                     verbosity=0)

        self.assertEqual(Concept.objects.get(concept_id=9001099).source, 'HealthKey')
        self.assertIsNone(Concept.objects.get(concept_id=500001).source)

    def test_is_idempotent(self):
        from omop_core.models import Concept

        self._concept(9001099, 'X-1')
        call_command('backfill_concept_source', '--apply', verbosity=0)
        call_command('backfill_concept_source', '--apply', verbosity=0)
        self.assertEqual(
            Concept.objects.filter(concept_id=9001099, source='HealthKey').count(), 1)


class RemapLocalDrugConceptsCommandTest(TestCase):
    """Covers remap_local_drug_concepts (see #427).

    Six drug concepts were minted locally with the drug's name as concept_code
    instead of being resolved against Athena. The clinical content is right, but
    the mint has no vocabulary edges at all — the genuine HemOnc olaparib
    concept participates in 63 relationships, the mint in zero — so the
    exposures are invisible to any standard drug-class or indication query.
    """

    @classmethod
    def setUpTestData(cls):
        from omop_core.models import Person
        call_command('seed_omop_concepts', verbosity=0)
        cls.person = Person.objects.create(person_id=780001, year_of_birth=1970)

    def _concept(self, concept_id, code, name, vocabulary_id, standard=None):
        from omop_core.models import Concept, ConceptClass, Domain, Vocabulary
        vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id=vocabulary_id,
            defaults={'vocabulary_name': vocabulary_id, 'vocabulary_concept_id': 0})
        domain, _ = Domain.objects.get_or_create(
            domain_id='Drug', defaults={'domain_name': 'Drug', 'domain_concept_id': 0})
        return Concept.objects.create(
            concept_id=concept_id, concept_name=name, domain=domain, vocabulary=vocab,
            concept_class=ConceptClass.objects.get(concept_class_id='Clinical Observation'),
            standard_concept=standard, concept_code=code,
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31))

    def _setup_olaparib(self, n_exposures=3):
        """Mint, HemOnc source concept and Standard target, as on staging."""
        from omop_core.models import Concept, DrugExposure

        mint = self._concept(2012334076, 'Olaparib', 'Olaparib', 'HemOnc')
        self._concept(35803216, '366', 'Olaparib', 'HemOnc')
        self._concept(45892579, '1597582', 'olaparib', 'RxNorm', standard='S')
        for i in range(n_exposures):
            DrugExposure.objects.create(
                drug_exposure_id=880001 + i,
                person=self.person,
                drug_concept=mint,
                drug_exposure_start_date=date(2024, 7, i + 1),
                drug_type_concept=Concept.objects.get(concept_id=32869),
            )
        return mint

    def test_dry_run_writes_nothing(self):
        from omop_core.models import Concept, DrugExposure

        self._setup_olaparib()
        call_command('remap_local_drug_concepts', '--dry-run', verbosity=0)
        self.assertEqual(
            DrugExposure.objects.filter(drug_concept_id=2012334076).count(), 3)
        self.assertTrue(Concept.objects.filter(concept_id=2012334076).exists())

    def test_apply_points_drug_concept_at_the_standard_concept(self):
        """drug_concept_id must hold a Standard concept.

        RxNorm is standard for the Drug domain, so the HemOnc drug concept is
        non-standard by design — pointing at it would swap one non-standard
        concept for another.
        """
        from omop_core.models import DrugExposure

        self._setup_olaparib()
        call_command('remap_local_drug_concepts', '--apply', verbosity=0)

        rows = DrugExposure.objects.filter(person=self.person)
        self.assertEqual(rows.count(), 3)
        for row in rows:
            self.assertEqual(row.drug_concept_id, 45892579)
            self.assertEqual(row.drug_concept.standard_concept, 'S')

    def test_apply_preserves_provenance_in_drug_source_concept(self):
        from omop_core.models import DrugExposure

        self._setup_olaparib()
        call_command('remap_local_drug_concepts', '--apply', verbosity=0)
        self.assertEqual(
            DrugExposure.objects.filter(person=self.person).first().drug_source_concept_id,
            35803216, 'the HemOnc concept records what was actually stated')

    def test_apply_deletes_the_mint(self):
        from omop_core.models import Concept

        self._setup_olaparib()
        call_command('remap_local_drug_concepts', '--apply', verbosity=0)
        self.assertFalse(Concept.objects.filter(concept_id=2012334076).exists())

    def test_keep_mints_leaves_the_concept(self):
        from omop_core.models import Concept

        self._setup_olaparib()
        call_command('remap_local_drug_concepts', '--apply', '--keep-mints', verbosity=0)
        self.assertTrue(Concept.objects.filter(concept_id=2012334076).exists())

    def test_affected_patient_records_are_marked_stale(self):
        """Therapy fields derive from drug_exposure, and queryset .update()
        sends no signals, so nothing would otherwise notice the change."""
        from omop_core.models import PatientRecord

        self._setup_olaparib()
        PatientRecord.objects.filter(person=self.person).update(derivation_version=99)
        call_command('remap_local_drug_concepts', '--apply', verbosity=0)
        self.assertEqual(
            PatientRecord.objects.get(person=self.person).derivation_version, 0)

    def test_skips_when_the_target_concept_is_absent(self):
        """On a database with no Athena load the targets do not exist. The
        command must leave the data alone rather than repoint it at nothing."""
        from omop_core.models import Concept, DrugExposure

        mint = self._concept(2012334076, 'Olaparib', 'Olaparib', 'HemOnc')
        DrugExposure.objects.create(
            drug_exposure_id=880900, person=self.person, drug_concept=mint,
            drug_exposure_start_date=date(2024, 7, 1),
            drug_type_concept=Concept.objects.get(concept_id=32869))

        call_command('remap_local_drug_concepts', '--apply', verbosity=0)

        self.assertEqual(
            DrugExposure.objects.get(drug_exposure_id=880900).drug_concept_id, 2012334076)
        self.assertTrue(Concept.objects.filter(concept_id=2012334076).exists())

    def test_refuses_a_non_standard_target(self):
        """Guards the mapping constant itself: if a listed target stops being
        Standard in a later vocabulary release, do not silently use it."""
        from omop_core.models import Concept, DrugExposure

        self._setup_olaparib()
        Concept.objects.filter(concept_id=45892579).update(standard_concept=None)

        call_command('remap_local_drug_concepts', '--apply', verbosity=0)

        self.assertEqual(
            DrugExposure.objects.filter(drug_concept_id=2012334076).count(), 3,
            'rows must be left alone when the target is not Standard')


# ===========================================================================
# Issue #434: re-derivation must not erase hand-entered values
# ===========================================================================

class CandidateUserEditedFieldsTest(TestCase):
    """Which edited fields need a fallback until derivation proves otherwise."""

    def test_derived_fields_are_flagged(self):
        from omop_core.services.omop_write_service import candidate_user_edited_fields

        self.assertEqual(
            candidate_user_edited_fields({'tumor_stage', 'her2_status', 'smoking_status'}),
            {'tumor_stage', 'her2_status', 'smoking_status'},
        )

    def test_non_derived_fields_are_never_flagged(self):
        """email and date_of_birth live on PatientRecord and are never cleared,
        so they need no preservation."""
        from omop_core.services.omop_write_service import candidate_user_edited_fields

        self.assertEqual(candidate_user_edited_fields({'email', 'date_of_birth'}), set())

    def test_fields_inside_a_trigger_set_are_still_flagged(self):
        """`stage` is in CONDITION_FIELDS and the therapy dates are in
        THERAPY_LINE_FIELDS, but _sync_condition writes only `disease` and
        _sync_therapy_line bails without a therapy name. Treating those trigger
        sets as proof of a round-trip is what let #434's stage='I' disappear."""
        from omop_core.services.omop_write_service import candidate_user_edited_fields

        self.assertEqual(candidate_user_edited_fields({'stage'}), {'stage'})
        self.assertEqual(
            candidate_user_edited_fields({'first_line_start_date'}),
            {'first_line_start_date'},
        )

    def test_patient_age_is_flagged_despite_triggering_the_demographic_sync(self):
        """_sync_demographics writes only gender and the birth date; a typed age
        has nowhere to land."""
        from omop_core.services.omop_write_service import candidate_user_edited_fields

        self.assertEqual(candidate_user_edited_fields({'patient_age'}), {'patient_age'})


class PreserveUserEditedFieldsTest(_OmopBase):
    """refresh_patient_record must not blank values OMOP cannot reproduce."""

    PERSON_ID = 90600

    def test_hand_entered_value_survives_re_derivation(self):
        """The staging symptom in #434: set in the UI, gone after the next refresh."""
        PatientRecord.objects.create(
            person=self.person,
            tumor_stage='T2',
            user_edited_fields=['tumor_stage'],
        )

        pi = refresh_patient_record(self.person)

        self.assertEqual(pi.tumor_stage, 'T2')

    def test_unflagged_value_is_still_cleared(self):
        """Preservation is opt-in per field. A derived field nobody edited must
        still be blanked, or deletions in OMOP would stop propagating."""
        PatientRecord.objects.create(person=self.person, tumor_stage='T2')

        pi = refresh_patient_record(self.person)

        self.assertIsNone(pi.tumor_stage)

    def test_omop_wins_when_it_has_a_value(self):
        """A hand-entered value is a fallback, not a pin: once OMOP can answer
        for the field, the derived value takes over."""
        PatientRecord.objects.create(
            person=self.person, stage='I', user_edited_fields=['stage'],
        )
        stage_concept = _concept(90610, 'Stage group.clinical', self.dom_meas,
                                 self.vocab, self.cc, code='21908-9')
        # Creating this fires the post_save refresh, so the assertion below covers
        # the real path as well as the explicit call.
        Measurement.objects.create(
            measurement_id=90611,
            person=self.person,
            measurement_concept=stage_concept,
            measurement_date=date(2026, 1, 15),
            measurement_type_concept=self.type_concept,
            value_as_string='III',
            measurement_source_value='21908-9',
        )

        pi = refresh_patient_record(self.person)

        self.assertEqual(pi.stage, 'III')

    def test_zero_is_preserved_as_a_real_answer(self):
        """Zero drinks a week is an answer, not an absence — a falsy check drops it."""
        PatientRecord.objects.create(
            person=self.person, drinks_per_week=0, user_edited_fields=['drinks_per_week'],
        )

        pi = refresh_patient_record(self.person)

        self.assertEqual(pi.drinks_per_week, 0)

    def test_false_is_preserved_as_a_real_answer(self):
        PatientRecord.objects.create(
            person=self.person,
            transformed_to_dlbcl=False,
            user_edited_fields=['transformed_to_dlbcl'],
        )

        pi = refresh_patient_record(self.person)

        self.assertIs(pi.transformed_to_dlbcl, False)

    def test_derivation_taking_over_drops_the_flag(self):
        """Once OMOP answers for a flagged field, the field stops being tracked
        as hand-entered — otherwise the next snapshot would capture OMOP's own
        value and treat it as the user's."""
        PatientRecord.objects.create(
            person=self.person, stage='I', user_edited_fields=['stage', 'her2_status'],
        )
        stage_concept = _concept(90612, 'Stage group.clinical', self.dom_meas,
                                 self.vocab, self.cc, code='21908-9')
        Measurement.objects.create(
            measurement_id=90613,
            person=self.person,
            measurement_concept=stage_concept,
            measurement_date=date(2026, 1, 15),
            measurement_type_concept=self.type_concept,
            value_as_string='III',
            measurement_source_value='21908-9',
        )

        pi = refresh_patient_record(self.person)

        self.assertEqual(pi.stage, 'III')
        # her2_status is still unanswered by OMOP, so it stays tracked.
        self.assertEqual(pi.user_edited_fields, ['her2_status'])

    def test_derived_value_is_not_resurrected_after_its_source_is_deleted(self):
        """The failure the hand-off prevents: user types a value, OMOP overrides
        it, the OMOP row is then deleted. Without dropping the flag the refresh
        would restore OMOP's old value — one nobody typed and no table backs."""
        PatientRecord.objects.create(
            person=self.person, stage='I', user_edited_fields=['stage'],
        )
        stage_concept = _concept(90614, 'Stage group.clinical', self.dom_meas,
                                 self.vocab, self.cc, code='21908-9')
        m = Measurement.objects.create(
            measurement_id=90615,
            person=self.person,
            measurement_concept=stage_concept,
            measurement_date=date(2026, 1, 15),
            measurement_type_concept=self.type_concept,
            value_as_string='III',
            measurement_source_value='21908-9',
        )
        self.assertEqual(refresh_patient_record(self.person).stage, 'III')

        m.delete()
        pi = refresh_patient_record(self.person)

        self.assertIsNone(pi.stage)

    def test_flagging_an_unknown_field_is_harmless(self):
        """user_edited_fields is written by the service, but a stale entry left
        by a renamed field must not break the refresh for the whole patient."""
        PatientRecord.objects.create(
            person=self.person,
            email='patient@example.com',
            user_edited_fields=['email', 'not_a_field_at_all'],
        )

        pi = refresh_patient_record(self.person)

        self.assertEqual(pi.email, 'patient@example.com')


class SyncToOmopMarksUserEditedTest(_OmopBase):
    """The write-through records what it could not persist."""

    PERSON_ID = 90620

    def test_unsynced_edit_is_recorded(self):
        from omop_core.services.omop_write_service import sync_to_omop

        pi = PatientRecord.objects.create(person=self.person, her2_status='positive')
        sync_to_omop(pi, {'her2_status'})

        pi.refresh_from_db()
        self.assertEqual(pi.user_edited_fields, ['her2_status'])

    def test_non_derived_edit_is_not_recorded(self):
        """email is never cleared by a refresh, so it needs no fallback."""
        from omop_core.services.omop_write_service import sync_to_omop

        pi = PatientRecord.objects.create(person=self.person, email='p@example.com')
        sync_to_omop(pi, {'email'})

        pi.refresh_from_db()
        self.assertEqual(pi.user_edited_fields, [])

    def test_a_field_omop_owns_unflags_itself_on_the_next_refresh(self):
        """Flagging is generous on the write side; the read side hands the field
        back to OMOP as soon as derivation can answer for it. Without that,
        nothing would ever remove a flag and deletions would stop propagating."""
        from omop_core.services.omop_write_service import sync_to_omop

        _concept(3000963, 'Laboratory test result', self.dom_meas, self.vocab, self.cc)
        pi = PatientRecord.objects.create(person=self.person, hemoglobin_g_dl=11.2)
        sync_to_omop(pi, {'hemoglobin_g_dl'})
        pi.refresh_from_db()
        self.assertEqual(pi.user_edited_fields, ['hemoglobin_g_dl'])

        pi = refresh_patient_record(self.person)

        self.assertAlmostEqual(float(pi.hemoglobin_g_dl), 11.2, places=1)
        self.assertEqual(pi.user_edited_fields, [])

    def test_repeated_edits_accumulate_without_duplicating(self):
        from omop_core.services.omop_write_service import sync_to_omop

        pi = PatientRecord.objects.create(person=self.person, her2_status='positive')
        sync_to_omop(pi, {'her2_status'})
        pi.smoking_status = 'never'
        sync_to_omop(pi, {'her2_status', 'smoking_status'})

        pi.refresh_from_db()
        self.assertEqual(pi.user_edited_fields, ['her2_status', 'smoking_status'])

    def test_clearing_a_field_is_recorded_too(self):
        """Blanking a field is an edit. Recording it only when a value is present
        would leave the flag unset exactly when the user meant 'none'."""
        from omop_core.services.omop_write_service import sync_to_omop

        pi = PatientRecord.objects.create(person=self.person, her2_status=None)
        sync_to_omop(pi, {'her2_status'})

        pi.refresh_from_db()
        self.assertEqual(pi.user_edited_fields, ['her2_status'])


class PerformanceStatusFromEitherTableTest(_OmopBase):
    """ECOG/Karnofsky reach OMOP as `observation` via FHIR upload and as
    `measurement` via the PatientRecord write-through. Both must be read."""

    PERSON_ID = 90640

    def _kps_concept(self):
        return _concept(90641, 'Karnofsky Performance Status score', self.dom_meas,
                        self.vocab, self.cc, code='89243-0')

    def _ecog_concept(self):
        return _concept(90642, 'ECOG Performance Status score', self.dom_obs,
                        self.vocab, self.cc, code='89247-1')

    def test_karnofsky_read_from_measurement(self):
        """The staging case: the write-through wrote a Measurement that the
        Observation-only reader could not see."""
        Measurement.objects.create(
            measurement_id=90643,
            person=self.person,
            measurement_concept=self._kps_concept(),
            measurement_date=date(2026, 8, 5),
            measurement_type_concept=self.type_concept,
            value_as_number=100,
            measurement_source_value='89243-0',
        )

        pi = refresh_patient_record(self.person)

        self.assertEqual(pi.karnofsky_performance_score, 100)

    def test_ecog_read_from_measurement(self):
        Measurement.objects.create(
            measurement_id=90644,
            person=self.person,
            measurement_concept=self._ecog_concept(),
            measurement_date=date(2026, 8, 5),
            measurement_type_concept=self.type_concept,
            value_as_number=1,
            measurement_source_value='89247-1',
        )

        pi = refresh_patient_record(self.person)

        self.assertEqual(pi.ecog_performance_status, 1)
        self.assertEqual(pi.ecog_assessment_date, date(2026, 8, 5))

    def test_ecog_still_read_from_observation(self):
        Observation.objects.create(
            observation_id=90645,
            person=self.person,
            observation_concept=self._ecog_concept(),
            observation_date=date(2026, 8, 5),
            observation_type_concept=self.type_concept,
            value_as_number=2,
        )

        pi = refresh_patient_record(self.person)

        self.assertEqual(pi.ecog_performance_status, 2)

    def test_most_recent_wins_across_both_tables(self):
        """A newer Measurement must beat an older Observation: the two sources
        are one timeline, not a preferred table plus a fallback."""
        Observation.objects.create(
            observation_id=90646,
            person=self.person,
            observation_concept=self._ecog_concept(),
            observation_date=date(2026, 1, 1),
            observation_type_concept=self.type_concept,
            value_as_number=3,
        )
        Measurement.objects.create(
            measurement_id=90647,
            person=self.person,
            measurement_concept=self._ecog_concept(),
            measurement_date=date(2026, 8, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=0,
            measurement_source_value='89247-1',
        )

        pi = refresh_patient_record(self.person)

        self.assertEqual(pi.ecog_performance_status, 0)
        self.assertEqual(pi.ecog_assessment_date, date(2026, 8, 1))

    def test_a_same_day_correction_beats_the_bundle_it_corrects(self):
        """_sync_measurement stamps today, so a clinician's PATCH lands on the
        same date as a bundle uploaded that morning. The correction must win the
        tie or the next refresh silently reverts it."""
        same_day = date(2026, 8, 5)
        Observation.objects.create(
            observation_id=90649,
            person=self.person,
            observation_concept=self._ecog_concept(),
            observation_date=same_day,
            observation_type_concept=self.type_concept,
            value_as_number=3,
        )
        Measurement.objects.create(
            measurement_id=90650,
            person=self.person,
            measurement_concept=self._ecog_concept(),
            measurement_date=same_day,
            measurement_type_concept=self.type_concept,
            value_as_number=1,
            measurement_source_value='89247-1',
        )

        pi = refresh_patient_record(self.person)

        self.assertEqual(pi.ecog_performance_status, 1)

    def test_matched_by_loinc_when_the_concept_name_does_not_say_karnofsky(self):
        """Vocabulary-poor environments keep the LOINC in the source value only."""
        generic = _concept(3000963, 'Laboratory test result', self.dom_meas,
                           self.vocab, self.cc)
        Measurement.objects.create(
            measurement_id=90648,
            person=self.person,
            measurement_concept=generic,
            measurement_date=date(2026, 8, 5),
            measurement_type_concept=self.type_concept,
            value_as_number=90,
            measurement_source_value='89243-0',
        )

        pi = refresh_patient_record(self.person)

        self.assertEqual(pi.karnofsky_performance_score, 90)


class EnrichDemoOmopDataTest(_OmopBase):
    """enrich_demo_omop_data inserts only the OMOP rows a cohort is missing."""

    PERSON_ID = 90700

    def setUp(self):
        _concept(3000963, 'Laboratory test result', self.dom_meas, self.vocab, self.cc)
        _concept(32856, 'Lab', self.dom_meas, self.vocab, self.cc)

    def _record(self, person_id, disease):
        person = Person.objects.create(
            person_id=person_id, year_of_birth=1970,
            gender_source_value='female', race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        # Backed by a real condition row: `disease` is OMOP-derived, so a
        # PatientRecord that only carries it as a column loses it on the first
        # refresh and drops out of the command's cohort selection.
        ConditionOccurrence.objects.create(
            condition_occurrence_id=person_id,
            person=person,
            condition_concept=_concept(
                900000 + (person_id % 1000), disease, self.dom_cond,
                self.vocab, self.cc),
            condition_start_date=date(2025, 1, 10),
            condition_type_concept=self.type_concept,
            condition_source_value=disease,
        )
        return PatientRecord.objects.get(person=person)

    def _codes(self, person_id):
        return set(
            Measurement.objects.filter(person_id=person_id)
            .values_list('measurement_source_value', flat=True)
        )

    def test_dry_run_writes_nothing(self):
        self._record(90701, 'Malignant tumor of breast')

        call_command('enrich_demo_omop_data', verbosity=0)

        self.assertEqual(Measurement.objects.filter(person_id=90701).count(), 0)

    def test_breast_cohort_gets_vitals_performance_and_grade(self):
        self._record(90702, 'Malignant tumor of breast')

        call_command('enrich_demo_omop_data', '--apply', verbosity=0)

        codes = self._codes(90702)
        for loinc in ('8302-2', '29463-7', '8867-4', '20570-8',
                      '8480-6', '8462-4', '89247-1', '89243-0', '2532-0', '44648-4'):
            self.assertIn(loinc, codes, f'breast cohort missing {loinc}')
        # ANC/eGFR already exist for the breast cohort upstream — not its gap.
        self.assertNotIn('751-8', codes)
        self.assertNotIn('62238-1', codes)

    def test_myeloma_cohort_gets_anc_and_egfr_but_not_breast_only_rows(self):
        self._record(90703, 'Multiple myeloma')

        call_command('enrich_demo_omop_data', '--apply', verbosity=0)

        codes = self._codes(90703)
        self.assertIn('751-8', codes)
        self.assertIn('62238-1', codes)
        self.assertIn('8302-2', codes)
        # Nottingham grade is meaningless outside a breast specimen.
        self.assertNotIn('44648-4', codes)
        self.assertNotIn('89247-1', codes)

    def test_existing_rows_are_never_duplicated(self):
        self._record(90704, 'Malignant tumor of breast')
        call_command('enrich_demo_omop_data', '--apply', verbosity=0)
        before = Measurement.objects.filter(person_id=90704).count()

        call_command('enrich_demo_omop_data', '--apply', verbosity=0)

        self.assertEqual(Measurement.objects.filter(person_id=90704).count(), before)

    def test_a_row_found_by_source_value_counts_as_covered(self):
        """Derivation matches concept_code OR source_value, so a row carrying
        only the source value must suppress the insert too."""
        self._record(90705, 'Malignant tumor of breast')
        Measurement.objects.create(
            measurement_id=90790,
            person_id=90705,
            measurement_concept=Concept.objects.get(concept_id=3000963),
            measurement_date=date(2026, 1, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=70.0,
            measurement_source_value='29463-7',
        )

        call_command('enrich_demo_omop_data', '--apply', verbosity=0)

        self.assertEqual(
            Measurement.objects.filter(
                person_id=90705, measurement_source_value='29463-7').count(), 1)

    def test_values_are_stable_across_runs(self):
        """Demos are re-run; a patient's numbers must not drift each time."""
        self._record(90706, 'Multiple myeloma')
        call_command('enrich_demo_omop_data', '--apply', verbosity=0)
        first = dict(
            Measurement.objects.filter(person_id=90706)
            .values_list('measurement_source_value', 'value_as_number'))

        Measurement.objects.filter(person_id=90706).delete()
        call_command('enrich_demo_omop_data', '--apply', verbosity=0)
        second = dict(
            Measurement.objects.filter(person_id=90706)
            .values_list('measurement_source_value', 'value_as_number'))

        self.assertEqual(first, second)

    def test_height_and_weight_yield_a_plausible_bmi(self):
        self._record(90707, 'Malignant tumor of breast')

        call_command('enrich_demo_omop_data', '--apply', verbosity=0)

        vals = dict(
            Measurement.objects.filter(person_id=90707)
            .values_list('measurement_source_value', 'value_as_number'))
        height_m = float(vals['8302-2']) / 100
        bmi = float(vals['29463-7']) / (height_m ** 2)
        self.assertGreater(bmi, 18.0)
        self.assertLess(bmi, 33.0)

    def test_touched_records_are_marked_stale_for_the_backfill(self):
        pr = self._record(90708, 'Multiple myeloma')
        PatientRecord.objects.filter(pk=pr.pk).update(derivation_version=3)

        call_command('enrich_demo_omop_data', '--apply', verbosity=0)

        pr.refresh_from_db()
        self.assertEqual(pr.derivation_version, 0)

    def test_cohort_filter_limits_scope(self):
        self._record(90709, 'Malignant tumor of breast')
        self._record(90710, 'Multiple myeloma')

        call_command('enrich_demo_omop_data', '--apply', '--cohort', 'breast', verbosity=0)

        self.assertGreater(Measurement.objects.filter(person_id=90709).count(), 0)
        self.assertEqual(Measurement.objects.filter(person_id=90710).count(), 0)

    def test_inserted_rows_actually_populate_the_read_model(self):
        """The point of the command: the fields the audit found blank must be
        non-empty after a re-derivation."""
        pr = self._record(90711, 'Malignant tumor of breast')

        call_command('enrich_demo_omop_data', '--apply', verbosity=0)
        refreshed = refresh_patient_record(pr.person)

        self.assertIsNotNone(refreshed.weight)
        self.assertIsNotNone(refreshed.height)
        self.assertIsNotNone(refreshed.bmi)
        self.assertIsNotNone(refreshed.heartrate)
        self.assertIsNotNone(refreshed.systolic_blood_pressure)
        self.assertIsNotNone(refreshed.ecog_performance_status)
        self.assertIsNotNone(refreshed.karnofsky_performance_score)
        self.assertIsNotNone(refreshed.ldh_u_l)
        self.assertIsNotNone(refreshed.hematocrit_percent)
        self.assertIsNotNone(refreshed.biopsy_grade)


class PlaceholderBirthYearTest(_OmopBase):
    """Registration seeds year_of_birth=1900; it is not a birth year."""

    PERSON_ID = 90660

    def test_placeholder_year_yields_no_age(self):
        self.person.year_of_birth = 1900
        self.person.save(update_fields=['year_of_birth'])

        pi = refresh_patient_record(self.person)

        self.assertIsNone(pi.patient_age)

    def test_stale_age_is_cleared_rather_than_left_behind(self):
        """patient_age was absent from the clear-list, so once the extractor
        stopped emitting it the old 126 simply stayed on the row."""
        self.person.year_of_birth = 1900
        self.person.save(update_fields=['year_of_birth'])
        PatientRecord.objects.create(person=self.person, patient_age=126)

        pi = refresh_patient_record(self.person)

        self.assertIsNone(pi.patient_age)

    def test_hand_entered_age_survives(self):
        """A typed age has nowhere to land in OMOP, so it must be preserved
        rather than cleared along with the stale ones."""
        self.person.year_of_birth = 1900
        self.person.save(update_fields=['year_of_birth'])
        PatientRecord.objects.create(
            person=self.person, patient_age=54, user_edited_fields=['patient_age'],
        )

        pi = refresh_patient_record(self.person)

        self.assertEqual(pi.patient_age, 54)

    def test_real_year_still_yields_an_age(self):
        self.person.year_of_birth = 1980
        self.person.save(update_fields=['year_of_birth'])

        pi = refresh_patient_record(self.person)

        self.assertEqual(pi.patient_age, date.today().year - 1980)

    def test_date_of_birth_overrides_the_placeholder(self):
        """A patient who supplied a DOB gets a real age even while Person still
        carries the placeholder year."""
        self.person.year_of_birth = 1900
        self.person.save(update_fields=['year_of_birth'])
        PatientRecord.objects.create(person=self.person, date_of_birth=date(1975, 6, 1))

        pi = refresh_patient_record(self.person)

        today = date.today()
        self.assertEqual(
            pi.patient_age,
            today.year - 1975 - ((today.month, today.day) < (6, 1)),
        )



class ConceptIdIsNotAConceptCodeTest(TestCase):
    """No concept may use its own concept_code as its concept_id (see #415).

    That single pattern caused the largest vocabulary defect in this database.
    enrich_breast_cancer_omop_data minted concepts at concept_id=int(code) on
    the reasoning that no SNOMED vocabulary was loaded and the numeric codes sat
    outside the id range. Loading Athena invalidated both halves: the genuine
    concepts existed, so the mints shadowed them, and MAX(concept_id) jumped to
    392021009, which next_pk's self-heal adopted — allocating 141 further mints
    behind it.
    """

    def test_no_seeded_concept_uses_its_code_as_its_id(self):
        from omop_core.management.commands.seed_omop_concepts import _CONCEPTS

        offenders = [
            (r['concept_id'], r['concept_code']) for r in _CONCEPTS
            if str(r['concept_id']) == str(r['concept_code'])
        ]
        self.assertEqual(
            offenders, [],
            f'concept_id must never equal concept_code: {offenders}')

    def test_enrichment_resolves_concepts_instead_of_minting(self):
        """The command must refuse to invent vocabulary content.

        Raising is the desired behaviour: a missing concept means the vocabulary
        is not loaded, which the operator needs to know. Minting silently
        produced a shadow of the genuine concept.
        """
        from omop_core.management.commands.enrich_breast_cancer_omop_data import (
            _resolve_concept,
        )
        from omop_core.models import Concept

        with self.assertRaises(CommandError) as ctx:
            _resolve_concept('SNOMED', '999999999', 'Not a real concept')
        self.assertIn('no longer mints OBSERVATION concepts', str(ctx.exception))
        self.assertFalse(
            Concept.objects.filter(concept_id=999999999).exists(),
            'no concept may be created at concept_id=int(code)')

    def test_response_code_defect_is_recorded_not_silently_swapped(self):
        """The four response codes are semantically wrong — SNOMED
        182840001-182843004 mean "drug treatment stopped", not treatment
        response — but they are deliberately left in place.

        Swapping in RECIST codes would entrench a solid-tumour vocabulary across
        five diseases, only one of which uses RECIST: lymphoma uses Lugano,
        myeloma IMWG, CLL iwCLL, and IMWG's VGPR/sCR and iwCLL's CRi/PR-L have
        no RECIST equivalent. The correct fix is per-disease value sets. This
        test pins the current state so the defect cannot be quietly "fixed" by a
        like-for-like swap.
        """
        import inspect
        from omop_core.management.commands import enrich_breast_cancer_omop_data as mod

        self.assertEqual(
            {code for _, code in mod._RESPONSE_CODES},
            {'182840001', '182841002', '182842009', '182843004'})
        source = inspect.getsource(mod)
        self.assertIn('KNOWN DEFECT', source,
                      'the mismatch must stay documented at the code table')

    def test_duplicate_concept_is_resolved_deterministically_not_by_error(self):
        """The duplicate branch had a NameError: it called logger.warning() in a
        module with no logger. Nothing exercised it, so the fix for the
        unrunnable-on-staging blocker was itself broken — staging would have
        raised NameError instead of the CommandError it replaced.

        Lowest concept_id is chosen because genuine Athena rows have lower ids
        than the code-as-id mints that shadow them (4144272 vs 266919005).
        """
        from omop_core.management.commands.enrich_breast_cancer_omop_data import (
            _resolve_concept,
        )
        from omop_core.models import Concept, ConceptClass, Domain, Vocabulary

        call_command('seed_omop_concepts', verbosity=0)
        vocab = Vocabulary.objects.get(vocabulary_id='SNOMED')
        domain = Domain.objects.get(domain_id='Observation')
        cc = ConceptClass.objects.get(concept_class_id='Clinical Finding')
        # The code-as-id shadow, alongside the genuine row seeded at 4144272.
        Concept.objects.create(
            concept_id=266919005, concept_name='Never smoked tobacco',
            domain=domain, vocabulary=vocab, concept_class=cc,
            standard_concept='S', concept_code='266919005',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31))

        resolved = _resolve_concept('SNOMED', '266919005', 'Never smoked tobacco')

        self.assertEqual(
            resolved.concept_id, 4144272,
            'must pick the genuine Athena row, not the code-as-id shadow')
