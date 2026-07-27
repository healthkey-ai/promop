"""
One-off patch: populate FHIR-derived PatientRecord fields (weight, height,
cytogenic_markers, behaviorals, etc.) for patients already imported via
bulk_import_fhir_bundle before Phase 5b existed.

Re-reads the original FHIR bundle, extracts extension + observation values,
and patches existing PatientRecords matched by (given_name, family_name).

Usage (on Render shell or locally):
    DATABASE_URL=... python manage.py patch_fhir_fields \
        --file /tmp/synthea_mm_1000.json --org synthea-mm

    # Dry-run first:
    DATABASE_URL=... python manage.py patch_fhir_fields \
        --file /tmp/synthea_mm_1000.json --org synthea-mm --dry-run
"""

import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Patch FHIR-derived fields onto existing PatientRecords'

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True, help='FHIR Bundle JSON file')
        parser.add_argument('--org', dest='org_slug', required=True,
                            help='Org slug to filter PatientRecords')
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be patched without writing')

    def _print(self, msg, err=False):
        stream = self.stderr if err else self.stdout
        stream.write(msg)
        sys.stdout.flush()
        sys.stderr.flush()

    def handle(self, *args, **options):
        from omop_core.models import Organization, PatientRecord, Person

        # Load bundle
        bundle_path = Path(options['file'])
        if not bundle_path.exists():
            raise CommandError(f'File not found: {bundle_path}')
        self._print(f'Loading {bundle_path}…')
        with open(bundle_path) as f:
            bundle = json.load(f)

        # Verify org
        try:
            org = Organization.objects.get(slug=options['org_slug'])
        except Organization.DoesNotExist:
            raise CommandError(f"Org '{options['org_slug']}' not found")

        # Group entries by patient
        patient_groups = []
        current = []
        for entry in bundle.get('entry', []):
            rt = entry.get('resource', {}).get('resourceType')
            if rt == 'Patient' and current:
                patient_groups.append(current)
                current = []
            current.append(entry)
        if current:
            patient_groups.append(current)

        self._print(f'{len(patient_groups)} patients in bundle')

        # Build name → patch dict
        patches = {}  # (given, family) → patch dict
        for group in patient_groups:
            patient_res = None
            observation_list = []
            for entry in group:
                res = entry.get('resource', {})
                rt = res.get('resourceType')
                if rt == 'Patient':
                    patient_res = res
                elif rt == 'Observation':
                    observation_list.append(res)

            if not patient_res:
                continue

            names = patient_res.get('name', [{}])
            given = ' '.join(names[0].get('given', []))
            family = names[0].get('family', '')
            if not given or not family:
                continue

            patch = {}

            # --- Patient extensions ---
            patch.update(self._extract_extensions(patient_res))

            # --- Date of birth ---
            bd = patient_res.get('birthDate', '')
            if bd:
                try:
                    patch['date_of_birth'] = datetime.strptime(bd, '%Y-%m-%d').date()
                except ValueError:
                    pass

            # --- Observation-derived fields ---
            patch.update(self._extract_obs_fields(observation_list))

            if patch:
                patches[(given, family)] = patch

        self._print(f'{len(patches)} patients have patch data')

        # Match to existing PatientRecords
        prs = PatientRecord.objects.filter(
            organization=org
        ).select_related('person')

        matched = 0
        skipped = 0
        errors = 0
        t0 = time.time()

        for pr in prs:
            person = pr.person
            key = (person.given_name or '', person.family_name or '')
            if key not in patches:
                skipped += 1
                continue

            patch = patches[key]
            if options['dry_run']:
                if matched < 5:
                    self._print(f'  [DRY RUN] {key[0]} {key[1]}: {list(patch.keys())}')
                matched += 1
                continue

            try:
                for field, value in patch.items():
                    setattr(pr, field, value)
                pr.save(update_fields=list(patch.keys()))
                matched += 1
            except Exception as exc:
                errors += 1
                if errors <= 5:
                    self._print(f'  Error {key}: {exc}', err=True)

        elapsed = time.time() - t0
        action = 'would patch' if options['dry_run'] else 'patched'
        self._print(f'Done: {matched} {action}, {skipped} no match, '
                     f'{errors} errors in {elapsed:.1f}s')

    def _extract_extensions(self, patient_res):
        """Extract all HealthKey extensions from Patient resource."""
        patch = {}
        base = 'https://healthkey.ai/fhir/StructureDefinition/'
        _EXT_MAP = {
            f'{base}bodyWeight': ('weight', lambda e: e.get('valueQuantity', {}).get('value')),
            f'{base}bodyHeight': ('height', lambda e: e.get('valueQuantity', {}).get('value')),
            f'{base}systolic-bp': ('systolic_blood_pressure', lambda e: e.get('valueQuantity', {}).get('value')),
            f'{base}diastolic-bp': ('diastolic_blood_pressure', lambda e: e.get('valueQuantity', {}).get('value')),
            f'{base}heartRate': ('heartrate', lambda e: e.get('valueQuantity', {}).get('value')),
            f'{base}ecog-performance-status': ('ecog_performance_status', lambda e: e.get('valueInteger')),
            f'{base}mm-cytogenetic-markers': ('cytogenic_markers', lambda e: e.get('valueString')),
            f'{base}mm-measurable-disease-imwg': ('measurable_disease_imwg', lambda e: e.get('valueBoolean')),
            f'{base}mm-sct-date': ('_sct_date_str', lambda e: e.get('valueString')),
            f'{base}mm-sct-history': ('_sct_history_str', lambda e: e.get('valueString')),
            f'{base}mm-sct-eligibility': ('_sct_eligibility_str', lambda e: e.get('valueString')),
        }

        for ext in patient_res.get('extension', []):
            url = ext.get('url', '')
            if url in _EXT_MAP:
                field, parser = _EXT_MAP[url]
                val = parser(ext)
                if val is not None:
                    patch[field] = val

        # Post-process weight/height units
        if 'weight' in patch:
            patch['weight_units'] = 'kg'
        if 'height' in patch:
            patch['height_units'] = 'cm'

        # Post-process SCT fields
        sct_date_str = patch.pop('_sct_date_str', None)
        if sct_date_str:
            try:
                parsed = datetime.strptime(sct_date_str, '%Y-%m-%d').date()
                if parsed <= date.today():
                    patch['sct_date'] = parsed
            except ValueError:
                pass

        sct_hist = patch.pop('_sct_history_str', None)
        if sct_hist:
            patch['stem_cell_transplant_history'] = [
                t.strip() for t in sct_hist.split(',') if t.strip()
            ]

        sct_elig = patch.pop('_sct_eligibility_str', None)
        if sct_elig:
            patch['sct_eligibility'] = [
                t.strip() for t in sct_elig.split(',') if t.strip()
            ]

        return patch

    def _extract_obs_fields(self, observation_list):
        """Extract observation-derived PatientRecord fields."""
        patch = {}
        for obs in observation_list:
            loinc_code = None
            for coding in obs.get('code', {}).get('coding', []):
                if coding.get('system', '') == 'http://loinc.org':
                    loinc_code = coding.get('code')
                    break
            if not loinc_code:
                continue

            value_number = None
            value_codeable = None
            if obs.get('valueQuantity'):
                value_number = obs['valueQuantity'].get('value')
            if obs.get('valueCodeableConcept'):
                value_codeable = obs['valueCodeableConcept'].get('text')
            elif obs.get('valueString'):
                value_codeable = obs['valueString']
            if obs.get('valueInteger') is not None:
                value_number = obs['valueInteger']

            # Behavioral: Lifestyle
            if loinc_code == '72166-2':
                patch['smoking_status'] = (value_codeable[:50] if value_codeable else value_codeable)
            elif loinc_code == '63640-7':
                patch['pack_years'] = value_number
            elif loinc_code == '74013-4':
                patch['alcohol_use'] = (value_codeable[:50] if value_codeable else value_codeable)
            elif loinc_code == '11286-7':
                patch['drinks_per_week'] = value_number
            elif loinc_code == '68516-4':
                patch['exercise_frequency'] = (value_codeable[:50] if value_codeable else value_codeable)
            elif loinc_code == '89555-7':
                patch['exercise_minutes_per_week'] = value_number
            elif loinc_code == '88365-2':
                patch['diet_type'] = value_codeable
            # Sleep & Wellbeing
            elif loinc_code == '93832-4':
                patch['sleep_hours_per_night'] = value_number
            elif loinc_code == '93831-6':
                patch['sleep_quality'] = (value_codeable[:50] if value_codeable else value_codeable)
            elif loinc_code == '73985-4':
                patch['stress_level'] = (value_codeable[:50] if value_codeable else value_codeable)
            elif loinc_code == '93033-9':
                patch['social_support'] = (value_codeable[:50] if value_codeable else value_codeable)
            # Socioeconomic
            elif loinc_code == '74165-2':
                patch['employment_status'] = (value_codeable[:50] if value_codeable else value_codeable)
            elif loinc_code == '82589-3':
                patch['education_level'] = value_codeable
            elif loinc_code == '45404-1':
                patch['marital_status'] = (value_codeable[:50] if value_codeable else value_codeable)
            elif loinc_code == '76513-1':
                patch['insurance_type'] = value_codeable
            elif loinc_code == '63512-8':
                patch['number_of_dependents'] = value_number
            elif loinc_code == '77243-3':
                patch['annual_household_income'] = value_number
            # Cancer Assessment
            elif loinc_code == '89247-1':
                if obs.get('effectiveDateTime'):
                    patch['ecog_assessment_date'] = obs['effectiveDateTime'][:10]
            elif loinc_code == '85337-4':
                patch['test_methodology'] = (value_codeable[:50] if value_codeable else value_codeable)
                if value_number is not None:
                    patch['oncotype_dx_score'] = value_number
            elif loinc_code == '31208-2':
                patch['test_specimen_type'] = (value_codeable[:50] if value_codeable else value_codeable)
                if obs.get('effectiveDateTime'):
                    patch['test_date'] = obs['effectiveDateTime'][:10]
            elif loinc_code == '69548-6':
                patch['report_interpretation'] = (value_codeable[:50] if value_codeable else value_codeable)
            # Labs not derived by refresh
            elif loinc_code == '2532-0':
                patch['ldh'] = value_number
            elif loinc_code == '6768-6':
                patch['alkaline_phosphatase'] = value_number
            elif loinc_code == '19123-9':
                patch['magnesium'] = value_number
            elif loinc_code == '1968-7':
                patch['serum_bilirubin_level_direct'] = value_number
            elif loinc_code == '6301-6':
                patch['inr'] = value_number
            elif loinc_code == '5902-2':
                patch['pt_seconds'] = value_number
            elif loinc_code == '3173-2':
                patch['ptt_seconds'] = value_number
            elif loinc_code == '2039-6':
                patch['cea_ng_ml'] = value_number
            elif loinc_code == '25390-6':
                patch['ca19_9_u_ml'] = value_number
            elif loinc_code == '2857-1':
                patch['psa_ng_ml'] = value_number

        return {k: v for k, v in patch.items() if v is not None}
