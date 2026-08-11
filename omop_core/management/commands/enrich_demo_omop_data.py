"""
Fill demo-visible OMOP gaps so derived PatientRecord fields stop reading empty.

An audit of the 3001-patient staging cohort found whole field groups blank in
the UI because no OMOP row backs them — not because derivation is broken:

    weight / height / BMI / heart rate      0% of ALL 3001 patients
    systolic+diastolic BP, ECOG, KPS, LDH   0% of the breast cohort
    Nottingham biopsy grade                 0% of the breast cohort
    hematocrit                              0% of all cohorts
    ANC, eGFR                               0% of myeloma and lymphoma

Every gap is an absent row rather than a row with a null value, so this command
only ever inserts; it never edits an existing measurement. Re-running it is a
no-op for anyone already covered, which is what makes it safe to run before each
demo refresh.

Deliberately NOT covered, because the codes the derivation searches for do not
mean what the derivation thinks they mean (verified against the loaded Athena
vocabulary):

    pd_l1_tumor_cells    85337-4  is 'Estrogen receptor Ag ... by Immune stain'
    pd_l1_ic_percentage  85336-6  is 'DCIS intraductal extension'
    pd_l1_combined_...   96893-3  is 'ERBB2 gene duplication in Tumor by FISH'
    menopausal_status    76690-7  is 'Sexual orientation'
    genetic_mutations    21667-1  is F5, not TP53;  62318-1 is a Niemann-Pick
                                  newborn screen, not PIK3CA;  21637-4 is
                                  BRCA1 c.185delAG, not BRCA2

Filling those would write clinically false rows — and 85337-4 already sits in
the ER biomarker set, so a "PD-L1" row under it would be read back as estrogen
receptor data. The mapping constants in patient_record_service need correcting
first; this is the same defect class as the LOINC 10839-9 Troponin I/T mismatch
called out in remap_local_drug_concepts.

Values are drawn per person from a seed derived from person_id, so a given
patient gets the same measurements on every run and demos stay reproducible.

Usage:
    python manage.py enrich_demo_omop_data                 # dry run (default)
    python manage.py enrich_demo_omop_data --apply
    python manage.py enrich_demo_omop_data --apply --cohort breast --limit 50
    python manage.py enrich_demo_omop_data --apply --person-ids 3542,7097
"""
import random
from collections import defaultdict
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from omop_core.models import Concept, Measurement, PatientRecord
from omop_core.services.mappings import CONCEPT_GENERIC_LAB, CONCEPT_LAB_TYPE
from omop_core.services.pk import next_pk_batch
from omop_core.signals import suppress_patient_record_refresh

ALL_COHORTS = ('breast', 'myeloma', 'lymphoma')


def _cohort_of(disease):
    d = (disease or '').lower()
    if 'breast' in d:
        return 'breast'
    if 'myeloma' in d:
        return 'myeloma'
    if 'lymphom' in d or 'follicular' in d:
        return 'lymphoma'
    return 'other'


# Height first, then weight from a target BMI, so the BMI PatientRecord.save()
# computes from the pair lands in a plausible range instead of pairing a 150 cm
# height with a 110 kg weight.
def _height_cm(rng, _cohort):
    return round(rng.uniform(152.0, 188.0), 1)


def _weight_kg_for(height_cm, rng):
    bmi = rng.uniform(19.0, 32.0)
    return round(bmi * (height_cm / 100.0) ** 2, 1)


