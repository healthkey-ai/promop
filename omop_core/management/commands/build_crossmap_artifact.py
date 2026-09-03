"""Build the consolidated code-to-concept mapping artifact.

Reads mapping data from multiple provenance sources and produces a single
JSON artifact (+ reviewable Markdown) that ``load_mappings`` loads at deploy
time.

Sources:
  - HealthTree One / Next (HT-One) — CPT4→SNOMED, ICD10→SNOMED,
    SNOMED→RxNorm, MedDRA→SNOMED
  - HK-Labs — uncoded lab text → LOINC
  - Apple HealthKit / Garmin Connect wearable device types → LOINC / HK-Wearable
"""
import json
import re
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


PROJECTS = {
    'one': ('HT-One', Path.home() / 'one'),
    'next': ('HT-Next', Path.home() / 'healthtree-platform'),
}
FHIR_ROOT = Path('functions/main/firestore/apps/curehub')
HKLABS_ROOT = Path.home() / 'hk-labs' / 'backend' / 'apps' / 'labs'

DEFAULT_ARTIFACT = Path(__file__).resolve().parents[2] / 'data' / 'code_concept_mappings.json'
DEFAULT_MARKDOWN = Path(__file__).resolve().parents[3] / 'docs' / 'code-concept-mappings.md'

# Lab catalog abbreviation → LOINC code (same mapping hk-labs uses).
_CATALOG_LOINC = {
    'wbc': '6690-2', 'hgb': '718-7', 'plt': '777-3', 'anc': '751-8',
    'alc': '731-0', 'hct': '4544-3', 'mcv': '787-2',
    'creatinine': '2160-0', 'egfr': '62238-1', 'crcl': '2164-2',
    'calcium': '17861-6', 'bun': '3094-0', 'ast': '1920-8', 'alt': '1742-6',
    'alp': '6768-6', 'bili_total': '1975-2', 'bili_direct': '1968-7',
    'albumin': '1751-7', 'ldh': '2532-0',
    'mspike_serum': '33358-3', 'mspike_urine': '34366-5',
    'flc_kappa': '36916-5', 'flc_lambda': '33944-0', 'flc_ratio': '48378-4',
    'bmpc': '26450-7', 'b2m': '1952-1',
    'ca_15_3': '6875-9', 'ca_27_29': '17842-6', 'ki67': '85337-4',
    'lvef': '10230-1', 'hba1c': '4548-4',
    'ldl': '13457-7', 'hdl': '2085-9', 'tsh': '3016-3',
    'hiv_ab': '75622-1', 'hbsag': '5195-3', 'hcv_ab': '16128-1',
}

# Apple HealthKit metrics: (source_code, display_name, unit, domain, dest_vocab, dest_code)
APPLE_METRICS = [
    ('HKQuantityTypeIdentifierStepCount', 'Step count', 'count', 'Observation', 'LOINC', '55423-8'),
    ('HKQuantityTypeIdentifierAppleExerciseTime', 'Exercise time (active minutes)', 'min', 'Observation', 'LOINC', '55411-3'),
    ('HKQuantityTypeIdentifierRestingHeartRate', 'Resting heart rate', 'bpm', 'Measurement', 'LOINC', '40443-4'),
    ('HKQuantityTypeIdentifierHeartRateVariabilitySDNN', 'Heart rate variability (SDNN)', 'ms', 'Measurement', 'LOINC', '80404-7'),
    ('HKQuantityTypeIdentifierOxygenSaturation', 'Oxygen saturation (SpO2)', '%', 'Measurement', 'LOINC', '59408-5'),
    ('HKQuantityTypeIdentifierRespiratoryRate', 'Respiratory rate', 'breaths/min', 'Measurement', 'LOINC', '9279-1'),
    ('HKQuantityTypeIdentifierVO2Max', 'VO2 max', 'mL/kg/min', 'Measurement', 'LOINC', '94122-9'),
    ('HKQuantityTypeIdentifierDistanceWalkingRunning', 'Walking + running distance', 'km', 'Measurement', 'LOINC', '41953-1'),
    ('HKQuantityTypeIdentifierWalkingSpeed', 'Walking speed', 'km/hr', 'Measurement', 'LOINC', '41957-2'),
    ('HKQuantityTypeIdentifierWalkingStepLength', 'Walking step length', 'cm', 'Measurement', 'HK-Wearable', 'HK-WEAR-STEP-LENGTH'),
    ('HKQuantityTypeIdentifierWalkingDoubleSupportPercentage', 'Walking double support percentage', '%', 'Measurement', 'HK-Wearable', 'HK-WEAR-DBL-SUPPORT'),
    ('HKQuantityTypeIdentifierWalkingHeartRateAverage', 'Walking heart rate average', 'bpm', 'Measurement', 'HK-Wearable', 'HK-WEAR-WALK-HR'),
    ('HKQuantityTypeIdentifierFlightsClimbed', 'Flights of stairs climbed', 'count', 'Observation', 'LOINC', '100304-5'),
    ('HKQuantityTypeIdentifierActiveEnergyBurned', 'Active energy burned', 'kcal', 'Measurement', 'LOINC', '93819-1'),
    ('HKQuantityTypeIdentifierBasalEnergyBurned', 'Basal energy expenditure', 'kcal', 'Measurement', 'HK-Wearable', 'HK-WEAR-BASAL-ENERGY'),
    ('HKQuantityTypeIdentifierBodyMass', 'Body weight', 'kg', 'Measurement', 'LOINC', '29463-7'),
    ('HKCategoryTypeIdentifierSleepAnalysis', 'Sleep duration', 'h', 'Observation', 'LOINC', '93832-4'),
]

