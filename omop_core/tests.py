"""
omop_core tests — TEST-01, TEST-02, TEST-03, TEST-04

TEST-01: PatientRecord model-level tests
TEST-02: refresh_patient_record service unit tests
TEST-03: Signal integration tests at omop_core level
TEST-04: FLBundleGenerator unit tests
"""

from datetime import date
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase

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
    """load_athena_vocabularies stage loaders for concept_synonym / drug_strength /
    vocabulary (#223; updated for the stage → validate → publish rework in #236).

    The per-file loaders now COPY into UNLOGGED _stage_* mirrors and read their
    concept filters from _stage_concept, so these tests create the mirrors,
    seed _stage_concept from the ORM-created concepts, and assert against the
    staged rows.  End-to-end publish behavior is covered by
    AtomicVocabPublishTest — including rerun idempotency
    (test_second_identical_load_is_a_noop), which subsumes the old
    direct-insert idempotency test.
    """

    PERSON_ID = 90280

    def _run_loader(self, method_name, filename, header, rows):
        import os
        import tempfile
        from io import StringIO
        from django.db import connection
        from omop_core.management.commands.load_athena_vocabularies import (
            Command, TABLE_SPECS,
        )
        d = tempfile.mkdtemp()
        with open(os.path.join(d, filename), 'w', encoding='utf-8', newline='') as f:
            f.write('\t'.join(header) + '\n')
            for r in rows:
                f.write('\t'.join(str(x) for x in r) + '\n')
        cmd = Command(stdout=StringIO())
        cmd._base = d
        cmd._gcs_bucket = None
        cmd._direct = False
        cmd._create_stage_tables()
        self.addCleanup(cmd._drop_stage_tables)
        # The ORM-created concepts stand in for a staged concept corpus.
        cols = ', '.join(TABLE_SPECS['concept']['cols'])
        with connection.cursor() as cur:
            cur.execute(f'INSERT INTO _stage_concept ({cols}) SELECT {cols} FROM concept')
        return getattr(cmd, method_name)(False)

    def _stage_rows(self, table, cols='*'):
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(f'SELECT {cols} FROM _stage_{table}')
            return cur.fetchall()

    def test_concept_synonym_loads_and_filters_unloaded_refs(self):
        _concept(4180186, 'English language', self.dom_meas, self.vocab, self.cc)
        _concept(950001, 'Doxorubicin', self.dom_drug, self.vocab, self.cc, code='1790')
        self._run_loader(
            '_load_concept_synonym', 'CONCEPT_SYNONYM.csv',
            ['concept_id', 'concept_synonym_name', 'language_concept_id'],
            [
                (950001, 'Adriamycin', 4180186),   # valid
                (999999, 'Ghost', 4180186),         # concept not staged -> skip
                (950001, 'BadLang', 888888),        # language not staged -> skip
            ],
        )
        names = [r[0] for r in self._stage_rows('concept_synonym', 'concept_synonym_name')]
        self.assertEqual(names, ['Adriamycin'])

    def test_drug_strength_loads_and_nulls_unloaded_unit(self):
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
                (999999, 950001, 5, 8576, '', '', '', '', '', '19700101', '20991231', ''),   # drug not staged -> skip
                (950002, 950001, 20, 777777, '', '', '', '', '', '19700101', '20991231', ''),  # unit not staged -> NULL
            ],
        )
        rows = self._stage_rows('drug_strength', 'amount_value, amount_unit_concept_id')
        self.assertEqual(len(rows), 2)
        by_amount = {float(a): u for a, u in rows}
        self.assertEqual(by_amount[10.0], 8576)
        self.assertIsNone(by_amount[20.0])

    def test_vocabulary_none_row_loaded_for_cdm_version(self):
        """The out-of-scope 'None' VOCABULARY.csv row is staged so cdm_source gets a version."""
        self._run_loader(
            '_load_vocabularies', 'VOCABULARY.csv',
            ['vocabulary_id', 'vocabulary_name', 'vocabulary_reference',
             'vocabulary_version', 'vocabulary_concept_id'],
            [
                ('None', 'OMOP CDM vocabulary', 'https://athena.ohdsi.org', 'v5.4 01-JAN-26', 756265),
                ('NotInScope', 'Some vocab', '', 'v1', 1),
            ],
        )
        rows = dict(self._stage_rows('vocabulary', 'vocabulary_id, vocabulary_version'))
        self.assertEqual(rows.get('None'), 'v5.4 01-JAN-26')
        self.assertNotIn('NotInScope', rows)

    def test_sync_cdm_source_recreates_missing_row(self):
        """sync_cdm_source_metadata re-seeds the row if it was wiped."""
        from omop_core.management.commands.load_athena_vocabularies import (
            sync_cdm_source_metadata,
        )
        from omop_core.models import CdmSource
        CdmSource.objects.filter(cdm_source_abbreviation='PRomop').delete()
        self.assertFalse(CdmSource.objects.filter(cdm_source_abbreviation='PRomop').exists())
        sync_cdm_source_metadata(lambda msg: None)
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
# Vocabulary release manifests (issue #236, ADR 0001)
# ---------------------------------------------------------------------------

