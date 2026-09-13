"""
Management command: populate_genomics_sample_data

Seeds plausible genetic variant data onto patients using save_variant() so that
OMOP Measurement/Observation rows, PatientRecord projections, and the
GenomicsTab all populate correctly.

Usage:
    DATABASE_URL="..." python manage.py populate_genomics_sample_data [--count 5] [--dry-run]
"""
import json
import random
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from omop_core.models import Concept, FieldConceptMapping, PatientRecord
from omop_core.services.genomics import (
    _measurement_event_concepts, delete_variant, list_variants, save_variant,
)
from omop_core.services.genomics_catalog import disease_code


# ── Variant pools by disease code ────────────────────────────────────────────

_HGVS_VARIANTS = {
    'brca1':  ['c.68_69delAG', 'c.5266dupC', 'c.181T>G'],
    'brca2':  ['c.5946delT'],
    'pik3ca': ['c.3140A>G', 'c.1633G>A'],
    'tp53':   ['c.743G>A', 'c.818G>A'],
    'esr1':   ['c.1610A>G', 'c.1613A>G'],
    'palb1':  ['c.3113G>A', 'c.509_510delGA'],
    'kras':   ['c.35G>T', 'c.35G>A', 'c.34G>T'],
    'nras':   ['c.181C>A', 'c.182A>G'],
    'braf':   ['c.1799T>A'],
    'ccnd1':  ['c.870G>A', 'c.723G>A'],
    'notch1': ['c.7541_7542delCT', 'c.7544T>C'],
    'notch2': ['c.7189C>T', 'c.6898G>A'],
    'atm':    ['c.8545C>T', 'c.5557G>A'],
    'sf3b1':  ['c.2098A>G', 'c.1997A>G', 'c.1874G>A'],
    'bcl2':   ['c.455T>C'],
    'ezh2':   ['c.1936T>C', 'c.2044G>A'],
    'kmt2d':  ['c.8390delG', 'c.13882C>T'],
    'crebbp': ['c.4414A>G', 'c.4406T>G'],
}

_AMINO_ACID_CHANGES = {
    'brca1':  ['p.Glu23ValfsTer17', 'p.Gln1756ProfsTer74', 'p.Cys61Gly'],
    'brca2':  ['p.Ser1982ArgfsTer22'],
    'pik3ca': ['p.His1047Arg', 'p.Glu545Lys'],
    'tp53':   ['p.Arg248Gln', 'p.Arg273His'],
    'esr1':   ['p.Tyr537Cys', 'p.Asp538Gly'],
    'kras':   ['p.Gly12Val', 'p.Gly12Asp', 'p.Gly12Cys'],
    'nras':   ['p.Gln61Lys', 'p.Gln61Arg'],
    'braf':   ['p.Val600Glu'],
}

