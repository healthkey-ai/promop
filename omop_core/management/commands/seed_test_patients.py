"""
Management command: seed_test_patients

Creates a small set of fake PatientInfo records for local end-to-end testing.
Covers all four diseases supported by exact (MM, FL, BC, CLL).

Each patient is designed to match at least one of the trials created by
exact's seed_test_trials command.

Usage:
    python manage.py seed_test_patients
    python manage.py seed_test_patients --clear   # wipe first
"""
from django.core.management.base import BaseCommand

from omop_core.models import PatientInfo, Person


# person_id range 9001-9999 reserved for test data so it won't
# collide with real patients loaded from cancerbot (which start at 1).
TEST_PATIENTS = [
    # ── Multiple Myeloma — relapsed/refractory, should match TEST-MM-001
    dict(
        person_id=9001,
        person_defaults=dict(year_of_birth=1958, gender_source_value='M'),
        pi=dict(
            disease='multiple myeloma',
            patient_age=66,
            gender='M',
            country='US',
            prior_therapy='More than two lines of therapy',
            first_line_therapy='VRd',
            first_line_outcome='CR',
            second_line_therapy='Daratumumab',
            second_line_outcome='PD',
            later_therapy='Pomalidomide',
            later_outcome='PD',
            progression='active',
            stage='III',
            ecog_performance_status=1,
            karnofsky_performance_score=80,
            hemoglobin_level='9.5',
            platelet_count=110000,
            creatinine_clearance_rate=55,
            no_hiv_status=True,
            no_hepatitis_b_status=True,
            no_hepatitis_c_status=True,
            no_other_active_malignancies=True,
            monoclonal_protein_serum='1.8',
            kappa_flc=180,
            lambda_flc=5,
        ),
    ),

    # ── Multiple Myeloma — newly diagnosed, should match TEST-MM-002
    dict(
        person_id=9002,
        person_defaults=dict(year_of_birth=1970, gender_source_value='F'),
        pi=dict(
            disease='multiple myeloma',
            patient_age=54,
            gender='F',
            country='DE',
            prior_therapy='None',
            progression='active',
            stage='II',
            ecog_performance_status=0,
            karnofsky_performance_score=100,
            hemoglobin_level='10.2',
            platelet_count=145000,
            creatinine_clearance_rate=72,
            no_hiv_status=True,
            no_hepatitis_b_status=True,
            no_hepatitis_c_status=True,
            no_other_active_malignancies=True,
            monoclonal_protein_serum='2.3',
        ),
    ),

    # ── Multiple Myeloma — ht-phr local-stack demo login (demo@healthtree.org,
    #    "Diana Demo"). person_id 9008 is the patient the host FindTrials page
    #    resolves for the demo account; seeded here with the canonical disease
    #    title so the trial-match demo is reproducible without manual DB edits.
    dict(
        person_id=9008,
        person_defaults=dict(year_of_birth=1968, gender_source_value='F'),
        pi=dict(
            disease='multiple myeloma',
            patient_age=56,
            gender='F',
            country='US',
            prior_therapy='More than two lines of therapy',
            first_line_therapy='VRd',
            first_line_outcome='CR',
            second_line_therapy='Daratumumab',
            second_line_outcome='PD',
            later_therapy='Pomalidomide',
            later_outcome='PD',
            progression='active',
            stage='III',
            ecog_performance_status=1,
            karnofsky_performance_score=80,
            hemoglobin_level='9.8',
            platelet_count=120000,
            creatinine_clearance_rate=58,
            no_hiv_status=True,
            no_hepatitis_b_status=True,
            no_hepatitis_c_status=True,
            no_other_active_malignancies=True,
            monoclonal_protein_serum='2.0',
            kappa_flc=165,
            lambda_flc=6,
        ),
    ),

    # ── Follicular Lymphoma — treatment-naive, should match TEST-FL-001
    dict(
        person_id=9003,
        person_defaults=dict(year_of_birth=1965, gender_source_value='F'),
        pi=dict(
            disease='follicular lymphoma',
            patient_age=59,
            gender='F',
            country='GB',
            prior_therapy='None',
            ecog_performance_status=0,
            karnofsky_performance_score=90,
            flipi_score=3,
            hemoglobin_level='11.5',
            no_hiv_status=True,
            no_hepatitis_b_status=True,
            no_hepatitis_c_status=True,
            no_other_active_malignancies=True,
        ),
    ),

    # ── Follicular Lymphoma — relapsed, should match TEST-FL-002
    dict(
        person_id=9004,
        person_defaults=dict(year_of_birth=1955, gender_source_value='M'),
        pi=dict(
            disease='follicular lymphoma',
            patient_age=69,
            gender='M',
            country='US',
            prior_therapy='More than two lines of therapy',
            first_line_therapy='R-CHOP',
            first_line_outcome='CR',
            second_line_therapy='R-Bendamustine',
            second_line_outcome='PD',
            ecog_performance_status=1,
            flipi_score=4,
            hemoglobin_level='10.8',
            no_hiv_status=True,
            no_hepatitis_b_status=True,
            no_hepatitis_c_status=True,
            no_other_active_malignancies=True,
        ),
    ),

    # ── Breast Cancer — TNBC, should match TEST-BC-001
    dict(
        person_id=9005,
        person_defaults=dict(year_of_birth=1975, gender_source_value='F'),
        pi=dict(
            disease='breast cancer',
            patient_age=49,
            gender='F',
            country='US',
            stage='IV',
            prior_therapy='One line',
            first_line_therapy='Carboplatin + Paclitaxel',
            first_line_outcome='PD',
            ecog_performance_status=1,
            estrogen_receptor_status='er_minus',
            progesterone_receptor_status='pr_minus',
            her2_status='her2_minus',
            tnbc_status=True,
            metastatic_status=True,
            no_hiv_status=True,
            no_hepatitis_b_status=True,
            no_hepatitis_c_status=True,
            no_other_active_malignancies=True,
        ),
    ),

    # ── CLL — relapsed, should match TEST-CLL-001
    dict(
        person_id=9006,
        person_defaults=dict(year_of_birth=1950, gender_source_value='M'),
        pi=dict(
            disease='chronic lymphocytic leukemia',
            patient_age=74,
            gender='M',
            country='US',
            prior_therapy='More than two lines of therapy',
            first_line_therapy='FCR',
            first_line_outcome='CR',
            second_line_therapy='Ibrutinib',
            second_line_outcome='PD',
            binet_stage='C',
            tp53_disruption=False,
            absolute_lymphocyte_count=42.5,
            hemoglobin_level='10.5',
            platelet_count=98000,
            ecog_performance_status=1,
            no_hiv_status=True,
            no_hepatitis_b_status=True,
            no_hepatitis_c_status=True,
            no_other_active_malignancies=True,
        ),
    ),

    # ── CLL — treatment-naive, should match TEST-CLL-002
    dict(
        person_id=9007,
        person_defaults=dict(year_of_birth=1960, gender_source_value='F'),
        pi=dict(
            disease='chronic lymphocytic leukemia',
            patient_age=64,
            gender='F',
            country='CA',
            prior_therapy='None',
            binet_stage='B',
            absolute_lymphocyte_count=28.0,
            hemoglobin_level='11.8',
            platelet_count=125000,
            ecog_performance_status=0,
            no_hiv_status=True,
            no_hepatitis_b_status=True,
            no_hepatitis_c_status=True,
            no_other_active_malignancies=True,
        ),
    ),
]