# Garmin Connect metrics: (source_code, display_name, unit, domain, dest_vocab, dest_code)
GARMIN_METRICS = [
    ('steps', 'Step count', 'count', 'Observation', 'LOINC', '55423-8'),
    ('active_minutes', 'Active minutes', 'min', 'Observation', 'LOINC', '55411-3'),
    ('resting_hr', 'Resting heart rate', 'bpm', 'Measurement', 'LOINC', '40443-4'),
    ('hrv_rmssd', 'Heart rate variability (RMSSD)', 'ms', 'Measurement', 'HK-Wearable', 'HK-WEAR-HRV-RMSSD'),
    ('spo2', 'Oxygen saturation (SpO2)', '%', 'Measurement', 'LOINC', '59408-5'),
    ('respiratory_rate', 'Respiratory rate', 'breaths/min', 'Measurement', 'LOINC', '9279-1'),
    ('sleep_duration', 'Sleep duration', 'h', 'Observation', 'LOINC', '93832-4'),
    ('vo2_max', 'VO2 max', 'mL/kg/min', 'Measurement', 'LOINC', '94122-9'),
    ('distance', 'Walking + running distance', 'km', 'Measurement', 'LOINC', '41953-1'),
    ('active_energy', 'Active energy burned', 'kcal', 'Measurement', 'LOINC', '93819-1'),
    ('basal_energy', 'Basal energy expenditure', 'kcal', 'Measurement', 'HK-Wearable', 'HK-WEAR-BASAL-ENERGY'),
]


def _normalize(text):
    """Lowercase, collapse whitespace, strip punctuation for matching."""
    return re.sub(r'[^a-z0-9 ]', '', text.lower()).strip()