class VocabReleaseServiceTest(TestCase):
    """publish_release / current_release / current_corpus_scope service tests."""

    def test_new_release_id_format(self):
        from omop_core.services.vocab_release import new_release_id
        rid = new_release_id()
        self.assertRegex(rid, r'^rel-\d{8}-[0-9a-f]{6}$')
        self.assertNotEqual(rid, new_release_id())

    def test_publish_release_creates_manifest_with_scope_and_checksums(self):
        from omop_core.models import VocabRelease
        from omop_core.services.vocab_release import CORPUS_TABLES, publish_release

        _make_vocab()
        release = publish_release(notes='test bundle')

        self.assertEqual(release.status, VocabRelease.STATUS_PUBLISHED)
        self.assertIsNotNone(release.published_at)
        self.assertEqual(release.schema_version, '1.0')
        self.assertEqual(release.notes, 'test bundle')

        # Corpus scope declares the boundary (loader scope + actually-loaded).
        scope = release.corpus_scope
        for key in ('declared_vocabularies', 'loaded_vocabularies',
                    'hk_vocabularies', 'rxnorm_classes', 'loinc_domains'):
            self.assertIn(key, scope)
        self.assertIn('OMOP_TEST', scope['loaded_vocabularies'])
        # Migration 0118 seeds the local quarantine vocabularies; they are
        # part of the published corpus.
        self.assertIn('HK-Regimen', scope['hk_vocabularies'])

        # Per-vocabulary versions captured from Vocabulary rows.
        self.assertIn('OMOP_TEST', release.vocabulary_versions)

        # Checksums + row counts cover every corpus table.
        self.assertEqual(set(release.table_checksums.keys()), set(CORPUS_TABLES))
        self.assertEqual(set(release.row_counts.keys()), set(CORPUS_TABLES))
        self.assertEqual(release.row_counts['concept'], Concept.objects.count())
        self.assertEqual(len(release.table_checksums['concept']), 64)  # sha256 hex

    def test_current_release_returns_newest_published(self):
        from omop_core.models import VocabRelease
        from omop_core.services.vocab_release import current_release, publish_release

        self.assertIsNone(current_release())

        first = publish_release()
        second = publish_release()
        # A staging build never becomes current.
        VocabRelease.objects.create(
            release_id='rel-99990101-staging', status=VocabRelease.STATUS_STAGING,
        )

        current = current_release()
        self.assertEqual(current.release_id, second.release_id)
        self.assertNotEqual(current.release_id, first.release_id)

    def test_publish_command_runs(self):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command('publish_vocab_release', notes='cmd test', stdout=out)
        text = out.getvalue()
        self.assertIn('Published rel-', text)
        self.assertIn('concept', text)


# ---------------------------------------------------------------------------
# Issue #236 PR 3 — load_athena_vocabularies stage → validate → publish
# ---------------------------------------------------------------------------