_DISEASE_POOLS = {
    'BC': [
        {'marker_key': 'brca1',  'gene': 'BRCA1',  'kind': 'gene', 'prevalence': 0.15},
        {'marker_key': 'brca2',  'gene': 'BRCA2',  'kind': 'gene', 'prevalence': 0.15},
        {'marker_key': 'pik3ca', 'gene': 'PIK3CA', 'kind': 'gene', 'prevalence': 0.35},
        {'marker_key': 'tp53',   'gene': 'TP53',   'kind': 'gene', 'prevalence': 0.30},
        {'marker_key': 'esr1',   'gene': 'ESR1',   'kind': 'gene', 'prevalence': 0.20},
        {'marker_key': 'palb1',  'gene': 'PALB1',  'kind': 'gene', 'prevalence': 0.08},
    ],
    'MM': [
        {'marker_key': 'kras',         'gene': 'KRAS',           'kind': 'gene',        'prevalence': 0.25},
        {'marker_key': 'nras',         'gene': 'NRAS',           'kind': 'gene',        'prevalence': 0.20},
        {'marker_key': 'braf',         'gene': 'BRAF',           'kind': 'gene',        'prevalence': 0.05},
        {'marker_key': 'tp53',         'gene': 'TP53',           'kind': 'gene',        'prevalence': 0.10},
        {'marker_key': 'del17p',       'gene': 'TP53',           'kind': 'abnormality', 'prevalence': 0.10},
        {'marker_key': 't414',         'gene': 'FGFR3/NSD2/IGH','kind': 'abnormality', 'prevalence': 0.15},
        {'marker_key': 't1114',        'gene': 'CCND1/IGH',     'kind': 'abnormality', 'prevalence': 0.20},
        {'marker_key': 't1416',        'gene': 'IGH/MAF',       'kind': 'abnormality', 'prevalence': 0.05},
        {'marker_key': 'gain1q',       'gene': '1q21',          'kind': 'abnormality', 'prevalence': 0.40},
        {'marker_key': 'hyperdiploidy','gene': 'Chromosomal',   'kind': 'abnormality', 'prevalence': 0.45},
    ],
    'FL': [
        {'marker_key': 'bcl2',   'gene': 'BCL2',   'kind': 'gene',        'prevalence': 0.85},
        {'marker_key': 'ezh2',   'gene': 'EZH2',   'kind': 'gene',        'prevalence': 0.25},
        {'marker_key': 'kmt2d',  'gene': 'KMT2D',  'kind': 'gene',        'prevalence': 0.70},
        {'marker_key': 'crebbp', 'gene': 'CREBBP',  'kind': 'gene',        'prevalence': 0.65},
        {'marker_key': 'bcl6',   'gene': 'BCL6',   'kind': 'abnormality', 'prevalence': 0.30},
    ],
    'MCL': [
        {'marker_key': 'ccnd1',             'gene': 'CCND1',       'kind': 'gene',        'prevalence': 0.30},
        {'marker_key': 'tp53',              'gene': 'TP53',        'kind': 'gene',        'prevalence': 0.15},
        {'marker_key': 'notch1',            'gene': 'NOTCH1',      'kind': 'gene',        'prevalence': 0.10},
        {'marker_key': 'notch2',            'gene': 'NOTCH2',      'kind': 'gene',        'prevalence': 0.08},
        {'marker_key': 'atm',              'gene': 'ATM',         'kind': 'gene',        'prevalence': 0.40},
        {'marker_key': 'del17p',           'gene': 'TP53',        'kind': 'abnormality', 'prevalence': 0.20},
        {'marker_key': 't1114',            'gene': 'CCND1/IGH',   'kind': 'abnormality', 'prevalence': 0.90},
        {'marker_key': 'complex_karyotype','gene': 'Chromosomal', 'kind': 'abnormality', 'prevalence': 0.25},
    ],
    'CLL': [
        {'marker_key': 'tp53',      'gene': 'TP53',  'kind': 'gene',        'prevalence': 0.10},
        {'marker_key': 'sf3b1',     'gene': 'SF3B1', 'kind': 'gene',        'prevalence': 0.15},
        {'marker_key': 'atm',       'gene': 'ATM',   'kind': 'gene',        'prevalence': 0.12},
        {'marker_key': 'notch1',    'gene': 'NOTCH1','kind': 'gene',        'prevalence': 0.12},
        {'marker_key': 'del17p',    'gene': 'TP53',  'kind': 'abnormality', 'prevalence': 0.10},
        {'marker_key': 'del11q',    'gene': '11q',   'kind': 'abnormality', 'prevalence': 0.18},
        {'marker_key': 'del13q',    'gene': '13q',   'kind': 'abnormality', 'prevalence': 0.55},
        {'marker_key': 'trisomy12', 'gene': '12',    'kind': 'abnormality', 'prevalence': 0.15},
    ],
}

# Abnormality label lookup (from the catalog).
_ABNORMALITY_LABELS = {
    'del17p': 'del(17p)', 't414': 't(4;14)', 't1114': 't(11;14)',
    't1416': 't(14;16)', 'gain1q': '1q21 gain / amplification',
    'hyperdiploidy': 'Hyperdiploidy', 'bcl6': 'BCL6 rearrangement',
    'complex_karyotype': 'Complex karyotype', 'del11q': 'del(11q)',
    'del13q': 'del(13q)', 'trisomy12': 'Trisomy 12',
}

_SLUG_MAP = {
    'BC': 'breast-cancer', 'MM': 'multiple-myeloma',
    'FL': 'follicular-lymphoma', 'MCL': 'mantle-cell-lymphoma',
    'CLL': 'chronic-lymphocytic-leukemia',
}
_SLUG_TO_CODE = {v: k for k, v in _SLUG_MAP.items()}