# field -> spec. `value` takes (rng, cohort) and returns the numeric result.
# `cohorts` limits the spec to the cohorts actually missing it.
SPECS = [
    # --- physical measurements: absent for every patient in every cohort
    {'key': 'height', 'loinc': '8302-2', 'unit': 'cm',
     'value': _height_cm, 'cohorts': ALL_COHORTS},
    # weight is derived from the height drawn above; see _measurements_for.
    {'key': 'weight', 'loinc': '29463-7', 'unit': 'kg',
     'value': None, 'cohorts': ALL_COHORTS},
    {'key': 'heartrate', 'loinc': '8867-4', 'unit': '/min',
     'value': lambda r, c: r.randint(56, 94), 'cohorts': ALL_COHORTS},
    {'key': 'hematocrit', 'loinc': '20570-8', 'unit': '%',
     'value': lambda r, c: round(r.uniform(33.0, 46.0), 1), 'cohorts': ALL_COHORTS},

    # --- vitals + performance + LDH: present for myeloma/lymphoma, absent for breast
    {'key': 'systolic', 'loinc': '8480-6', 'unit': 'mm[Hg]',
     'value': lambda r, c: r.randint(104, 146), 'cohorts': ('breast',)},
    {'key': 'diastolic', 'loinc': '8462-4', 'unit': 'mm[Hg]',
     'value': lambda r, c: r.randint(64, 92), 'cohorts': ('breast',)},
    {'key': 'ecog', 'loinc': '89247-1', 'unit': '{score}',
     'value': lambda r, c: r.choices([0, 1, 2], weights=[50, 35, 15])[0],
     'cohorts': ('breast',)},
    {'key': 'karnofsky', 'loinc': '89243-0', 'unit': '{score}',
     'value': lambda r, c: r.choice([70, 80, 90, 90, 100, 100]),
     'cohorts': ('breast',)},
    {'key': 'ldh', 'loinc': '2532-0', 'unit': 'U/L',
     'value': lambda r, c: r.randint(120, 280), 'cohorts': ('breast',)},
    # Nottingham grade is 1-3 and only meaningful for a breast specimen.
    {'key': 'biopsy_grade', 'loinc': '44648-4', 'unit': '{score}',
     'value': lambda r, c: r.choices([1, 2, 3], weights=[25, 45, 30])[0],
     'cohorts': ('breast',)},

    # --- CBC/renal: present for breast, absent for myeloma/lymphoma
    {'key': 'anc', 'loinc': '751-8', 'unit': '10*3/uL',
     'value': lambda r, c: round(r.uniform(1.6, 6.8), 1),
     'cohorts': ('myeloma', 'lymphoma')},
    {'key': 'egfr', 'loinc': '62238-1', 'unit': 'mL/min/1.73m2',
     'value': lambda r, c: r.randint(58, 122),
     'cohorts': ('myeloma', 'lymphoma')},
]