_TSV = {
    'RELATIONSHIP.csv': (
        'relationship_id\trelationship_name\tis_hierarchical\tdefines_ancestry\treverse_relationship_id\trelationship_concept_id',
        ['Maps to\tMaps to\t0\t0\tMapped from\t0'],
    ),
    'VOCABULARY.csv': (
        'vocabulary_id\tvocabulary_name\tvocabulary_reference\tvocabulary_version\tvocabulary_concept_id',
        [
            'None\tNone\t\tv20260101\t0',
            'HemOnc\tHemOnc\t\t\t0',
            'RxNorm\tRxNorm\t\t\t0',
            'SNOMED\tSNOMED\t\t\t0',
        ],
    ),
    'DOMAIN.csv': (
        'domain_id\tdomain_name\tdomain_concept_id',
        ['Drug\tDrug\t0', 'Metadata\tMetadata\t0'],
    ),
    'CONCEPT_CLASS.csv': (
        'concept_class_id\tconcept_class_name\tconcept_class_concept_id',
        ['Regimen\tRegimen\t0', 'Ingredient\tIngredient\t0',
         'Clinical Drug\tClinical Drug\t0', 'Undefined\tUndefined\t0'],
    ),
    'CONCEPT.csv': (
        'concept_id\tconcept_name\tdomain_id\tvocabulary_id\tconcept_class_id\tstandard_concept\tconcept_code\tvalid_start_date\tvalid_end_date\tinvalid_reason',
        [
            '101\tBortezomib regimen\tDrug\tHemOnc\tRegimen\tS\tHO101\t20200101\t20991231\t',
            '102\tbortezomib\tDrug\tRxNorm\tIngredient\tS\tRX102\t20200101\t20991231\t',
            '103\tbortezomib 3.5 MG\tDrug\tRxNorm\tClinical Drug\tS\tRX103\t20200101\t20991231\t',
            '4180186\tEnglish\tMetadata\tSNOMED\tUndefined\tS\t4180186\t20200101\t20991231\t',
        ],
    ),
    'CONCEPT_RELATIONSHIP.csv': (
        'concept_id_1\tconcept_id_2\trelationship_id\tvalid_start_date\tvalid_end_date\tinvalid_reason',
        ['101\t102\tMaps to\t20200101\t20991231\t'],
    ),
    'CONCEPT_ANCESTOR.csv': (
        'ancestor_concept_id\tdescendant_concept_id\tmin_levels_of_separation\tmax_levels_of_separation',
        ['101\t101\t0\t0'],
    ),
    'CONCEPT_SYNONYM.csv': (
        'concept_id\tconcept_synonym_name\tlanguage_concept_id',
        ['101\tBortezomib-based regimen\t4180186'],
    ),
    'DRUG_STRENGTH.csv': (
        'drug_concept_id\tingredient_concept_id\tamount_value\tamount_unit_concept_id\tnumerator_value\tnumerator_unit_concept_id\tdenominator_value\tdenominator_unit_concept_id\tbox_size\tvalid_start_date\tvalid_end_date\tinvalid_reason',
        ['103\t102\t3.5\t\t\t\t\t\t\t20200101\t20991231\t'],
    ),
    'SOURCE_TO_CONCEPT_MAP.csv': (
        'source_code\tsource_concept_id\tsource_vocabulary_id\tsource_code_description\ttarget_concept_id\ttarget_vocabulary_id\tvalid_start_date\tvalid_end_date\tinvalid_reason',
        ['BOR-R\t0\tHemOnc\tBortezomib regimen map\t101\tHemOnc\t20200101\t20991231\t'],
    ),
}