# Reference annotations for the paired examples above. Coordinates are only
# supplied where checked; do not invent a genomic position from a c.HGVS.
# See docs/genomics.md for reference sources and the legacy PALB1 limitation.
_GENE_ANNOTATIONS = {
    'brca1': ('17', '17q21.31', 'NM_007294.4', 'NC_000017.11'),
    'brca2': ('13', '13q13.1', 'NM_000059.4', 'NC_000013.11'),
    'pik3ca': ('3', '3q26.32', 'NM_006218.4', 'NC_000003.12'),
    'tp53': ('17', '17p13.1', 'NM_000546.6', 'NC_000017.11'),
    'esr1': ('6', '6q25.1', 'NM_000125.4', 'NC_000006.12'),
    'kras': ('12', '12p12.1', 'NM_004985.5', 'NC_000012.12'),
    'nras': ('1', '1p13.2', 'NM_002524.5', 'NC_000001.11'),
    'braf': ('7', '7q34', 'NM_004333.6', 'NC_000007.14'),
}
_GENOMIC_CHANGES = {
    ('braf', 'c.1799T>A'): 'NC_000007.14:g.140753336A>T',
    ('tp53', 'c.743G>A'): 'NC_000017.11:g.7674220C>T',
}
_ABNORMALITY_DETAILS = {
    'del17p': ('17', '17p13.1', 'FISH'),
    't414': ('4;14', '4p16.3;14q32.33', 'FISH'),
    't1114': ('11;14', '11q13.3;14q32.33', 'FISH'),
    't1416': ('14;16', '14q32.33;16q23.2', 'FISH'),
    'gain1q': ('1', '1q21', 'FISH'),
    'bcl6': ('3', '3q27.3', 'FISH'),
    'del11q': ('11', '11q22.3', 'FISH'),
    'del13q': ('13', '13q14', 'FISH'),
    'trisomy12': ('12', '', 'FISH'),
    'hyperdiploidy': ('', '', 'Karyotyping'),
    'complex_karyotype': ('', '', 'Karyotyping'),
}


def _random_test_date():
    """Random date within the last 2 years."""
    days_ago = random.randint(1, 730)
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _report_payload(entry, *, person_id=None, disease=None, origin='Somatic'):
    """Synthetic report context, with collection preceding test/report dates."""
    test_date = _random_test_date()
    identifier = f'SAMPLE-{person_id or "DEMO"}-{entry["marker_key"]}-{test_date}'
    specimen = ('Peripheral blood' if origin == 'Germline' else
                'Breast tumour tissue' if disease == 'BC' else
                'Lymph node tissue' if disease in ('FL', 'MCL') else
                'Peripheral blood' if disease == 'CLL' else 'Bone marrow aspirate')
    return {
        'gene': entry['gene'],
        'marker_key': entry['marker_key'],
        'origin': origin,
        'genomic_source_class': origin,
        'test_date': test_date,
        'collection_date': (date.fromisoformat(test_date) - timedelta(days=7)).isoformat(),
        'interpretation_date': test_date,
        'specimen_id': identifier + '-SP',
        'specimen_type': specimen,
        'report_id': identifier + '-RPT',
        'laboratory': 'Synthetic demonstration laboratory',
        'classification_framework': 'Synthetic demonstration classification; not clinically assessed',
        'evidence_source': 'Synthetic sample data; not a patient laboratory result',
    }


def _build_gene_payload(entry, *, person_id=None, disease=None):
    """Build coherent sequence annotations and complete synthetic report context."""
    key = entry['marker_key']
    hgvs = _HGVS_VARIANTS[key]
    index = random.randrange(len(hgvs))
    variant = hgvs[index]
    origin = 'Germline' if key in ('brca1', 'brca2') else 'Somatic'
    payload = _report_payload(entry, person_id=person_id, disease=disease, origin=origin)
    payload.update({
        'variant': variant,
        'transcript_dna_change': variant,
        'variant_name': f'{entry["gene"]} {variant}',
        'variant_description': (
            f'Synthetic {entry["gene"]} sequence finding for demonstration. '
            'Annotations are sample fixtures, not an interpretation of patient sequencing.'
        ),
        'interpretation': 'Uncertain',
        'status': 'present',
        'assessment': 'present',
        'genome_assembly': 'GRCh38',
        'variant_category': 'Simple variant',
        'variant_analysis_method_type': 'Next generation sequencing',
        'zygosity': 'Heterozygous' if origin == 'Germline' else 'Unknown',
        'allelic_frequency': Decimal(str(round(random.uniform(
            40.0 if origin == 'Germline' else 5.0,
            60.0 if origin == 'Germline' else 75.0,
        ), 1))),
        'allelic_frequency_unit': '%',
        'coverage_depth': random.randint(250, 1500),
    })
    aa = _AMINO_ACID_CHANGES.get(key)
    if aa:
        # DNA and protein describe the same example, never independent draws.
        protein = aa[index]
        payload['amino_acid_change'] = protein
        payload['amino_acid_change_type'] = 'Frameshift' if 'fs' in protein else 'Missense'
    annotation = _GENE_ANNOTATIONS.get(key)
    if annotation:
        payload.update(zip((
            'chromosome', 'cytogenetic_location', 'transcript_reference_sequence_id',
            'genomic_reference_sequence_id',
        ), annotation))
    if (key, variant) in _GENOMIC_CHANGES:
        payload['genomic_dna_change'] = _GENOMIC_CHANGES[key, variant]
    if key == 'palb1':
        payload['variant_description'] += ' PALB1 is an unresolved legacy catalog label; no reference annotation assigned.'
    return payload


