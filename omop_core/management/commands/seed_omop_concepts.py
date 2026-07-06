"""
Seed the minimal set of OMOP CDM concepts required for the mCODE FHIR import
pipeline to write Measurements, ConditionOccurrences, and DrugExposures and for
refresh_patient_info to derive PatientInfo fields from them.

This is a dev/test shortcut.  Production environments should load concepts via
the full load_athena_vocabularies command instead.

Usage:
    DATABASE_URL=... python manage.py seed_omop_concepts
"""
from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from omop_core.models import Concept, ConceptClass, Domain, Vocabulary


# ---------------------------------------------------------------------------
# Minimal vocabularies, domains, and concept-classes required by the rows below
# ---------------------------------------------------------------------------

_VOCABULARIES = [
    dict(vocabulary_id='Type Concept',
         vocabulary_name='OMOP Type Concept',
         vocabulary_reference='OMOP generated',
         vocabulary_version='v5',
         vocabulary_concept_id=0),
    dict(vocabulary_id='LOINC',
         vocabulary_name='Logical Observation Identifiers Names and Codes',
         vocabulary_reference='https://loinc.org',
         vocabulary_version='2.76',
         vocabulary_concept_id=0),
    dict(vocabulary_id='SNOMED',
         vocabulary_name='SNOMED Clinical Terms',
         vocabulary_reference='https://www.snomed.org',
         vocabulary_version='2024-09-01',
         vocabulary_concept_id=0),
    dict(vocabulary_id='CDM',
         vocabulary_name='OMOP CDM',
         vocabulary_reference='OMOP generated',
         vocabulary_version='CDM v5',
         vocabulary_concept_id=0),
    dict(vocabulary_id='Episode',
         vocabulary_name='OMOP Episode',
         vocabulary_reference='OMOP generated',
         vocabulary_version='v5',
         vocabulary_concept_id=0),
    dict(vocabulary_id='Gender',
         vocabulary_name='OMOP Gender',
         vocabulary_reference='OMOP generated',
         vocabulary_version='v5',
         vocabulary_concept_id=0),
]

_DOMAINS = [
    dict(domain_id='Type Concept', domain_name='Type Concept', domain_concept_id=0),
    dict(domain_id='Measurement',  domain_name='Measurement',   domain_concept_id=0),
    dict(domain_id='Condition',    domain_name='Condition',      domain_concept_id=0),
    dict(domain_id='Meas Value',   domain_name='Meas Value',     domain_concept_id=0),
    dict(domain_id='Episode',      domain_name='Episode',        domain_concept_id=0),
    dict(domain_id='Observation',  domain_name='Observation',    domain_concept_id=0),
    dict(domain_id='Metadata',     domain_name='Metadata',       domain_concept_id=0),
    dict(domain_id='Gender',       domain_name='Gender',          domain_concept_id=0),
]

_CONCEPT_CLASSES = [
    dict(concept_class_id='Type Concept',       concept_class_name='Type Concept',         concept_class_concept_id=0),
    dict(concept_class_id='Lab Test',            concept_class_name='Lab Test',             concept_class_concept_id=0),
    dict(concept_class_id='Clinical Observation', concept_class_name='Clinical Observation', concept_class_concept_id=0),
    dict(concept_class_id='Disorder',            concept_class_name='Disorder',             concept_class_concept_id=0),
    dict(concept_class_id='Qualifier Value',     concept_class_name='Qualifier Value',      concept_class_concept_id=0),
    dict(concept_class_id='Field',               concept_class_name='Field',                concept_class_concept_id=0),
    dict(concept_class_id='Treatment',           concept_class_name='Treatment',            concept_class_concept_id=0),
    dict(concept_class_id='Gender',              concept_class_name='Gender',               concept_class_concept_id=0),
]


# ---------------------------------------------------------------------------
# Core concepts
# ---------------------------------------------------------------------------

_START = date(1970, 1, 1)
_END   = date(2099, 12, 31)


def _c(concept_id, concept_name, domain_id, vocabulary_id, concept_class_id,
        standard_concept, concept_code,
        valid_start=None, valid_end=None):
    return dict(
        concept_id=concept_id,
        concept_name=concept_name,
        domain_id=domain_id,
        vocabulary_id=vocabulary_id,
        concept_class_id=concept_class_id,
        standard_concept=standard_concept,
        concept_code=concept_code,
        valid_start_date=valid_start or _START,
        valid_end_date=valid_end or _END,
        invalid_reason=None,
    )