class AtomicVocabPublishTest(TestCase):
    """load_athena_vocabularies stage → validate → publish (issue #236 PR 3)."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        # FK targets for locally-created (HK-*) rows.
        Domain.objects.get_or_create(
            domain_id='Drug', defaults={'domain_name': 'Drug', 'domain_concept_id': 0})
        ConceptClass.objects.get_or_create(
            concept_class_id='Regimen',
            defaults={'concept_class_name': 'Regimen', 'concept_class_concept_id': 0})

    # -- fixture helpers -----------------------------------------------------

    def _write(self, name, header, rows):
        from pathlib import Path
        Path(self.dir, name).write_text(
            header + '\n' + '\n'.join(rows) + '\n', encoding='utf-8')

    def _write_corpus(self, **overrides):
        """Write the full 10-file corpus; per-file row lists may be overridden."""
        for name, (header, rows) in _TSV.items():
            self._write(name, header, overrides.get(name, rows))

    def _run(self, *args):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command('load_athena_vocabularies', '--path', self.dir, *args,
                     stdout=out, stderr=out)
        return out.getvalue()

    def _table_count(self, table):
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {table}')
            return cur.fetchone()[0]

    def _changes(self, release, **filters):
        from omop_core.models import ReleaseTableChange
        return ReleaseTableChange.objects.filter(release=release, **filters)

    def _published_release(self):
        from omop_core.models import VocabRelease
        return VocabRelease.objects.get(status=VocabRelease.STATUS_PUBLISHED)

    # -- tests ---------------------------------------------------------------

    def test_fresh_load_publishes_release_with_insert_change_rows(self):
        from omop_core.models import VocabRelease
        self._write_corpus()
        out = self._run()

        self.assertIn('Published rel-', out)
        # Live corpus populated (4 concepts from the fixture; concept 0 pre-exists).
        self.assertEqual(Concept.objects.filter(concept_id__gt=0).count(), 4)
        self.assertEqual(self._table_count('source_to_concept_map'), 1)

        release = self._published_release()
        self.assertEqual(release.status, VocabRelease.STATUS_PUBLISHED)
        # Every staged corpus row arrives as an insert change row; the publish
        # may also update migration-seeded metadata rows (vocabulary, domain,
        # concept_class, relationship) to match the files — but a fresh load
        # never updates or tombstones corpus content.
        self.assertEqual(
            set(self._changes(release, table_name='concept')
                .values_list('operation', flat=True)),
            {'insert'},
        )
        self.assertFalse(self._changes(release, operation='tombstone').exists())
        self.assertLessEqual(
            set(self._changes(release, operation='update')
                .values_list('table_name', flat=True)),
            {'vocabulary', 'domain', 'concept_class', 'relationship'},
        )
        self.assertEqual(self._changes(release, table_name='concept').count(), 4)
        self.assertEqual(self._changes(release, table_name='source_to_concept_map').count(), 1)
        # seq is dense starting at 1.
        seqs = sorted(self._changes(release).values_list('seq', flat=True))
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        # Manifest describes the post-publish corpus.
        self.assertEqual(release.row_counts['concept'], Concept.objects.count())
        self.assertEqual(release.row_counts['source_to_concept_map'], 1)
        # Stage mirrors are dropped after publish.
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE '_stage_%'")
            self.assertEqual(cur.fetchall(), [])

    def test_stage_only_leaves_live_untouched(self):
        self._write_corpus()
        out = self._run('--stage-only')
        self.assertIn('--stage-only', out)
        self.assertEqual(Concept.objects.filter(concept_id__gt=0).count(), 0)
        self.assertEqual(self._table_count('_stage_concept'), 4)
        from omop_core.models import VocabRelease
        self.assertEqual(VocabRelease.objects.count(), 0)

    def test_update_in_place_writes_update_change_row(self):
        self._write_corpus()
        self._run()
        # Rename concept 101 in the file and reload.
        self._write_corpus(**{
            'CONCEPT.csv': [r.replace('Bortezomib regimen', 'Bortezomib RENAMED')
                            if r.startswith('101\t') else r
                            for r in _TSV['CONCEPT.csv'][1]],
        })
        self._run()

        self.assertEqual(Concept.objects.get(concept_id=101).concept_name,
                         'Bortezomib RENAMED')
        from omop_core.models import VocabRelease
        second = VocabRelease.objects.filter(
            status=VocabRelease.STATUS_PUBLISHED).latest('published_at')
        upd = self._changes(second, table_name='concept', operation='update')
        self.assertEqual(upd.count(), 1)
        payload = upd.get().payload
        self.assertEqual(payload['old']['concept_name'], 'Bortezomib regimen')
        self.assertEqual(payload['new']['concept_name'], 'Bortezomib RENAMED')

    def test_retired_row_produces_tombstone_with_validity_window(self):
        self._write_corpus()
        self._run()
        # Athena retires concept 103 (and with it the drug_strength row).
        self._write_corpus(**{
            'CONCEPT.csv': [r for r in _TSV['CONCEPT.csv'][1]
                            if not r.startswith('103\t')],
            'DRUG_STRENGTH.csv': [],
        })
        self._run('--max-drift-pct', '100')

        self.assertFalse(Concept.objects.filter(concept_id=103).exists())
        self.assertEqual(self._table_count('drug_strength'), 0)
        from omop_core.models import VocabRelease
        second = VocabRelease.objects.filter(
            status=VocabRelease.STATUS_PUBLISHED).latest('published_at')
        tom = self._changes(second, operation='tombstone')
        self.assertEqual(
            set(tom.values_list('table_name', flat=True)),
            {'concept', 'drug_strength'},
        )
        payload = tom.get(table_name='concept').payload
        self.assertEqual(payload['concept_id'], 103)
        self.assertIn('valid_start_date', payload)
        self.assertIn('valid_end_date', payload)
        self.assertIn('invalid_reason', payload)

    def test_hk_rows_survive_publish(self):
        hk = Concept.objects.create(
            concept_id=900001, concept_name='Local quarantine regimen',
            domain_id='Drug', vocabulary_id='HK-Regimen',
            concept_class_id='Regimen', concept_code='hkr:local-x',
            valid_start_date='2020-01-01', valid_end_date='2099-12-31',
        )
        self._write_corpus()
        self._run()

        hk.refresh_from_db()
        self.assertEqual(hk.concept_name, 'Local quarantine regimen')
        self.assertTrue(Vocabulary.objects.filter(vocabulary_id='HK-Regimen').exists())
        from omop_core.models import ReleaseTableChange
        self.assertFalse(
            ReleaseTableChange.objects.filter(row_key__contains='900001').exists())

    def test_stage_id_colliding_with_live_hk_concept_aborts(self):
        Concept.objects.create(
            concept_id=900001, concept_name='Local quarantine regimen',
            domain_id='Drug', vocabulary_id='HK-Regimen',
            concept_class_id='Regimen', concept_code='hkr:local-x',
            valid_start_date='2020-01-01', valid_end_date='2099-12-31',
        )
        from django.core.management.base import CommandError
        self._write_corpus(**{
            'CONCEPT.csv': _TSV['CONCEPT.csv'][1] + [
                '900001\tAthena newcomer\tDrug\tHemOnc\tRegimen\tS\tHO900001\t20200101\t20991231\t'],
        })
        with self.assertRaises(CommandError) as ctx:
            self._run()
        self.assertIn('collides with a live locally-minted', str(ctx.exception))
        # The HK row is untouched and nothing was published.
        self.assertEqual(Concept.objects.get(concept_id=900001).concept_name,
                         'Local quarantine regimen')
        from omop_core.models import VocabRelease
        self.assertEqual(VocabRelease.objects.count(), 0)

    def test_fk_integrity_gate_aborts(self):
        from django.core.management.base import CommandError
        from omop_core.management.commands.load_athena_vocabularies import Command as LoadCmd
        # Simulate a loader/filter bug: let a relationship referencing an
        # unstaged concept_id through to the stage mirror.
        self._write_corpus(**{
            'CONCEPT_RELATIONSHIP.csv': ['101\t999\tMaps to\t20200101\t20991231\t'],
        })
        staged = {101, 102, 103, 4180186, 999}
        with patch.object(LoadCmd, '_stage_ids', lambda self, where='': staged):
            with self.assertRaises(CommandError) as ctx:
                self._run()
        self.assertIn('concept_relationship.concept_id_2', str(ctx.exception))

    def test_drift_gate_aborts_on_shrinking_corpus(self):
        from django.core.management.base import CommandError
        self._write_corpus()
        self._run()
        # Next export carries only 1 of the 4 in-scope concepts (75% drift).
        self._write_corpus(**{'CONCEPT.csv': [_TSV['CONCEPT.csv'][1][0]]})
        with self.assertRaises(CommandError) as ctx:
            self._run()
        self.assertIn('drifts', str(ctx.exception))
        # Live tables unchanged.
        self.assertEqual(Concept.objects.filter(concept_id__gt=0).count(), 4)

    def test_namespace_gate_rejects_locally_minted_codes(self):
        from django.core.management.base import CommandError
        self._write_corpus(**{
            'CONCEPT.csv': _TSV['CONCEPT.csv'][1] + [
                '104\tFake regimen\tDrug\tHemOnc\tRegimen\tS\tFHIR-104\t20200101\t20991231\t'],
        })
        with self.assertRaises(CommandError) as ctx:
            self._run()
        self.assertIn('locally-minted codes', str(ctx.exception))
        self.assertFalse(Concept.objects.filter(concept_id=104).exists())

    def test_second_identical_load_is_a_noop(self):
        from omop_core.models import VocabRelease
        self._write_corpus()
        self._run()
        self._run()  # same files again
        self.assertEqual(VocabRelease.objects.filter(
            status=VocabRelease.STATUS_PUBLISHED).count(), 2)
        second = VocabRelease.objects.filter(
            status=VocabRelease.STATUS_PUBLISHED).latest('published_at')
        self.assertEqual(self._changes(second).count(), 0)
        self.assertEqual(self._table_count('source_to_concept_map'), 1)
        self.assertEqual(Concept.objects.filter(concept_id__gt=0).count(), 4)

    def test_publish_rolls_back_atomically_on_error(self):
        from omop_core.models import ReleaseTableChange, VocabRelease
        self._write_corpus()
        with patch('omop_core.services.vocab_release.compute_table_checksums',
                   side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self._run()
        # Nothing applied, nothing published, no change rows.
        self.assertEqual(Concept.objects.filter(concept_id__gt=0).count(), 0)
        self.assertEqual(VocabRelease.objects.count(), 0)
        self.assertEqual(ReleaseTableChange.objects.count(), 0)


class ResetVocabTablesTest(TransactionTestCase):
    """reset_vocab_tables escape hatch (issue #236 PR 3).

    TransactionTestCase (not TestCase): the command TRUNCATEs, and Postgres
    refuses to TRUNCATE a table with pending deferred-FK trigger events —
    which any ORM write inside TestCase's never-committed wrapping
    transaction leaves queued.  Autocommit mode matches how the command is
    actually run in production.
    """

    serialized_rollback = True

    def test_refuses_without_confirm(self):
        from django.core.management.base import CommandError
        from io import StringIO
        from django.core.management import call_command
        with self.assertRaises(CommandError):
            call_command('reset_vocab_tables', stdout=StringIO())

    def test_truncates_and_reseeds(self):
        from io import StringIO
        from django.core.management import call_command
        Domain.objects.get_or_create(
            domain_id='Drug', defaults={'domain_name': 'Drug', 'domain_concept_id': 0})
        ConceptClass.objects.get_or_create(
            concept_class_id='Regimen',
            defaults={'concept_class_name': 'Regimen', 'concept_class_concept_id': 0})
        Vocabulary.objects.get_or_create(
            vocabulary_id='HemOnc', defaults={'vocabulary_name': 'HemOnc',
                                              'vocabulary_concept_id': 0})
        Concept.objects.create(
            concept_id=101, concept_name='x', domain_id='Drug',
            vocabulary_id='HemOnc', concept_class_id='Regimen',
            concept_code='HO101', valid_start_date='2020-01-01',
            valid_end_date='2099-12-31',
        )
        out = StringIO()
        call_command('reset_vocab_tables', '--confirm', stdout=out)
        self.assertEqual(Concept.objects.filter(concept_id__gt=0).count(), 0)
        self.assertFalse(Vocabulary.objects.filter(vocabulary_id='HemOnc').exists())
        # concept 0 and cdm_source are re-seeded by the command.
        self.assertTrue(Concept.objects.filter(concept_id=0).exists())
        self.assertIn('CASCADE', out.getvalue())