def _build_abnormality_payload(entry, *, person_id=None, disease=None):
    """Cytogenetic context; clone fraction is distinct from sequence VAF."""
    key = entry['marker_key']
    label = _ABNORMALITY_LABELS[key]
    chromosome, location, method = _ABNORMALITY_DETAILS[key]
    status = random.choice(['present', 'absent'])
    payload = _report_payload(entry, person_id=person_id, disease=disease)
    payload.update({
        'variant_name': label,
        'variant_description': f'Synthetic cytogenetic report: {label} {status}. Not a patient laboratory result.',
        'status': status,
        'assessment': status,
        'interpretation': 'Uncertain',
        'variant_category': 'Structural variant',
        'variant_analysis_method_type': method,
    })
    if chromosome:
        payload['chromosome'] = chromosome
    if location:
        payload['cytogenetic_location'] = location
    if method == 'FISH':
        payload['clone_fraction'] = random.randint(10, 90) if status == 'present' else 0
        payload['clone_fraction_unit'] = '%'
    return payload


def _select_markers(pool, min_count=1, max_count=6):
    """Select markers by prevalence, guaranteeing at least min_count."""
    selected = [entry for entry in pool if random.random() < entry['prevalence']]
    if len(selected) < min_count:
        remaining = [e for e in pool if e not in selected]
        random.shuffle(remaining)
        selected.extend(remaining[:min_count - len(selected)])
    return selected[:max_count]