class Command(BaseCommand):
    help = 'Insert missing OMOP measurements so demo PatientRecord fields populate.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write the rows. Without this the command only reports.')
        parser.add_argument('--cohort', choices=ALL_COHORTS,
                            help='Restrict to one disease cohort.')
        parser.add_argument('--person-ids', help='Comma-separated person_ids.')
        parser.add_argument('--limit', type=int, help='Process at most N patients.')
        parser.add_argument('--batch-size', type=int, default=500,
                            help='Measurement rows per bulk insert (default 500).')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — nothing will be written. Re-run with --apply.\n'))

        concepts, fallbacks = self._resolve_concepts()
        if fallbacks:
            self.stdout.write(self.style.WARNING(
                'No LOINC concept for {}; using the generic lab concept and '
                'relying on measurement_source_value for derivation.\n'.format(
                    ', '.join(sorted(fallbacks)))))

        type_concept = Concept.objects.filter(concept_id=CONCEPT_LAB_TYPE).first()
        if type_concept is None:
            raise CommandError(
                f'Measurement type concept {CONCEPT_LAB_TYPE} is missing. '
                f'Run seed_omop_concepts first.')

        records = self._select_records(options)
        self.stdout.write(f'{len(records)} patient(s) in scope.\n')

        existing = self._existing_codes_by_person([pid for pid, _ in records])

        pending = []
        per_field = defaultdict(int)
        touched = set()
        for person_id, disease in records:
            cohort = _cohort_of(disease)
            rows = self._measurements_for(person_id, cohort, existing[person_id])
            if rows:
                touched.add(person_id)
            for key, loinc, value, unit in rows:
                per_field[key] += 1
                pending.append((person_id, loinc, value, unit))

        self._report(per_field, len(touched))

        if not apply_changes or not pending:
            if not pending:
                self.stdout.write(self.style.SUCCESS(
                    'Nothing to do — every patient in scope already has these rows.'))
            elif not apply_changes:
                self.stdout.write(self.style.WARNING(
                    'Nothing was written. Re-run with --apply.'))
            return

        self._write(pending, concepts, type_concept, options['batch_size'])
        stale = self._mark_stale(touched)
        self.stdout.write(self.style.SUCCESS(
            f'Inserted {len(pending)} measurement(s) for {len(touched)} patient(s); '
            f'{stale} PatientRecord(s) marked stale.'))
        self.stdout.write(self.style.WARNING(
            'Run `manage.py backfill_patient_records` to re-derive them.'))

    # ------------------------------------------------------------------

    def _resolve_concepts(self):
        """LOINC code -> Concept, falling back to the generic lab concept.

        Scoped to vocabulary_id='LOINC': a bare concept_code is ambiguous across
        vocabularies, and picking the wrong one is how a measurement ends up
        asserting a test it is not.
        """
        generic = Concept.objects.filter(concept_id=CONCEPT_GENERIC_LAB).first()
        concepts, fallbacks = {}, set()
        for spec in SPECS:
            loinc = spec['loinc']
            c = (Concept.objects
                 .filter(concept_code=loinc, vocabulary_id='LOINC', domain_id='Measurement')
                 .order_by('concept_id').first())
            if c is None:
                fallbacks.add(loinc)
                c = generic
            concepts[loinc] = c
        if any(c is None for c in concepts.values()):
            raise CommandError(
                f'Generic lab concept {CONCEPT_GENERIC_LAB} is missing and some '
                f'LOINC concepts are unresolved. Run seed_omop_concepts first.')
        return concepts, fallbacks

    def _select_records(self, options):
        qs = PatientRecord.objects.all()
        if options['person_ids']:
            try:
                ids = [int(x) for x in options['person_ids'].split(',') if x.strip()]
            except ValueError:
                raise CommandError('--person-ids must be comma-separated integers.')
            qs = qs.filter(person_id__in=ids)
        rows = [
            (pid, disease) for pid, disease in qs.values_list('person_id', 'disease')
            if _cohort_of(disease) in (
                (options['cohort'],) if options['cohort'] else ALL_COHORTS)
        ]
        rows.sort()
        if options['limit']:
            rows = rows[:options['limit']]
        return rows

    def _existing_codes_by_person(self, person_ids):
        """LOINC codes each person already has, by concept code or source value.

        Derivation matches on either, so a row found by one of them counts as
        covered and must not be duplicated.
        """
        codes = {spec['loinc'] for spec in SPECS}
        found = defaultdict(set)
        qs = (Measurement.objects
              .filter(person_id__in=person_ids)
              .filter(Q(measurement_concept__concept_code__in=codes)
                      | Q(measurement_source_value__in=codes))
              .values_list('person_id', 'measurement_concept__concept_code',
                           'measurement_source_value'))
        for pid, concept_code, source_value in qs.iterator(chunk_size=5000):
            if concept_code in codes:
                found[pid].add(concept_code)
            if source_value in codes:
                found[pid].add(source_value)
        for pid in person_ids:
            found.setdefault(pid, set())
        return found

    def _measurements_for(self, person_id, cohort, already_have):
        """Rows to insert for one person: (key, loinc, value, unit)."""
        # Seeded from person_id so a patient's demo values never move between
        # runs, and so a partial run can be resumed without contradicting itself.
        rng = random.Random(person_id)
        rows = []
        height = None
        for spec in SPECS:
            if cohort not in spec['cohorts']:
                continue
            loinc = spec['loinc']
            if spec['key'] == 'height':
                # Drawn even when already present so weight stays consistent
                # with the height actually on file.
                height = spec['value'](rng, cohort)
                if loinc in already_have:
                    continue
                rows.append((spec['key'], loinc, height, spec['unit']))
                continue
            if spec['key'] == 'weight':
                if loinc in already_have or height is None:
                    continue
                rows.append((spec['key'], loinc, _weight_kg_for(height, rng), spec['unit']))
                continue
            if loinc in already_have:
                continue
            rows.append((spec['key'], loinc, spec['value'](rng, cohort), spec['unit']))
        return rows

    def _write(self, pending, concepts, type_concept, batch_size):
        # Dated recently so these read as current observations in a demo rather
        # than decades-old history; the synthetic cohort's own dates run from the
        # 1970s, which would bury them at the bottom of any latest-first view.
        today = date.today()
        for start in range(0, len(pending), batch_size):
            chunk = pending[start:start + batch_size]
            with transaction.atomic():
                ids = next_pk_batch(Measurement, 'measurement_id', len(chunk))
                objs = []
                for mid, (person_id, loinc, value, unit) in zip(ids, chunk):
                    objs.append(Measurement(
                        measurement_id=mid,
                        person_id=person_id,
                        measurement_concept=concepts[loinc],
                        measurement_date=today - timedelta(days=person_id % 30),
                        measurement_type_concept=type_concept,
                        value_as_number=value,
                        measurement_source_value=loinc,
                        unit_source_value=unit,
                    ))
                # These inserts would otherwise fire one full re-derivation per
                # row; the backfill re-derives each patient once at the end.
                with suppress_patient_record_refresh():
                    Measurement.objects.bulk_create(objs)
            self.stdout.write(f'  inserted {start + len(chunk)}/{len(pending)}')

    def _mark_stale(self, person_ids):
        """Zero derivation_version so backfill_patient_records picks these up.

        bulk_create sends no post_save, so without this nothing would notice the
        new measurements and the read model would stay empty.
        """
        return PatientRecord.objects.filter(person_id__in=person_ids).update(
            derivation_version=0)

    def _report(self, per_field, n_touched):
        if not per_field:
            return
        self.stdout.write('Rows to insert by field:')
        for spec in SPECS:
            n = per_field.get(spec['key'], 0)
            if n:
                self.stdout.write(f'  {spec["key"]:16s} {spec["loinc"]:10s} {n:6d}')
        self.stdout.write(f'  {"":16s} {"TOTAL":10s} {sum(per_field.values()):6d} '
                          f'across {n_touched} patient(s)\n')
