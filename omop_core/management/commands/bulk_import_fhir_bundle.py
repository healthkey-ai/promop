"""
Bulk-import a FHIR R4 Bundle JSON file into OMOP tables using batch PK
allocation and bulk_create, bypassing the per-patient upload_fhir view.

~100x faster than import_fhir_bundle for large synthetic cohorts (minutes
instead of hours for 1000 patients).

Usage:
    DATABASE_URL=... python manage.py bulk_import_fhir_bundle \
      --file /tmp/mm_bundle.json --org synthea-mm

    # Wipe existing org data before import:
    DATABASE_URL=... python manage.py bulk_import_fhir_bundle \
      --file /tmp/mm_bundle.json --org synthea-mm --wipe-existing

    # Skip PatientRecord refresh (when enrichment handles it):
    DATABASE_URL=... python manage.py bulk_import_fhir_bundle \
      --file /tmp/mm_bundle.json --org synthea-mm --skip-refresh
"""

import json
import logging
import socket
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connection, connections, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# Socket timeout for remote DB connections
_SOCKET_TIMEOUT = 120  # seconds — more generous for bulk writes


def _patch_db_timeouts():
    """Add statement_timeout and keepalive options to prevent hanging on Render."""
    socket.setdefaulttimeout(_SOCKET_TIMEOUT)
    db = connections.databases['default']
    opts = dict(db.get('OPTIONS', {}))
    existing_options = opts.get('options', '')
    if 'statement_timeout' not in existing_options:
        # 5 minutes per statement for bulk creates
        opts['options'] = (existing_options + ' -c statement_timeout=300000').strip()
    opts.setdefault('keepalives', 1)
    opts.setdefault('keepalives_idle', 20)
    opts.setdefault('keepalives_interval', 5)
    opts.setdefault('keepalives_count', 3)
    db['OPTIONS'] = opts