class Command(BaseCommand):
    help = 'Seed detailed synthetic genomic findings and laboratory report context onto patients'

    def add_arguments(self, parser):
        parser.add_argument('--org', type=str, help='Filter patients by organization slug')
        parser.add_argument('--count', type=int, help='Seed exactly N patients (default: 10%% of eligible)')
        parser.add_argument('--patient', type=str, help='Seed a single patient (person_id or email)')
        parser.add_argument('--disease', type=str, help='Filter by disease code (BC, MM, FL, MCL, CLL)')
        parser.add_argument('--all', action='store_true', help='Seed all eligible patients')
        parser.add_argument('--overwrite', action='store_true', help='Delete existing variants before re-seeding')
        parser.add_argument('--dry-run', action='store_true', help='Preview what would be seeded, no writes')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        overwrite = options['overwrite']
        if options['count'] is not None and options['count'] < 1:
            raise CommandError('--count must be a positive integer.')

        # Precondition: required OMOP concepts and genomics field mappings must exist.
        if not dry_run:
            missing = []
            if not Concept.objects.filter(pk=0).exists():
                missing.append('Concept(pk=0)')
            if not Concept.objects.filter(pk=32817).exists():
                missing.append('Concept(pk=32817)')
            if not _measurement_event_concepts().filter(invalid_reason__isnull=True).exists():
                missing.append("CDM concept 'measurement.measurement_id'")
            if missing:
                raise CommandError(f'Required OMOP concepts missing: {", ".join(missing)}. Load vocabularies first.')
            genomics_mappings = FieldConceptMapping.objects.filter(
                field_name__startswith='genomics_', status='approved',
            ).count()
            if genomics_mappings == 0:
                raise CommandError(
                    'No approved genomics FieldConceptMapping rows found. '
                    'Run migrations (0224_seed_genomics_mappings) first.'
                )

        # Build queryset.
        qs = PatientRecord.objects.select_related('person').exclude(
            disease_slug__isnull=True,
        ).exclude(disease_slug='')

        if options['org']:
            qs = qs.filter(organization__slug=options['org'])

        if options['disease']:
            code = disease_code(options['disease'])
            if code is None:
                raise CommandError(f'Unknown disease code: {options["disease"]}')
            qs = qs.filter(disease_slug=_SLUG_MAP[code])

        if options['patient']:
            val = options['patient']
            try:
                pid = int(val)
                qs = qs.filter(person_id=pid)
            except ValueError:
                qs = qs.filter(email=val)

        # Filter to patients whose disease_slug maps to a known pool.
        qs = qs.filter(disease_slug__in=_SLUG_MAP.values())

        if not overwrite:
            qs = qs.filter(genetic_mutations=[])

        qs = qs.order_by('person_id')

        total_eligible = qs.count()
        if total_eligible == 0:
            if options['patient']:
                self._diagnose_patient(options['patient'], overwrite)
            self.stdout.write('No eligible patients found.')
            return

        # Determine count.
        if options['patient']:
            count = total_eligible
        elif options['all']:
            count = total_eligible
        elif options['count'] is not None:
            count = min(options['count'], total_eligible)
        else:
            count = max(1, total_eligible // 10)

        patients = list(qs[:count])
        self.stdout.write(f'Seeding {len(patients)} of {total_eligible} eligible patients'
                          f'{" (dry run)" if dry_run else ""}')

        # Phase 1: Write all OMOP variant data (skip per-variant refresh).
        seeded_persons = []
        seeded = 0
        total_variants = 0
        failed = 0
        for pr in patients:
            code = _SLUG_TO_CODE.get(pr.disease_slug)
            if code is None:
                continue
            pool = _DISEASE_POOLS.get(code)
            if not pool:
                continue

            markers = _select_markers(pool)
            payloads = [
                (_build_gene_payload if entry['kind'] == 'gene' else _build_abnormality_payload)(
                    entry, person_id=pr.person_id, disease=code,
                )
                for entry in markers
            ]
            if dry_run:
                marker_names = ', '.join(m['marker_key'] for m in markers)
                self.stdout.write(f'  [DRY RUN] person_id={pr.person_id} ({code}): '
                                  f'{len(markers)} variants — {marker_names}')
                if options['verbosity'] >= 2:
                    for payload in payloads:
                        self.stdout.write(json.dumps(payload, default=str, sort_keys=True))
                seeded += 1
                total_variants += len(markers)
                continue

            try:
                with transaction.atomic():
                    if overwrite:
                        existing = list_variants(pr.person)
                        for v in existing:
                            delete_variant(pr.person, v['id'], skip_refresh=True)

                    for payload in payloads:
                        save_variant(pr.person, payload, skip_refresh=True)
            except Exception as e:
                self.stderr.write(f'  ERROR person_id={pr.person_id}: {e}')
                failed += 1
                continue

            total_variants += len(payloads)
            seeded_persons.append(pr.person)
            seeded += 1
            self.stdout.write(f'  person_id={pr.person_id} ({code}): {len(markers)} variants written')

        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f'Done. Would seed {total_variants} variants across {seeded} patients.'
            ))
            return

        # Phase 2: Refresh PatientRecord once per patient.
        self.stdout.write(f'Refreshing {len(seeded_persons)} patient records...')
        from omop_core.services.patient_record_service import refresh_patient_record
        for i, person in enumerate(seeded_persons, 1):
            refresh_patient_record(person)
            self.stdout.write(f'  refreshed {i}/{len(seeded_persons)} (person_id={person.person_id})')

        if failed:
            raise CommandError(
                f'{failed} patient(s) failed; seeded {total_variants} variants across {seeded} patients.'
            )

        self.stdout.write(self.style.SUCCESS(
            f'Done. Seeded {total_variants} variants across {seeded} patients.'
        ))

    def _diagnose_patient(self, val, overwrite):
        """Provide a specific error when --patient targets an ineligible patient."""
        try:
            pid = int(val)
            pr = PatientRecord.objects.filter(person_id=pid).first()
        except ValueError:
            pr = PatientRecord.objects.filter(email=val).first()
        if pr is None:
            raise CommandError(f'Patient not found: {val}')
        if not pr.disease_slug:
            raise CommandError(f'Patient {val} has no disease_slug set.')
        if pr.disease_slug not in _SLUG_MAP.values():
            raise CommandError(f'Patient {val} has unsupported disease: {pr.disease_slug}')
        if not overwrite and pr.genetic_mutations:
            raise CommandError(f'Patient {val} already has variants. Use --overwrite to replace.')