class Command(BaseCommand):
    help = 'Create fake test patients (Person + PatientInfo) for local end-to-end testing.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Delete test persons (IDs 9001–9999) before seeding.',
        )

    def handle(self, *args, **options):
        if options['clear']:
            deleted, _ = Person.objects.filter(
                person_id__gte=9001, person_id__lte=9999
            ).delete()
            self.stdout.write(f'Deleted {deleted} existing test person records (cascade includes PatientInfo).')

        created_p = updated_p = created_pi = updated_pi = 0

        for spec in TEST_PATIENTS:
            person_id = spec['person_id']
            person, p_created = Person.objects.update_or_create(
                person_id=person_id,
                defaults=spec['person_defaults'],
            )
            if p_created:
                created_p += 1
            else:
                updated_p += 1

            _, pi_created = PatientInfo.objects.update_or_create(
                person=person,
                defaults=spec['pi'],
            )
            if pi_created:
                created_pi += 1
            else:
                updated_pi += 1

            disease = spec['pi']['disease']
            self.stdout.write(f'  person_id={person_id}  [{disease}]  age={spec["pi"]["patient_age"]}')

        self.stdout.write(self.style.SUCCESS(
            f'\nPerson   : {created_p} created, {updated_p} updated\n'
            f'PatientInfo: {created_pi} created, {updated_pi} updated'
        ))