class Command(BaseCommand):
    help = 'Bulk-import a FHIR R4 Bundle into OMOP tables (~100x faster than import_fhir_bundle)'

    def add_arguments(self, parser):
        parser.add_argument('--file', dest='file', required=True,
                            help='Path to a single FHIR Bundle JSON file')
        parser.add_argument('--org', dest='org_slug', required=True,
                            help='Org slug to create (if needed) and assign patients to')
        parser.add_argument('--wipe-existing', action='store_true', dest='wipe_existing',
                            help='Delete all patients for this org before importing')
        parser.add_argument('--skip-refresh', action='store_true', dest='skip_refresh',
                            help='Skip PatientRecord refresh (use when enrichment handles it)')
        parser.add_argument('--batch-size', type=int, default=500, dest='batch_size',
                            help='bulk_create batch size (default: 500)')
        parser.add_argument('--email', default=None, dest='email',
                            help='Admin email to authenticate as (default: first superuser)')

    def _print(self, msg, err=False):
        stream = self.stderr if err else self.stdout
        stream.write(msg + '\n')
        stream.flush()

    def handle(self, *args, **options):
        _patch_db_timeouts()

        # ── Validate input ──────────────────────────────────────────────
        bundle_path = Path(options['file'])
        if not bundle_path.exists():
            raise CommandError(f'File not found: {bundle_path}')

        self._print(f'Loading {bundle_path}…')
        t0 = time.time()
        with open(bundle_path) as f:
            bundle = json.load(f)
        if bundle.get('resourceType') != 'Bundle':
            raise CommandError('File must be a FHIR Bundle')

        # ── Get user ────────────────────────────────────────────────────
        close_old_connections()
        User = get_user_model()
        if options['email']:
            try:
                user = User.objects.get(email=options['email'])
            except User.DoesNotExist:
                raise CommandError(f"User '{options['email']}' not found")
        else:
            user = User.objects.filter(is_staff=True).order_by('pk').first()
            if user is None:
                raise CommandError('No staff user found. Run: manage.py createsuperuser (or set is_staff=True on an existing user)')

        # ── Org setup ───────────────────────────────────────────────────
        from omop_core.models import Organization
        org, created = Organization.objects.get_or_create(
            slug=options['org_slug'],
            defaults={'name': options['org_slug'].upper(), 'created_by': user},
        )
        action = 'Created' if created else 'Found existing'
        self._print(f'{action} org: {org.name} (slug={org.slug})')

        # ── Wipe existing ──────────────────────────────────────────────
        if options['wipe_existing']:
            self._wipe_org(org)

        # ── Phase 1: Parse & group by patient ──────────────────────────
        patient_groups = self._group_by_patient(bundle)
        self._print(f'Phase 1: {len(patient_groups)} patients parsed in {time.time()-t0:.1f}s')

        # ── Phase 2: Pre-resolve concepts ──────────────────────────────
        t1 = time.time()
        concept_cache = self._preload_concepts(patient_groups)
        self._print(f'Phase 2: Concepts pre-resolved in {time.time()-t1:.1f}s '
                     f'({len(concept_cache)} cached)')

        # ── Phase 3-4: Build & bulk-create OMOP rows ──────────────────
        t2 = time.time()
        stats = self._build_and_create(patient_groups, org, concept_cache, options['batch_size'])
        self._print(f'Phase 3-4: OMOP rows created in {time.time()-t2:.1f}s')
        for table, count in sorted(stats.items()):
            self._print(f'  {table}: {count}')

        # ── Phase 5: PatientRecord refresh ─────────────────────────────
        if not options['skip_refresh']:
            self._refresh_patients(org)
        else:
            self._print('Phase 5: Skipped (--skip-refresh)')

        # ── Phase 6: Summary ──────────────────────────────────────────
        total_time = time.time() - t0
        self._print(self.style.SUCCESS(
            f'Done. {len(patient_groups)} patients imported in {total_time:.1f}s '
            f'({total_time/max(len(patient_groups),1):.2f}s/patient)'
        ))

    # ==================================================================
    # Phase helpers
    # ==================================================================

    def _group_by_patient(self, bundle):
        """Split bundle entries into per-patient groups."""
        groups = []
        current = []
        for entry in bundle.get('entry', []):
            res = entry.get('resource', {})
            rt = res.get('resourceType')
            if rt == 'Patient' and current:
                groups.append(current)
                current = []
            current.append(entry)
        if current:
            groups.append(current)
        return groups

    def _wipe_org(self, org):
        """Delete all patients and cascaded OMOP data for an org."""
        from omop_core.models import PatientRecord, Person
        self._print(f'Wiping existing data for org={org.slug}…')
        prs = PatientRecord.objects.filter(organization=org).select_related('person')
        person_ids = list(prs.values_list('person_id', flat=True))
        if not person_ids:
            self._print('  No existing data to wipe.')
            return
        # Delete persons — cascades to all OMOP clinical tables + PatientRecord
        deleted_count, _ = Person.objects.filter(person_id__in=person_ids).delete()
        self._print(f'  Deleted {deleted_count} records (persons + cascaded rows)')

    def _preload_concepts(self, patient_groups):
        """Batch-load all concepts referenced in the bundle."""
        from omop_core.models import Concept

        # Collect all codes by vocabulary
        # Always include vital LOINCs needed for Patient extension → Measurement rows
        loinc_codes = {'29463-7', '8302-2', '8480-6', '8462-4', '8867-4'}
        snomed_codes = set()
        rxnorm_codes = set()
        hemonc_codes = set()
        visit_class_codes = set()

        for group in patient_groups:
            for entry in group:
                res = entry.get('resource', {})
                rt = res.get('resourceType')

                codings = []
                if 'code' in res:
                    codings.extend(res['code'].get('coding', []))
                if 'vaccineCode' in res:
                    codings.extend(res['vaccineCode'].get('coding', []))
                if 'medicationCodeableConcept' in res:
                    codings.extend(res['medicationCodeableConcept'].get('coding', []))

                # Component codings (BP panel etc.)
                for comp in res.get('component', []):
                    codings.extend(comp.get('code', {}).get('coding', []))

                for coding in codings:
                    system = coding.get('system', '')
                    code = coding.get('code', '')
                    if not code:
                        continue
                    if 'loinc' in system:
                        loinc_codes.add(code)
                    elif 'snomed' in system:
                        snomed_codes.add(code)
                    elif 'rxnorm' in system:
                        rxnorm_codes.add(code)
                    elif 'HemOnc' in system or 'hemonc' in system:
                        hemonc_codes.add(code)

                if rt == 'Encounter':
                    class_info = res.get('class', {})
                    cc = class_info.get('code')
                    if cc:
                        visit_class_codes.add(cc)

        # Batch query
        cache = {}  # (vocab, code) -> Concept

        def _load_vocab(vocab_id, codes):
            if not codes:
                return
            for c in Concept.objects.filter(vocabulary_id=vocab_id, concept_code__in=codes):
                cache[(vocab_id, c.concept_code)] = c

        _load_vocab('LOINC', loinc_codes)
        _load_vocab('SNOMED', snomed_codes)
        _load_vocab('RxNorm', rxnorm_codes)
        _load_vocab('HemOnc', hemonc_codes)
        _load_vocab('RxNorm Extension', rxnorm_codes)  # some drugs only in extension

        # Load visit class concepts (FHIR-VISIT-AMB etc.)
        if visit_class_codes:
            fhir_codes = {f'FHIR-VISIT-{cc}' for cc in visit_class_codes}
            for c in Concept.objects.filter(vocabulary_id='FHIR', concept_code__in=fhir_codes):
                cache[('FHIR', c.concept_code)] = c

        # Load system concepts by ID
        system_ids = [32817, 32856, 32869, 32531, 1147094, 8507, 8532, 8551]
        for c in Concept.objects.filter(concept_id__in=system_ids):
            cache[('_id', c.concept_id)] = c

        # Load concept 0 (No matching concept) — used as fallback
        try:
            c0 = Concept.objects.get(concept_id=0)
            cache[('_id', 0)] = c0
        except Concept.DoesNotExist:
            pass

        return cache

    def _get_concept(self, cache, vocab, code, fallback_id=0):
        """Look up a concept from cache, falling back to concept 0."""
        c = cache.get((vocab, code))
        if c:
            return c
        return cache.get(('_id', fallback_id))

    def _get_concept_by_id(self, cache, concept_id):
        return cache.get(('_id', concept_id))

    def _build_and_create(self, patient_groups, org, cache, batch_size):
        """Phase 3-4: Build OMOP model instances and bulk_create them."""
        from omop_core.models import (
            Concept, ConditionOccurrence, Death, DrugExposure, Location,
            Measurement, Observation, PatientRecord, Person,
            ProcedureOccurrence, VisitDetail, VisitOccurrence,
        )
        from omop_oncology.models import Episode, EpisodeEvent
        from omop_core.services.pk import next_pk_batch
        from omop_core.services.mappings import get_gender_concept
        from omop_core.signals import suppress_patient_record_refresh

        n_patients = len(patient_groups)

        # ── Count rows needed per table ─────────────────────────────────
        # We estimate generously; unused PKs are harmless (sequence advances)
        n_encounters = 0
        n_observations_fhir = 0
        n_medications = 0
        n_conditions = 0
        n_procedures = 0
        n_immunizations = 0

        for group in patient_groups:
            for entry in group:
                rt = entry.get('resource', {}).get('resourceType')
                if rt == 'Encounter':
                    n_encounters += 1
                elif rt == 'Observation':
                    n_observations_fhir += 1
                elif rt == 'MedicationStatement':
                    n_medications += 1
                elif rt == 'Condition':
                    n_conditions += 1
                elif rt == 'Procedure':
                    n_procedures += 1
                elif rt == 'Immunization':
                    n_immunizations += 1

        # ── Pre-allocate PKs ────────────────────────────────────────────
        self._print(f'  Allocating PKs: {n_patients} persons, {n_encounters} visits, '
                     f'{n_observations_fhir} obs, {n_medications} meds…')

        person_pks = next_pk_batch(Person, 'person_id', n_patients)
        location_pks = next_pk_batch(Location, 'location_id', n_patients)
        visit_pks = next_pk_batch(VisitOccurrence, 'visit_occurrence_id', n_encounters)
        visit_detail_pks = next_pk_batch(VisitDetail, 'visit_detail_id', n_encounters)
        # Observations go to either Measurement or Observation table
        # Extra PKs: +5 per patient for extension vitals (weight, height, BP, HR)
        meas_pks = next_pk_batch(Measurement, 'measurement_id', n_observations_fhir + n_patients * 5)
        # Extra PKs: +4 per patient for extension observations (cytogenetics, SCT)
        obs_pks = next_pk_batch(Observation, 'observation_id', n_observations_fhir * 2 + n_patients * 4)
        cond_pks = next_pk_batch(ConditionOccurrence, 'condition_occurrence_id', n_conditions)
        # DrugExposure: regimen + individual drugs + immunizations
        de_pks = next_pk_batch(DrugExposure, 'drug_exposure_id', n_medications + n_immunizations)
        proc_pks = next_pk_batch(ProcedureOccurrence, 'procedure_occurrence_id', n_procedures)
        episode_pks = next_pk_batch(Episode, 'episode_id', n_medications)
        # EpisodeEvent doesn't have its own PK sequence — uses auto-generated id

        # PK iterators
        _pi = iter(person_pks)
        _li = iter(location_pks)
        _vi = iter(visit_pks)
        _vdi = iter(visit_detail_pks)
        _mi = iter(meas_pks)
        _oi = iter(obs_pks)
        _ci = iter(cond_pks)
        _di = iter(de_pks)
        _pri = iter(proc_pks)
        _ei = iter(episode_pks)

        def _next(it):
            return next(it, None)

        # System concepts
        ehr_type = self._get_concept_by_id(cache, 32817)
        lab_type = self._get_concept_by_id(cache, 32856)
        drug_type = self._get_concept_by_id(cache, 32869)
        tx_regimen = self._get_concept_by_id(cache, 32531)
        de_field = self._get_concept_by_id(cache, 1147094)
        concept_0 = self._get_concept_by_id(cache, 0)

        # ── LOINC codes that should go to Measurement (labs) ────────────
        # Everything else goes to Observation
        MEASUREMENT_LOINC = {
            '718-7', '4544-3', '6690-2', '789-8', '777-3', '751-8', '731-0', '742-7',
            '26499-4', '17861-6', '2000-8', '2160-0', '2164-2', '33914-3', '69405-9',
            '3094-0', '2951-2', '2823-3', '19123-9', '2601-3', '2777-1',
            '1975-2', '1968-7', '1742-6', '1920-8', '6768-6', '1751-7', '2885-2',
            '1754-1', '48346-3', '1952-1', '1988-5', '4537-7', '30341-2',
            '10839-9', '6598-7', '42637-9', '2345-7', '4548-4', '2532-0', '14804-9',
            '6301-6', '5902-2', '3173-2', '2039-6', '25390-6', '2857-1',
            '8806-2',  # EF
            '51435-6', '32730-5', '33944-8', '33945-5', '26098-4',  # MM disease burden
            '8480-6', '8462-4',  # BP components
            '63640-7',  # pack years
            '11286-7',  # drinks/week
            '89555-7',  # exercise minutes
            '63512-8',  # dependents
            # Behavioral / lifestyle (must be in Measurement for _get_behavior_data)
            '72166-2',  # smoking status
            '74013-4',  # alcohol use
            '68516-4',  # exercise frequency
            '88365-2',  # diet type
            '93831-6',  # sleep quality
            '73985-4',  # stress level
            '93033-9',  # social support
            '74165-2',  # employment status
            '82589-3',  # education level
            '45404-1',  # marital status
            '76513-1',  # insurance type
            '77243-3',  # annual household income
            # Wearable metrics
            '55423-8', '55411-3', '40443-4', '80404-7', '59408-5', '9279-1', '93832-4',
        }

        # ── Accumulate model instances ──────────────────────────────────
        persons = []
        locations = []
        visits = []
        visit_details = []
        measurements = []
        observations = []
        conditions = []
        drug_exposures = []
        procedures = []
        episodes = []
        episode_events = []
        patient_records = []
        deaths = []

        # Track FHIR ID → OMOP PK for cross-references within each patient
        for group_idx, group in enumerate(patient_groups):
            # Classify resources
            patient_res = None
            encounter_list = []
            observation_list = []
            condition_list = []
            medication_list = []
            procedure_list = []
            immunization_list = []

            for entry in group:
                res = entry.get('resource', {})
                rt = res.get('resourceType')
                if rt == 'Patient':
                    patient_res = res
                elif rt == 'Encounter':
                    encounter_list.append(res)
                elif rt == 'Observation':
                    observation_list.append(res)
                elif rt == 'Condition':
                    condition_list.append(res)
                elif rt == 'MedicationStatement':
                    medication_list.append(res)
                elif rt == 'Procedure':
                    procedure_list.append(res)
                elif rt == 'Immunization':
                    immunization_list.append(res)

            if not patient_res:
                continue

            # ── Person ──────────────────────────────────────────────────
            person_id = _next(_pi)
            if person_id is None:
                break

            names = patient_res.get('name', [{}])
            given = ' '.join(names[0].get('given', []))
            family = names[0].get('family', '')
            gender_str = patient_res.get('gender', '')
            birth_date_str = patient_res.get('birthDate', '')

            yob = mob = dob = None
            if birth_date_str:
                parts = birth_date_str.split('-')
                yob = int(parts[0]) if len(parts) >= 1 else None
                mob = int(parts[1]) if len(parts) >= 2 else None
                dob = int(parts[2]) if len(parts) >= 3 else None

            gender_concept = get_gender_concept(gender_str)

            # Extract extensions
            ext_data = self._parse_patient_extensions(patient_res)

            # Location
            loc_id = _next(_li)
            address = (patient_res.get('address') or [{}])[0] if patient_res.get('address') else {}
            loc = Location(
                location_id=loc_id,
                city=address.get('city', '')[:50] or None,
                state=address.get('state', '')[:2] or None,
                zip=address.get('postalCode', '')[:9] or None,
                country=address.get('country', '')[:100] or None,
                location_source_value='|'.join(filter(None, [
                    address.get('city', ''), address.get('state', ''),
                    address.get('postalCode', ''), address.get('country', ''),
                ]))[:50] or None,
            )
            locations.append(loc)

            person = Person(
                person_id=person_id,
                given_name=given[:100] or None,
                family_name=family[:100] or None,
                gender_concept=gender_concept,
                gender_source_value=gender_str[:50] if gender_str else None,
                year_of_birth=yob,
                month_of_birth=mob,
                day_of_birth=dob,
                location_id=loc_id,
                race_source_value=ext_data.get('race', '')[:50] or None,
                ethnicity_source_value=ext_data.get('ethnicity', '')[:50] or None,
            )
            persons.append(person)

            # ── Death ───────────────────────────────────────────────────
            deceased_dt = patient_res.get('deceasedDateTime')
            if deceased_dt and ehr_type:
                try:
                    d_date = _parse_date(deceased_dt)
                    deaths.append(Death(
                        person_id=person_id,
                        death_date=d_date,
                        death_type_concept=ehr_type,
                    ))
                except (ValueError, TypeError):
                    pass

            # ── Encounters → VisitOccurrence + VisitDetail ──────────────
            fhir_enc_to_visit = {}  # FHIR encounter id → VisitOccurrence instance
            for enc in encounter_list:
                v_pk = _next(_vi)
                vd_pk = _next(_vdi)
                if v_pk is None:
                    break

                period = enc.get('period', {})
                start_str = period.get('start', '')
                end_str = period.get('end', start_str)
                v_start = _parse_date(start_str) if start_str else date.today()
                v_end = _parse_date(end_str) if end_str else v_start

                class_info = enc.get('class', {})
                class_code = class_info.get('code', 'AMB')
                class_display = class_info.get('display', class_code)

                visit_concept = self._get_concept(cache, 'FHIR', f'FHIR-VISIT-{class_code}')
                if visit_concept is None:
                    visit_concept = ehr_type  # fallback

                vo = VisitOccurrence(
                    visit_occurrence_id=v_pk,
                    person_id=person_id,
                    visit_concept=visit_concept,
                    visit_start_date=v_start,
                    visit_start_datetime=timezone.make_aware(
                        datetime.combine(v_start, datetime.min.time())),
                    visit_end_date=v_end,
                    visit_end_datetime=timezone.make_aware(
                        datetime.combine(v_end, datetime.min.time())),
                    visit_type_concept=ehr_type or visit_concept,
                    visit_source_value=class_code[:255],
                )
                visits.append(vo)
                fhir_enc_to_visit[enc.get('id', '')] = vo

                if vd_pk is not None:
                    vd = VisitDetail(
                        visit_detail_id=vd_pk,
                        person_id=person_id,
                        visit_detail_concept=visit_concept,
                        visit_detail_start_date=v_start,
                        visit_detail_start_datetime=timezone.make_aware(
                            datetime.combine(v_start, datetime.min.time())),
                        visit_detail_end_date=v_end,
                        visit_detail_end_datetime=timezone.make_aware(
                            datetime.combine(v_end, datetime.min.time())),
                        visit_detail_type_concept=ehr_type or visit_concept,
                        visit_detail_source_value=class_code[:255],
                        visit_occurrence_id=v_pk,
                    )
                    visit_details.append(vd)

            # ── Observations → Measurement or Observation ───────────────
            for obs in observation_list:
                obs_codings = obs.get('code', {}).get('coding', [])
                obs_loinc = None
                obs_snomed = None
                for coding in obs_codings:
                    sys = coding.get('system', '')
                    if 'loinc' in sys:
                        obs_loinc = coding.get('code')
                    elif 'snomed' in sys:
                        obs_snomed = coding.get('code')

                obs_code = obs_loinc or obs_snomed or ''
                obs_text = obs.get('code', {}).get('text', '') or ''

                # Parse date
                eff_dt = obs.get('effectiveDateTime', '')
                obs_date_val = _parse_date(eff_dt) if eff_dt else date.today()
                obs_datetime = _parse_datetime(eff_dt) if eff_dt else datetime.now()

                # Parse value
                val_num, val_str, unit = _extract_obs_value(obs)

                # Determine target table
                if obs_loinc and obs_loinc in MEASUREMENT_LOINC:
                    # Measurement
                    m_pk = _next(_mi)
                    if m_pk is None:
                        continue
                    m_concept = self._get_concept(cache, 'LOINC', obs_loinc)
                    if m_concept is None:
                        m_concept = concept_0
                    measurements.append(Measurement(
                        measurement_id=m_pk,
                        person_id=person_id,
                        measurement_concept=m_concept,
                        measurement_date=obs_date_val,
                        measurement_datetime=timezone.make_aware(obs_datetime)
                            if obs_datetime and timezone.is_naive(obs_datetime) else obs_datetime,
                        measurement_type_concept=lab_type or m_concept,
                        value_as_number=val_num,
                        value_as_string=val_str[:60] if val_str else None,
                        measurement_source_value=(obs_loinc or obs_text)[:50],
                        unit_source_value=unit[:50] if unit else None,
                    ))
                else:
                    # Observation table
                    o_pk = _next(_oi)
                    if o_pk is None:
                        continue
                    if obs_loinc:
                        o_concept = self._get_concept(cache, 'LOINC', obs_loinc)
                    elif obs_snomed:
                        o_concept = self._get_concept(cache, 'SNOMED', obs_snomed)
                    else:
                        o_concept = concept_0
                    if o_concept is None:
                        o_concept = concept_0
                    observations.append(Observation(
                        observation_id=o_pk,
                        person_id=person_id,
                        observation_concept=o_concept,
                        observation_date=obs_date_val,
                        observation_datetime=timezone.make_aware(obs_datetime)
                            if obs_datetime and timezone.is_naive(obs_datetime) else obs_datetime,
                        observation_type_concept=ehr_type or o_concept,
                        value_as_number=val_num,
                        value_as_string=val_str[:60] if val_str else None,
                        observation_source_value=(obs_code or obs_text)[:50],
                        unit_source_value=unit[:50] if unit else None,
                    ))

            # ── Conditions → ConditionOccurrence ────────────────────────
            for cond in condition_list:
                c_pk = _next(_ci)
                if c_pk is None:
                    break

                cond_codings = cond.get('code', {}).get('coding', [])
                cond_concept = concept_0
                cond_source = ''
                for coding in cond_codings:
                    sys = coding.get('system', '')
                    code = coding.get('code', '')
                    if 'snomed' in sys:
                        c = self._get_concept(cache, 'SNOMED', code)
                        if c:
                            cond_concept = c
                        cond_source = code
                        break
                    elif 'icd-10' in sys:
                        cond_source = code

                onset = cond.get('onsetDateTime', '')
                cond_date = _parse_date(onset) if onset else date.today()

                # Clinical status
                cs_codings = cond.get('clinicalStatus', {}).get('coding', [])
                cs_val = cs_codings[0].get('code', '') if cs_codings else ''

                conditions.append(ConditionOccurrence(
                    condition_occurrence_id=c_pk,
                    person_id=person_id,
                    condition_concept=cond_concept,
                    condition_start_date=cond_date,
                    condition_start_datetime=timezone.make_aware(
                        datetime.combine(cond_date, datetime.min.time())),
                    condition_type_concept=ehr_type or cond_concept,
                    condition_source_value=(
                        cond.get('code', {}).get('text', '') or cond_source)[:50],
                    condition_status_source_value=cs_val[:50] if cs_val else None,
                ))

            # ── MedicationStatements → DrugExposure + Episode ───────────
            # Separate regimen-level (no partOf) from individual drugs (has partOf)
            regimen_meds = []
            drug_meds = []
            for med in medication_list:
                if med.get('partOf'):
                    drug_meds.append(med)
                else:
                    regimen_meds.append(med)

            # Process regimen-level entries → Episode + DrugExposure
            regimen_fhir_to_de = {}  # FHIR med id → DrugExposure PK
            for med in regimen_meds:
                de_pk = _next(_di)
                ep_pk = _next(_ei)
                if de_pk is None:
                    break

                med_cc = med.get('medicationCodeableConcept', {})
                med_codings = med_cc.get('coding', [])
                med_text = med_cc.get('text', '')

                # Resolve concept: try HemOnc first, then custom regimen
                drug_concept = concept_0
                for coding in med_codings:
                    sys = coding.get('system', '')
                    code = coding.get('code', '')
                    if 'HemOnc' in sys or 'hemonc' in sys:
                        c = self._get_concept(cache, 'HemOnc', code)
                        if c:
                            drug_concept = c
                            break
                    elif 'rxnorm' in sys.lower():
                        c = self._get_concept(cache, 'RxNorm', code)
                        if c is None:
                            c = self._get_concept(cache, 'RxNorm Extension', code)
                        if c:
                            drug_concept = c
                            break

                period = med.get('effectivePeriod', {})
                start_str = period.get('start', '')
                end_str = period.get('end', '')
                de_start = _parse_date(start_str) if start_str else date.today()
                de_end = _parse_date(end_str) if end_str else None

                de = DrugExposure(
                    drug_exposure_id=de_pk,
                    person_id=person_id,
                    drug_concept=drug_concept,
                    drug_exposure_start_date=de_start,
                    drug_exposure_start_datetime=timezone.make_aware(
                        datetime.combine(de_start, datetime.min.time())),
                    drug_exposure_end_date=de_end,
                    drug_exposure_end_datetime=timezone.make_aware(
                        datetime.combine(de_end, datetime.min.time())) if de_end else None,
                    drug_type_concept=drug_type or ehr_type,
                    drug_source_value=med_text[:50] if med_text else None,
                )
                drug_exposures.append(de)
                regimen_fhir_to_de[med.get('id', '')] = de_pk

                # Extract therapy line + outcome from extensions
                line_num = None
                outcome = None
                for ext in med.get('extension', []):
                    url = ext.get('url', '')
                    if 'therapy-line' in url:
                        line_num = ext.get('valueInteger')
                    elif 'therapy-outcome' in url:
                        outcome = ext.get('valueString')

                # Create Episode for this therapy line
                if ep_pk is not None and line_num is not None and tx_regimen and ehr_type:
                    ep = Episode(
                        episode_id=ep_pk,
                        person_id=person_id,
                        episode_concept=tx_regimen,
                        episode_start_date=de_start,
                        episode_start_datetime=timezone.make_aware(
                            datetime.combine(de_start, datetime.min.time())),
                        episode_end_date=de_end,
                        episode_end_datetime=timezone.make_aware(
                            datetime.combine(de_end, datetime.min.time())) if de_end else None,
                        episode_number=line_num,
                        episode_object_concept=drug_concept or concept_0,
                        episode_type_concept=ehr_type,
                        episode_source_value=f'LOT-{line_num}'[:50],
                    )
                    episodes.append(ep)

                    # Link DrugExposure to Episode via EpisodeEvent
                    if de_field:
                        episode_events.append(EpisodeEvent(
                            episode_id=ep_pk,
                            event_id=de_pk,
                            episode_event_field_concept=de_field,
                        ))

                    # Store outcome as Observation
                    if outcome:
                        outcome_snomed = {
                            'Complete Response': '182840001',
                            'Very Good Partial Response': '182840001',
                            'Partial Response': '182841002',
                            'Stable Disease': '182843004',
                            'Progressive Disease': '182842009',
                        }.get(outcome)
                        o_pk = _next(_oi)
                        if o_pk is not None:
                            outcome_concept = concept_0
                            if outcome_snomed:
                                c = self._get_concept(cache, 'SNOMED', outcome_snomed)
                                if c:
                                    outcome_concept = c
                            observations.append(Observation(
                                observation_id=o_pk,
                                person_id=person_id,
                                observation_concept=outcome_concept,
                                observation_date=de_end or de_start,
                                observation_datetime=timezone.make_aware(
                                    datetime.combine(de_end or de_start, datetime.min.time())),
                                observation_type_concept=ehr_type,
                                value_as_string=outcome[:60],
                                observation_source_value=f'LOT-{line_num}-outcome'[:50],
                            ))

            # Process individual drug entries → DrugExposure
            for med in drug_meds:
                de_pk = _next(_di)
                if de_pk is None:
                    break

                med_cc = med.get('medicationCodeableConcept', {})
                med_codings = med_cc.get('coding', [])
                med_text = med_cc.get('text', '')

                drug_concept = concept_0
                for coding in med_codings:
                    sys = coding.get('system', '')
                    code = coding.get('code', '')
                    if 'rxnorm' in sys.lower():
                        c = self._get_concept(cache, 'RxNorm', code)
                        if c is None:
                            c = self._get_concept(cache, 'RxNorm Extension', code)
                        if c:
                            drug_concept = c
                            break

                period = med.get('effectivePeriod', {})
                start_str = period.get('start', '')
                end_str = period.get('end', '')
                de_start = _parse_date(start_str) if start_str else date.today()
                de_end = _parse_date(end_str) if end_str else None

                de = DrugExposure(
                    drug_exposure_id=de_pk,
                    person_id=person_id,
                    drug_concept=drug_concept,
                    drug_exposure_start_date=de_start,
                    drug_exposure_start_datetime=timezone.make_aware(
                        datetime.combine(de_start, datetime.min.time())),
                    drug_exposure_end_date=de_end,
                    drug_exposure_end_datetime=timezone.make_aware(
                        datetime.combine(de_end, datetime.min.time())) if de_end else None,
                    drug_type_concept=drug_type or ehr_type,
                    drug_source_value=med_text[:50] if med_text else None,
                )
                drug_exposures.append(de)

                # Link to parent regimen's Episode via EpisodeEvent
                part_of = med.get('partOf', [{}])
                parent_ref = part_of[0].get('reference', '') if part_of else ''
                parent_id = parent_ref.split('/')[-1] if '/' in parent_ref else parent_ref
                parent_de_pk = regimen_fhir_to_de.get(parent_id)

                # Find the Episode that links to the parent DrugExposure
                if parent_de_pk and de_field:
                    # Find the episode for the parent
                    for ee in episode_events:
                        if ee.event_id == parent_de_pk:
                            episode_events.append(EpisodeEvent(
                                episode_id=ee.episode_id,
                                event_id=de_pk,
                                episode_event_field_concept=de_field,
                            ))
                            break

            # ── Procedures → ProcedureOccurrence ────────────────────────
            for proc in procedure_list:
                p_pk = _next(_pri)
                if p_pk is None:
                    break

                proc_codings = proc.get('code', {}).get('coding', [])
                proc_concept = concept_0
                proc_source = ''
                for coding in proc_codings:
                    sys = coding.get('system', '')
                    code = coding.get('code', '')
                    if 'snomed' in sys:
                        c = self._get_concept(cache, 'SNOMED', code)
                        if c:
                            proc_concept = c
                        proc_source = code
                        break

                proc_date_str = proc.get('performedDateTime', '')
                proc_date = _parse_date(proc_date_str) if proc_date_str else date.today()

                procedures.append(ProcedureOccurrence(
                    procedure_occurrence_id=p_pk,
                    person_id=person_id,
                    procedure_concept=proc_concept,
                    procedure_date=proc_date,
                    procedure_datetime=timezone.make_aware(
                        datetime.combine(proc_date, datetime.min.time())),
                    procedure_type_concept=ehr_type or proc_concept,
                    procedure_source_value=(
                        proc.get('code', {}).get('text', '') or proc_source)[:50],
                ))

            # ── Immunizations → DrugExposure ────────────────────────────
            for imm in immunization_list:
                de_pk = _next(_di)
                if de_pk is None:
                    break

                vax_cc = imm.get('vaccineCode', {})
                vax_text = vax_cc.get('text', '')
                vax_codings = vax_cc.get('coding', [])
                vax_concept = concept_0
                for coding in vax_codings:
                    sys = coding.get('system', '')
                    code = coding.get('code', '')
                    # CVX codes may be in various vocabs
                    if code:
                        for vocab in ['CVX', 'RxNorm', 'SNOMED']:
                            c = self._get_concept(cache, vocab, code)
                            if c:
                                vax_concept = c
                                break
                        if vax_concept != concept_0:
                            break

                occ_date_str = imm.get('occurrenceDateTime', '') or imm.get('recorded', '')
                occ_date = _parse_date(occ_date_str) if occ_date_str else date.today()

                drug_exposures.append(DrugExposure(
                    drug_exposure_id=de_pk,
                    person_id=person_id,
                    drug_concept=vax_concept,
                    drug_exposure_start_date=occ_date,
                    drug_exposure_start_datetime=timezone.make_aware(
                        datetime.combine(occ_date, datetime.min.time())),
                    drug_type_concept=drug_type or ehr_type,
                    drug_source_value=vax_text[:50] if vax_text else None,
                    route_source_value='VACCINE',
                ))

            # ── Write Patient extension vitals as Measurement rows ─────
            # These are derived by refresh_patient_record via _get_vitals_data
            _VITAL_EXT_LOINC = {
                'weight': ('29463-7', 'kg'),
                'height': ('8302-2', 'cm'),
                'systolic_blood_pressure': ('8480-6', 'mmHg'),
                'diastolic_blood_pressure': ('8462-4', 'mmHg'),
                'heartrate': ('8867-4', '/min'),
            }
            ext_date = date.today()
            for ext_field, (loinc, unit) in _VITAL_EXT_LOINC.items():
                val = ext_data.get(ext_field)
                if val is not None:
                    m_pk = _next(_mi)
                    if m_pk is None:
                        continue
                    m_concept = self._get_concept(cache, 'LOINC', loinc) or concept_0
                    measurements.append(Measurement(
                        measurement_id=m_pk,
                        person_id=person_id,
                        measurement_concept=m_concept,
                        measurement_date=ext_date,
                        measurement_datetime=timezone.make_aware(
                            datetime.combine(ext_date, datetime.min.time())),
                        measurement_type_concept=ehr_type or m_concept,
                        value_as_number=Decimal(str(val)),
                        measurement_source_value=loinc,
                        unit_source_value=unit,
                    ))

            # ── Write cytogenetics + SCT as Observation rows ──────────
            # These are derived by refresh_patient_record via _get_sct_cytogenetic_data
            _EXT_OBS = {
                'cytogenic_markers': 'mm-cytogenetic-markers',
                'sct_date_str': 'mm-sct-date',
                'sct_history_str': 'mm-sct-history',
                'sct_eligibility_str': 'mm-sct-eligibility',
            }
            for ext_field, src_val in _EXT_OBS.items():
                val = ext_data.get(ext_field)
                if val is not None and str(val).strip():
                    o_pk = _next(_oi)
                    if o_pk is None:
                        continue
                    observations.append(Observation(
                        observation_id=o_pk,
                        person_id=person_id,
                        observation_concept=concept_0,
                        observation_date=ext_date,
                        observation_datetime=timezone.make_aware(
                            datetime.combine(ext_date, datetime.min.time())),
                        observation_type_concept=ehr_type or concept_0,
                        value_as_string=str(val)[:60],
                        observation_source_value=src_val,
                    ))

            # ── PatientRecord stub ──────────────────────────────────────
            patient_records.append(PatientRecord(
                person_id=person_id,
                organization=org,
            ))

        # ── Bulk create in FK-dependency order ──────────────────────────
        self._print(f'  Bulk creating rows…')
        with suppress_patient_record_refresh():
            with transaction.atomic():
                Person.objects.bulk_create(persons, batch_size=batch_size)
                Location.objects.bulk_create(locations, batch_size=batch_size)
                if deaths:
                    Death.objects.bulk_create(deaths, batch_size=batch_size)
                VisitOccurrence.objects.bulk_create(visits, batch_size=batch_size)
                VisitDetail.objects.bulk_create(visit_details, batch_size=batch_size)
                Measurement.objects.bulk_create(measurements, batch_size=batch_size)
                Observation.objects.bulk_create(observations, batch_size=batch_size)
                ConditionOccurrence.objects.bulk_create(conditions, batch_size=batch_size)
                DrugExposure.objects.bulk_create(drug_exposures, batch_size=batch_size)
                ProcedureOccurrence.objects.bulk_create(procedures, batch_size=batch_size)
                Episode.objects.bulk_create(episodes, batch_size=batch_size)
                EpisodeEvent.objects.bulk_create(episode_events, batch_size=batch_size,
                                                  ignore_conflicts=True)
                PatientRecord.objects.bulk_create(patient_records, batch_size=batch_size)

        return {
            'Person': len(persons),
            'Location': len(locations),
            'Death': len(deaths),
            'VisitOccurrence': len(visits),
            'VisitDetail': len(visit_details),
            'Measurement': len(measurements),
            'Observation': len(observations),
            'ConditionOccurrence': len(conditions),
            'DrugExposure': len(drug_exposures),
            'ProcedureOccurrence': len(procedures),
            'Episode': len(episodes),
            'EpisodeEvent': len(episode_events),
            'PatientRecord': len(patient_records),
        }

    def _parse_patient_extensions(self, patient_res):
        """Extract HealthKey and US Core extensions from Patient resource.

        Returns a dict with keys matching PatientRecord fields where possible:
        race, ethnicity, weight, height, systolic_blood_pressure,
        diastolic_blood_pressure, heartrate, ecog_performance_status,
        cytogenic_markers, measurable_disease_imwg, sct_date, sct_history,
        sct_eligibility.
        """
        data = {}
        base = 'https://healthkey.ai/fhir/StructureDefinition/'
        _HK_EXTENSIONS = {
            f'{base}bodyWeight': ('weight', lambda e: e.get('valueQuantity', {}).get('value')),
            f'{base}bodyHeight': ('height', lambda e: e.get('valueQuantity', {}).get('value')),
            f'{base}systolic-bp': ('systolic_blood_pressure', lambda e: e.get('valueQuantity', {}).get('value')),
            f'{base}diastolic-bp': ('diastolic_blood_pressure', lambda e: e.get('valueQuantity', {}).get('value')),
            f'{base}heartRate': ('heartrate', lambda e: e.get('valueQuantity', {}).get('value')),
            f'{base}ecog-performance-status': ('ecog_performance_status', lambda e: e.get('valueInteger')),
            f'{base}mm-cytogenetic-markers': ('cytogenic_markers', lambda e: e.get('valueString')),
            f'{base}mm-measurable-disease-imwg': ('measurable_disease_imwg', lambda e: e.get('valueBoolean')),
            f'{base}mm-sct-date': ('sct_date_str', lambda e: e.get('valueString')),
            f'{base}mm-sct-history': ('sct_history_str', lambda e: e.get('valueString')),
            f'{base}mm-sct-eligibility': ('sct_eligibility_str', lambda e: e.get('valueString')),
        }
        for ext in patient_res.get('extension', []):
            url = ext.get('url', '')
            # HealthKey typed extensions
            if url in _HK_EXTENSIONS:
                field, parser = _HK_EXTENSIONS[url]
                val = parser(ext)
                if val is not None:
                    data[field] = val
            # Race / ethnicity — HealthKey or US Core
            elif 'ethnicity' in url:
                if 'valueString' in ext:
                    data['ethnicity'] = ext['valueString']
                else:
                    for sub in ext.get('extension', []):
                        if sub.get('url') == 'text':
                            data['ethnicity'] = sub.get('valueString', '')
            elif 'race' in url and 'race' not in data:
                if 'valueString' in ext:
                    data['race'] = ext['valueString']
                else:
                    for sub in ext.get('extension', []):
                        if sub.get('url') == 'text':
                            data['race'] = sub.get('valueString', '')
        return data

    def _refresh_patients(self, org):
        """Phase 5: Refresh PatientRecord for all imported patients."""
        from omop_core.models import PatientRecord
        from omop_core.services.patient_record_service import refresh_patient_record
        from omop_core.services.lot_inference_service import infer_lot_for_person

        self._print('Phase 5: Refreshing PatientRecord…')
        t0 = time.time()
        close_old_connections()

        patients = PatientRecord.objects.filter(
            organization=org
        ).select_related('person')
        total = patients.count()

        refreshed = errors = 0
        for i, pr in enumerate(patients.iterator(), 1):
            try:
                refresh_patient_record(pr.person)
                infer_lot_for_person(pr.person)
                refreshed += 1
            except Exception as exc:
                errors += 1
                if errors <= 5:
                    self._print(f'  refresh failed person {pr.person_id}: {exc}', err=True)
            if i % 50 == 0 or i == total:
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed > 0 else 0
                eta = (total - i) / rate if rate > 0 else 0
                self._print(f'  {i}/{total} refreshed ({rate:.1f}/s, ETA {eta:.0f}s)')
                close_old_connections()

        self._print(f'Phase 5: {refreshed} refreshed, {errors} errors in {time.time()-t0:.1f}s')