class Command(BaseCommand):
    help = 'Build a consolidated code-to-concept mapping artifact from all provenance sources.'

    def add_arguments(self, parser):
        parser.add_argument('--one-root', help='Path to the HealthTree One repository.')
        parser.add_argument('--next-root', help='Path to the HealthTree Next repository.')
        parser.add_argument('--hklabs-root', default=str(HKLABS_ROOT),
                            help='Path to hk-labs/backend/apps/labs/ directory.')
        parser.add_argument('--output', default=str(DEFAULT_ARTIFACT),
                            help='Artifact JSON output path.')
        parser.add_argument('--markdown-output', default=str(DEFAULT_MARKDOWN),
                            help='Reviewable Markdown output path.')

    def handle(self, **options):
        roots = {
            'one': Path(options['one_root']) if options['one_root'] else PROJECTS['one'][1],
            'next': Path(options['next_root']) if options['next_root'] else PROJECTS['next'][1],
        }
        hklabs_root = Path(options['hklabs_root'])

        # candidates[source_key][target_key] = {count, origins}
        candidates = defaultdict(lambda: defaultdict(lambda: {'count': 0, 'origins': set()}))
        source_metadata = {}

        # --- HT-One / HT-Next ---
        for project, root in roots.items():
            origin = PROJECTS[project][0]
            if not root.is_dir():
                self.stderr.write(self.style.WARNING(
                    f'HealthTree {project} repository not found: {root} — skipping.'
                ))
                continue
            for row in self._read_project(root / FHIR_ROOT):
                self._add_candidate(candidates, source_metadata, row, origin)

        # --- HK-Labs ---
        hklabs_count = 0
        for row in self._read_hklabs(hklabs_root):
            self._add_candidate(candidates, source_metadata, row, 'HK-Labs')
            hklabs_count += 1
        if hklabs_count:
            self.stdout.write(f'Read {hklabs_count} HK-Labs mappings.')

        # --- Apple / Garmin wearables ---
        wearable_count = 0
        for row in self._read_wearables():
            self._add_candidate(candidates, source_metadata, row, row.pop('_origin'))
            wearable_count += 1
        if wearable_count:
            self.stdout.write(f'Read {wearable_count} wearable device mappings.')

        if not candidates:
            raise CommandError('No mappings found from any source.')

        # --- Rank and select ---
        mappings = []
        for key in sorted(candidates):
            ranked = sorted(
                candidates[key].items(),
                key=lambda item: (-item[1]['count'], -len(item[1]['origins']), item[0]),
            )
            selected, selected_meta = ranked[0]
            metadata = source_metadata[key]
            ambiguous = len(ranked) > 1
            mappings.append({
                'source_vocabulary_id': key[0],
                'source_code': key[1],
                'source_code_description': metadata['source_code_description'],
                'source_concept_id': metadata['source_concept_id'],
                'target_vocabulary_id': selected[0],
                'target_concept_code': selected[1],
                'domain_id': selected[2],
                'status': 'proposed' if ambiguous else 'approved',
                'origins': sorted(metadata['origins']),
                'selection_reason': self._selection_reason(selected_meta, len(ranked)),
                'candidates': [
                    {
                        'target_vocabulary_id': target[0],
                        'target_concept_code': target[1],
                        'domain_id': target[2],
                        'occurrences': candidate['count'],
                        'origins': sorted(candidate['origins']),
                    }
                    for target, candidate in ranked
                ],
            })

        artifact = {
            'schema_version': 1,
            'selection_policy': (
                'For HT-One mappings, select the target with the greatest occurrence count '
                'across HealthTree One and Next; break ties by contributing-project count, '
                'then target vocabulary/code/domain. HK-Labs and wearable mappings are '
                'curated one-to-one and always approved.'
            ),
            'mappings': mappings,
        }
        output = Path(options['output'])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + '\n')
        markdown_output = Path(options['markdown_output'])
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
        self._write_markdown(markdown_output, artifact)

        approved = sum(1 for row in mappings if row['status'] == 'approved')
        proposed = sum(1 for row in mappings if row['status'] == 'proposed')
        self.stdout.write(self.style.SUCCESS(
            f'Wrote {len(mappings):,} mappings ({approved:,} approved, {proposed:,} proposed) '
            f'to {output} and {markdown_output}.'
        ))

    # ------------------------------------------------------------------
    # Candidate accumulation
    # ------------------------------------------------------------------

    @staticmethod
    def _add_candidate(candidates, source_metadata, row, origin):
        key = (row['source_vocabulary_id'], row['source_code'])
        target_key = (row['target_vocabulary_id'], row['target_concept_code'], row['domain_id'])
        candidate = candidates[key][target_key]
        candidate['count'] += 1
        candidate['origins'].add(origin)
        metadata = source_metadata.setdefault(key, {
            'source_code_description': row['source_code_description'],
            'source_concept_id': row['source_concept_id'],
            'origins': set(),
        })
        if not metadata['source_code_description'] and row['source_code_description']:
            metadata['source_code_description'] = row['source_code_description']
        if metadata['source_concept_id'] is None and row['source_concept_id'] is not None:
            metadata['source_concept_id'] = row['source_concept_id']
        metadata['origins'].add(origin)

    # ------------------------------------------------------------------
    # HT-One / HT-Next reader
    # ------------------------------------------------------------------

    def _read_project(self, root):
        for code, targets in self._load_json(root / 'FHIR/resourcesTypes/r4/Condition/_icd10ToSnomedMappings.json').items():
            for target in targets or []:
                yield self._row('ICD10', code, 'SNOMED', target, 'Condition')
        for entry in self._load_json(root / 'FHIR/codeSystems/cptToSnomedMap.json').values():
            if entry.get('snomedId') and entry['snomedId'] != '0':
                yield self._row('CPT4', entry.get('cptCode', ''), 'SNOMED', entry['snomedId'], 'Procedure', entry.get('cptDescriptor', ''), entry.get('cptConceptId'))
        for source, target in self._load_json(root / 'FHIR/codeSystems/snomedToRxNormMap.json').items():
            yield self._row('SNOMED', source, 'RxNorm', target, 'Drug')
        for entry in self._load_json(root / 'medicalResources/_DocumentReferenceAI/linesOfTherapy/adverseEvents/_utils/MDRToSnomed.json'):
            if entry.get('mdr_code') and entry.get('snomed_code'):
                yield self._row('MedDRA', entry['mdr_code'], 'SNOMED', entry['snomed_code'], 'Condition', entry.get('mdr_name', ''))

    # ------------------------------------------------------------------
    # HK-Labs reader
    # ------------------------------------------------------------------

    def _read_hklabs(self, root):
        """Read HK-Labs mapping data from the three JSON sources."""
        seen = set()  # dedup by normalized source_code

        # 1. loinc_common.json — LOINC short names
        loinc_path = root / 'data' / 'loinc_common.json'
        if loinc_path.exists():
            data = json.loads(loinc_path.read_text())
            for entry in data.get('codes', []):
                short_name = entry.get('loinc_short_name', '')
                loinc_code = entry.get('loinc_code', '')
                unit = entry.get('loinc_default_unit', '')
                normalized = _normalize(short_name)[:100]
                if not normalized or not loinc_code or normalized in seen:
                    continue
                seen.add(normalized)
                desc = f'{short_name} ({unit})' if unit else short_name
                yield self._row('', normalized, 'LOINC', loinc_code, 'Measurement', desc)
        else:
            self.stderr.write(self.style.WARNING(f'Not found: {loinc_path}'))

        # 2. lab_catalog.json — catalog display names via _CATALOG_LOINC
        catalog_path = root / 'fixtures' / 'lab_catalog.json'
        if catalog_path.exists():
            data = json.loads(catalog_path.read_text())
            for item in data:
                fields = item.get('fields', item)
                abbrev = fields.get('abbreviation', '')
                loinc_code = _CATALOG_LOINC.get(abbrev)
                if not loinc_code:
                    continue
                name_normalized = fields.get('name_normalized', '')
                normalized = _normalize(name_normalized)[:100]
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                yield self._row('', normalized, 'LOINC', loinc_code, 'Measurement', fields.get('name', name_normalized))
        else:
            self.stderr.write(self.style.WARNING(f'Not found: {catalog_path}'))

        # 3. curated_aliases_manual.json — curated alias text → LOINC
        aliases_path = root / 'fixtures' / 'curated_aliases_manual.json'
        if aliases_path.exists():
            for entry in json.loads(aliases_path.read_text()):
                alias = entry.get('alias', '')
                loinc_code = entry.get('loinc_num', '')
                normalized = _normalize(alias)[:100]
                if not normalized or not loinc_code or normalized in seen:
                    continue
                seen.add(normalized)
                yield self._row('', normalized, 'LOINC', loinc_code, 'Measurement', alias)
        else:
            self.stderr.write(self.style.WARNING(f'Not found: {aliases_path}'))

    # ------------------------------------------------------------------
    # Wearable reader
    # ------------------------------------------------------------------

    @staticmethod
    def _read_wearables():
        """Yield rows for Apple HealthKit and Garmin Connect device metrics."""
        for src_code, display, unit, domain, d_vocab, d_code in APPLE_METRICS:
            desc = f'{display} ({unit})' if unit else display
            row = Command._row('Apple', src_code, d_vocab, d_code, domain, desc)
            row['_origin'] = 'HK-Wearable-Apple'
            yield row
        for src_code, display, unit, domain, d_vocab, d_code in GARMIN_METRICS:
            desc = f'{display} ({unit})' if unit else display
            row = Command._row('Garmin', src_code, d_vocab, d_code, domain, desc)
            row['_origin'] = 'HK-Wearable-Garmin'
            yield row

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_markdown(path, artifact):
        lines = [
            '# Code-to-Concept Mapping Artifact',
            '',
            'Generated by `manage.py build_crossmap_artifact`; do not edit by hand.',
            '',
            f"Selection policy: {artifact['selection_policy']}",
            '',
            '| Source system | Source code | Target OMOP vocabulary | Target OMOP code | Domain | Status | Origins | Candidate targets |',
            '| --- | --- | --- | --- | --- | --- | --- | --- |',
        ]
        for row in artifact['mappings']:
            def cell(value):
                return str(value or '').replace('|', '\\|').replace('\n', ' ')
            lines.append('| ' + ' | '.join([
                cell(row['source_vocabulary_id']), cell(row['source_code']),
                cell(row['target_vocabulary_id']), cell(row['target_concept_code']),
                cell(row['domain_id']), cell(row['status']), ', '.join(row['origins']),
                str(len(row['candidates'])),
            ]) + ' |')
        path.write_text('\n'.join(lines) + '\n')

    @staticmethod
    def _selection_reason(selected_meta, candidate_count):
        if candidate_count == 1:
            return 'Only target observed; approved.'
        return (
            f"Selected from {selected_meta['count']} occurrence(s) across "
            f"{len(selected_meta['origins'])} source(s), ahead of {candidate_count - 1} alternative target(s)."
        )

    @staticmethod
    def _load_json(path):
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f'Cannot read {path}: {exc}') from exc

    @staticmethod
    def _row(source_vocab, source_code, target_vocab, target_code, domain, description='', source_id=None):
        return {
            'source_vocabulary_id': source_vocab,
            'source_code': str(source_code)[:100],
            'source_code_description': str(description or '')[:255],
            'source_concept_id': int(source_id) if source_id and str(source_id).isdigit() else None,
            'target_vocabulary_id': target_vocab,
            'target_concept_code': str(target_code),
            'domain_id': domain,
        }