_CONCEPTS = [
    # ------------------------------------------------------------------
    # Gender concepts — needed for Person.gender_concept FK.
    # ------------------------------------------------------------------
    _c(8507, 'MALE',    'Gender', 'Gender', 'Gender', 'S', 'M'),
    _c(8532, 'FEMALE',  'Gender', 'Gender', 'Gender', 'S', 'F'),
    _c(8551, 'UNKNOWN', 'Gender', 'Gender', 'Gender', 'S', 'U'),

    # ------------------------------------------------------------------
    # Type concepts — needed for measurement_type_concept_id and
    # condition_type_concept_id FK fields in every OMOP event table.
    # ------------------------------------------------------------------
    _c(32817, 'EHR',               'Type Concept', 'Type Concept', 'Type Concept', 'S', 'OMOP4976890'),
    _c(32856, 'Lab',               'Type Concept', 'Type Concept', 'Type Concept', 'S', 'OMOP4976929'),
    _c(32869, 'Pharmacy claim',    'Type Concept', 'Type Concept', 'Type Concept', 'S', 'OMOP4976942'),
    _c(32531, 'Treatment Regimen', 'Episode',       'Episode',       'Treatment',    'S', 'OMOP4822256'),

    # CDM metadata concept used in DrugExposure FK lookups
    _c(1147094, 'drug_exposure.drug_exposure_id', 'Metadata', 'CDM', 'Field', 'S', 'CDM150'),

    # ------------------------------------------------------------------
    # Generic lab fallback — used when no specific LOINC concept is found.
    # concept_id 3000963 is pre-hoisted in upload_fhir as _concept_generic_lab.
    # vocabulary_id='None' and concept_code='0' are intentional non-LOINC
    # placeholders so this concept is never matched by LOINC code lookups
    # and cannot pollute specific lab fields (e.g. hemoglobin_g_dl).
    # ------------------------------------------------------------------
    _c(3000963, 'Generic Lab Measurement', 'Measurement', 'None', 'Lab Test', 'S', '0'),

    # ------------------------------------------------------------------
    # Breast cancer condition
    # ------------------------------------------------------------------
    _c(4112853, 'Malignant tumor of breast', 'Condition', 'SNOMED', 'Disorder', 'S', '254837009'),

    # ------------------------------------------------------------------
    # Tumor marker biomarkers — critical for refresh_patient_info to set
    # estrogen_receptor_status, progesterone_receptor_status, her2_status.
    # ------------------------------------------------------------------
    _c(3004390, 'Estrogen receptor [Interpretation] in Tissue',    'Measurement', 'LOINC', 'Lab Test', 'S', '16112-5'),
    _c(3003289, 'Progesterone receptor [Interpretation] in Tissue', 'Measurement', 'LOINC', 'Lab Test', 'S', '16113-3'),
    _c(3048223, 'HER2 [Interpretation] in Tissue',                 'Measurement', 'LOINC', 'Lab Test', 'S', '48676-1'),

    # Categorical Positive / Negative values stored in value_as_concept_id
    _c(9191, 'Positive', 'Meas Value', 'SNOMED', 'Qualifier Value', 'S', '10828004'),
    _c(9189, 'Negative', 'Meas Value', 'SNOMED', 'Qualifier Value', 'S', '260385009'),

    # ------------------------------------------------------------------
    # TNM staging and cancer disease status
    # ------------------------------------------------------------------
    _c(3022698,  'Stage group.clinical Cancer',    'Measurement',  'LOINC', 'Clinical Observation', 'S', '21908-9'),
    _c(36305408, 'Response to cancer treatment',   'Observation',  'LOINC', 'Clinical Observation', 'S', '88040-1'),

    # ------------------------------------------------------------------
    # CBC — enables CBC fields in PatientInfo via _LOINC_LAB_FIELDS
    # ------------------------------------------------------------------
    _c(3023314, 'Hematocrit [Volume Fraction] of Blood by Automated count', 'Measurement', 'LOINC', 'Lab Test', 'S', '4544-3'),
    _c(3000905, 'Leukocytes [#/volume] in Blood by Automated count',        'Measurement', 'LOINC', 'Lab Test', 'S', '6690-2'),
    _c(3020416, 'Erythrocytes [#/volume] in Blood by Automated count',      'Measurement', 'LOINC', 'Lab Test', 'S', '789-8'),
    _c(3024929, 'Platelets [#/volume] in Blood by Automated count',         'Measurement', 'LOINC', 'Lab Test', 'S', '777-3'),
    _c(3013650, 'Neutrophils [#/volume] in Blood by Automated count',       'Measurement', 'LOINC', 'Lab Test', 'S', '751-8'),
    _c(3004327, 'Lymphocytes [#/volume] in Blood by Automated count',       'Measurement', 'LOINC', 'Lab Test', 'S', '731-0'),
    _c(3033575, 'Monocytes [#/volume] in Blood by Automated count',         'Measurement', 'LOINC', 'Lab Test', 'S', '742-7'),

    # ------------------------------------------------------------------
    # CMP basics
    # ------------------------------------------------------------------
    _c(3016723, 'Creatinine [Mass/volume] in Serum or Plasma', 'Measurement', 'LOINC', 'Lab Test', 'S', '2160-0'),
    _c(3051825, 'Creatinine [Mass/volume] in Blood',           'Measurement', 'LOINC', 'Lab Test', None, '38483-4'),  # mCODE variant
    _c(3006906, 'Calcium [Mass/volume] in Serum or Plasma',    'Measurement', 'LOINC', 'Lab Test', 'S', '17861-6'),
    _c(3032503, 'Calcium [Mass/volume] in Blood',              'Measurement', 'LOINC', 'Lab Test', None, '49765-1'),
    _c(3016436, 'Lactate dehydrogenase [Enzymatic activity/volume] in Serum or Plasma',
                                                               'Measurement', 'LOINC', 'Lab Test', 'S', '2532-0'),
    _c(3000285, 'Sodium [Moles/volume] in Blood',              'Measurement', 'LOINC', 'Lab Test', None, '2947-0'),
    _c(3005456, 'Potassium [Moles/volume] in Blood',           'Measurement', 'LOINC', 'Lab Test', None, '6298-4'),
    _c(3004295, 'Urea nitrogen [Mass/volume] in Blood',        'Measurement', 'LOINC', 'Lab Test', None, '6299-2'),
    _c(3030354, 'Glomerular filtration rate in Serum or Plasma by CKD-EPI',
                                                               'Measurement', 'LOINC', 'Lab Test', None, '33914-3'),
    _c(3000483, 'Glucose [Mass/volume] in Blood',              'Measurement', 'LOINC', 'Lab Test', None, '2339-0'),
    # ------------------------------------------------------------------
    # LFT — present in mCODE synthetic data
    # ------------------------------------------------------------------
    _c(3006923, 'Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma',
                                                               'Measurement', 'LOINC', 'Lab Test', 'S', '1742-6'),
    _c(3013721, 'Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma',
                                                               'Measurement', 'LOINC', 'Lab Test', 'S', '1920-8'),
    _c(3024561, 'Albumin [Mass/volume] in Serum or Plasma',    'Measurement', 'LOINC', 'Lab Test', 'S', '1751-7'),
    _c(3024128, 'Bilirubin.total [Mass/volume] in Serum or Plasma',
                                                               'Measurement', 'LOINC', 'Lab Test', 'S', '1975-2'),
    _c(3020630, 'Protein [Mass/volume] in Serum or Plasma',    'Measurement', 'LOINC', 'Lab Test', 'S', '2885-2'),
    _c(3035995, 'Alkaline phosphatase [Enzymatic activity/volume] in Serum or Plasma',
                                                               'Measurement', 'LOINC', 'Lab Test', 'S', '6768-6'),
    _c(3004410, 'Hemoglobin A1c/Hemoglobin.total in Blood',    'Measurement', 'LOINC', 'Lab Test', 'S', '4548-4'),

    # ------------------------------------------------------------------
    # Vitals — enables weight, height, BP, HR in PatientInfo
    # ------------------------------------------------------------------
    _c(3025315, 'Body weight',             'Measurement', 'LOINC', 'Clinical Observation', 'S', '29463-7'),
    _c(3036277, 'Body height',             'Measurement', 'LOINC', 'Clinical Observation', 'S', '8302-2'),
    _c(3004249, 'Systolic blood pressure', 'Measurement', 'LOINC', 'Clinical Observation', 'S', '8480-6'),
    _c(3012888, 'Diastolic blood pressure','Measurement', 'LOINC', 'Clinical Observation', 'S', '8462-4'),
    _c(3027018, 'Heart rate',              'Measurement', 'LOINC', 'Clinical Observation', 'S', '8867-4'),
    _c(3038553, 'Body mass index (BMI) [Ratio]', 'Measurement', 'LOINC', 'Clinical Observation', 'S', '39156-5'),
    _c(3020891, 'Body temperature',        'Measurement', 'LOINC', 'Clinical Observation', 'S', '8310-5'),
    _c(40762499, 'Oxygen saturation in Arterial blood by Pulse oximetry',
                                           'Measurement', 'LOINC', 'Clinical Observation', 'S', '59408-5'),
    _c(40758413, 'Blood pressure systolic and diastolic', 'Measurement', 'LOINC', 'Clinical Observation', 'S', '55284-4'),

    # ------------------------------------------------------------------
    # Clinical assessments
    # ------------------------------------------------------------------
    _c(36305384, 'ECOG Performance Status score', 'Measurement',  'LOINC', 'Clinical Observation', 'S', '89247-1'),
    _c(43054909, 'Tobacco smoking status',         'Observation',  'LOINC', 'Clinical Observation', 'S', '72166-2'),
]