# ======================================================================
# Utility functions
# ======================================================================

def _parse_date(s):
    """Parse a date string (YYYY-MM-DD or ISO datetime) to a date object."""
    if not s:
        return date.today()
    try:
        return datetime.strptime(s[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return date.today()


def _parse_datetime(s):
    """Parse a datetime string to a datetime object."""
    if not s:
        return datetime.now()
    try:
        # Try full ISO first
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        try:
            return datetime.strptime(s[:10], '%Y-%m-%d')
        except (ValueError, TypeError):
            return datetime.now()


def _extract_obs_value(obs):
    """Extract numeric value, string value, and unit from a FHIR Observation."""
    val_num = None
    val_str = None
    unit = None

    if 'valueQuantity' in obs:
        vq = obs['valueQuantity']
        raw = vq.get('value')
        if raw is not None:
            try:
                val_num = Decimal(str(raw))
            except (InvalidOperation, TypeError, ValueError):
                pass
        unit = vq.get('unit', '')
    elif 'valueInteger' in obs:
        try:
            val_num = Decimal(obs['valueInteger'])
        except (InvalidOperation, TypeError, ValueError):
            pass
    elif 'valueBoolean' in obs:
        val_num = Decimal(1) if obs['valueBoolean'] else Decimal(0)
    elif 'valueString' in obs:
        val_str = obs['valueString']
    elif 'valueCodeableConcept' in obs:
        vcc = obs['valueCodeableConcept']
        val_str = vcc.get('text', '')
        if not val_str:
            codings = vcc.get('coding', [])
            if codings:
                val_str = codings[0].get('display', codings[0].get('code', ''))

    return val_num, val_str, unit