class Command(BaseCommand):
    help = (
        'Seed the minimal OMOP concept rows required for mCODE FHIR import. '
        'Use load_athena_vocabularies for a full production vocabulary load.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print what would be seeded without writing to the DB.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        if dry_run:
            self.stdout.write(self.style.WARNING('Dry-run mode — no changes will be written.\n'))

        with transaction.atomic():
            # Vocabularies
            v_created = 0
            for v in _VOCABULARIES:
                if dry_run:
                    if not Vocabulary.objects.filter(vocabulary_id=v['vocabulary_id']).exists():
                        self.stdout.write(f"  [would create vocabulary] {v['vocabulary_id']}")
                        v_created += 1
                else:
                    _, created = Vocabulary.objects.get_or_create(
                        vocabulary_id=v['vocabulary_id'], defaults=v)
                    if created:
                        v_created += 1

            # Domains
            d_created = 0
            for d in _DOMAINS:
                if dry_run:
                    if not Domain.objects.filter(domain_id=d['domain_id']).exists():
                        self.stdout.write(f"  [would create domain] {d['domain_id']}")
                        d_created += 1
                else:
                    _, created = Domain.objects.get_or_create(
                        domain_id=d['domain_id'], defaults=d)
                    if created:
                        d_created += 1

            # Concept classes
            cc_created = 0
            for cc in _CONCEPT_CLASSES:
                if dry_run:
                    if not ConceptClass.objects.filter(concept_class_id=cc['concept_class_id']).exists():
                        self.stdout.write(f"  [would create concept_class] {cc['concept_class_id']}")
                        cc_created += 1
                else:
                    _, created = ConceptClass.objects.get_or_create(
                        concept_class_id=cc['concept_class_id'], defaults=cc)
                    if created:
                        cc_created += 1

            # Concepts
            c_created = c_existing = 0
            for row in _CONCEPTS:
                if dry_run:
                    exists = Concept.objects.filter(concept_id=row['concept_id']).exists()
                    status = 'exists' if exists else 'would create'
                    self.stdout.write(
                        f"  [{status}] {row['concept_id']:>8}  {row['concept_code']:<12}  {row['concept_name']}")
                    if not exists:
                        c_created += 1
                    else:
                        c_existing += 1
                else:
                    _, created = Concept.objects.get_or_create(
                        concept_id=row['concept_id'], defaults=row)
                    if created:
                        c_created += 1
                    else:
                        c_existing += 1

            if dry_run:
                transaction.set_rollback(True)

        summary = (
            f'Vocabularies: {v_created} new  |  '
            f'Domains: {d_created} new  |  '
            f'ConceptClasses: {cc_created} new  |  '
            f'Concepts: {c_created} new, {c_existing} already present'
        )
        if dry_run:
            self.stdout.write(self.style.WARNING(f'\nDry-run summary: {summary}'))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
