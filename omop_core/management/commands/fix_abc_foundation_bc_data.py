"""
Management command: fix_abc_foundation_bc_data

ABC Foundation patients are correctly tagged as ER+/HER2+ breast cancer but
have contaminated data:
  - First/second/later therapy lines contain MM regimens (VRd, KRd, Dara-Vd …)
  - stem_cell_transplant_history is set on ~56% of patients (MM-only concept)
  - ER/HER2/PR receptor status is missing on ~81% of patients
  - Other BC-specific diagnostics are sparsely or incorrectly populated

This command replaces all of that with plausible ER+/HER2+ breast cancer data.

Usage:
    python manage.py fix_abc_foundation_bc_data --dry-run
    python manage.py fix_abc_foundation_bc_data --confirm
    python manage.py fix_abc_foundation_bc_data --confirm --org-slug bbc-foundation
"""
import random
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from omop_core.models import Organization, PatientRecord

# ---------------------------------------------------------------------------
# Regimen catalogues  (ER+/HER2+ breast cancer)
# ---------------------------------------------------------------------------
# (name, weight)
_EARLY_REGIMENS = [
    ('AC-THP',  30),   # doxorubicin/cyclophosphamide → paclitaxel/trastuzumab/pertuzumab
    ('TCHP',    25),   # docetaxel/carboplatin/trastuzumab/pertuzumab
    ('THP',     18),   # paclitaxel/trastuzumab/pertuzumab
    ('AC-T',    12),   # doxorubicin/cyclophosphamide → paclitaxel
    ('TCH',     10),   # docetaxel/carboplatin/trastuzumab
    ('TC',       5),   # docetaxel/cyclophosphamide
]

_SECOND_LINE_REGIMENS = [
    ('T-DM1',                         35),  # ado-trastuzumab emtansine (Kadcyla)
    ('Tucatinib+Capecitabine+H',      20),  # tucatinib/capecitabine/trastuzumab (HER2CLIMB)
    ('Lapatinib+Capecitabine',        15),  # lapatinib/capecitabine
    ('THP',                           10),  # rechallenge
    ('Palbociclib+Letrozole+H',       10),  # CDK4/6 inhibitor + AI + HER2 agent
    ('Neratinib+Capecitabine',         5),
    ('Margetuximab+Chemotherapy',      5),
]

_LATER_REGIMENS = [
    ('T-DXd',                          35),  # trastuzumab deruxtecan (Enhertu)
    ('Tucatinib+Capecitabine+H',       20),
    ('T-DM1',                          15),  # re-use if not 2nd line
    ('Capecitabine+Trastuzumab',       10),
    ('Ribociclib+Exemestane+H',         8),
    ('Eribulin+Trastuzumab',            7),
    ('Atezolizumab+Nab-Paclitaxel',    5),
]

_OUTCOMES_PENULTIMATE = [
    ('Progressive Disease',       55),
    ('Stable Disease',            15),
    ('Partial Response',          20),
    ('Complete Response',         10),
]
_OUTCOMES_LAST = [
    ('Progressive Disease',       10),
    ('Stable Disease',            20),
    ('Partial Response',          40),
    ('Complete Response',         30),
]

# BC-specific intent / discontinuation
_INTENTS = ['Curative', 'Palliative', 'Adjuvant', 'Neoadjuvant']
_INTENT_WEIGHTS = [30, 35, 20, 15]

_DISCONTINUATION_REASONS = [
    'Disease progression', 'Toxicity', 'Patient preference',
    'Treatment completed', 'Clinical trial entry',
]

# Receptor status (ER+/HER2+ disease — ER and HER2 always positive)
_PR_STATUS = [('Positive', 65), ('Negative', 35)]
_NODES_STAGES = [
    ('N0: No regional lymph node metastasis',       35),
    ('N1: Micrometastasis or 1-3 axillary nodes',   30),
    ('N2: 4-9 axillary nodes',                      20),
    ('N3a: ≥10 axillary nodes (≥2 mm) or infraclavicular', 15),
]
_METASTASIS_STAGES = [('M0', 80), ('M1', 20)]
_METASTASIS_STATUS_MAP = {
    'M0': 'M0: No distant metastasis',
    'M1': 'M1: Distant metastasis present',
}

# MM-specific fields to clear
_MM_FIELDS_TO_CLEAR = [
    'stem_cell_transplant_history',
    'sct_date',
    'sct_eligibility',
    'plasma_cell_leukemia',
    'line_of_therapy',
]


def _weighted(pairs):
    items, weights = zip(*pairs)
    return random.choices(items, weights=weights, k=1)[0]


def _random_date(start: date, min_days: int, max_days: int) -> date:
    return start + timedelta(days=random.randint(min_days, max_days))


def _pick_regimen(catalogue):
    names, weights = zip(*catalogue)
    return random.choices(names, weights=weights, k=1)[0]


def _build_therapy_lines(stage: str) -> dict:
    """Return a dict of therapy-line fields for one patient."""
    today = date.today()
    advanced = stage in ('Stage III', 'Stage IV')

    # Diagnosis and first line
    dx_days_ago = random.randint(365, 365 * 4)
    dx_date = today - timedelta(days=dx_days_ago)

    n_lines = random.choices([1, 2, 3], weights=[35, 35, 30])[0]
    # Advanced patients more likely to progress
    if advanced:
        n_lines = random.choices([1, 2, 3], weights=[20, 35, 45])[0]

    # --- Line 1 ---
    l1_name = _pick_regimen(_EARLY_REGIMENS)
    l1_start = _random_date(dx_date, 14, 60)
    l1_duration = random.randint(84, 210)  # 3–7 months
    l1_end = l1_start + timedelta(days=l1_duration)
    l1_outcome = _weighted(_OUTCOMES_PENULTIMATE) if n_lines > 1 else _weighted(_OUTCOMES_LAST)
    l1_intent = _weighted(list(zip(_INTENTS, _INTENT_WEIGHTS)))
    l1_discont = _weighted([(r, 1) for r in _DISCONTINUATION_REASONS]) if n_lines > 1 else 'Treatment completed'

    fields = dict(
        first_line_therapy=l1_name,
        first_line_start_date=l1_start,
        first_line_end_date=l1_end if n_lines > 1 else None,
        first_line_outcome=l1_outcome,
        first_line_intent=l1_intent,
        first_line_discontinuation_reason=l1_discont,
        therapy_lines_count=n_lines,
        # clear second/later by default
        second_line_therapy=None,
        second_line_start_date=None,
        second_line_end_date=None,
        second_line_outcome=None,
        second_line_intent=None,
        second_line_discontinuation_reason=None,
        later_therapy=None,
        later_start_date=None,
        later_end_date=None,
        later_outcome=None,
        later_intent=None,
        later_discontinuation_reason=None,
    )

    if n_lines < 2:
        return fields

    # --- Line 2 ---
    l2_start = _random_date(l1_end, 14, 60)
    l2_duration = random.randint(63, 180)
    l2_end = l2_start + timedelta(days=l2_duration)
    l2_name = _pick_regimen(_SECOND_LINE_REGIMENS)
    l2_outcome = _weighted(_OUTCOMES_PENULTIMATE) if n_lines > 2 else _weighted(_OUTCOMES_LAST)
    l2_intent = 'Palliative'
    l2_discont = _weighted([(r, 1) for r in _DISCONTINUATION_REASONS]) if n_lines > 2 else 'Treatment completed'

    fields.update(
        second_line_therapy=l2_name,
        second_line_start_date=l2_start,
        second_line_end_date=l2_end if n_lines > 2 else None,
        second_line_outcome=l2_outcome,
        second_line_intent=l2_intent,
        second_line_discontinuation_reason=l2_discont,
    )

    if n_lines < 3:
        return fields

    # --- Line 3 (later) ---
    l3_start = _random_date(l2_end, 14, 60)
    l3_name = _pick_regimen(_LATER_REGIMENS)
    # avoid same as line 2
    if l3_name == l2_name:
        l3_name = _pick_regimen(_LATER_REGIMENS)
    l3_outcome = _weighted(_OUTCOMES_LAST)
    l3_intent = 'Palliative'

    fields.update(
        later_therapy=l3_name,
        later_start_date=l3_start,
        later_end_date=None,
        later_outcome=l3_outcome,
        later_intent=l3_intent,
        later_discontinuation_reason=None,
    )

    return fields


def _build_bc_diagnostics(record: PatientRecord) -> dict:
    """Return BC-specific diagnostic field values."""
    m_stage = _weighted(_METASTASIS_STAGES)
    pr = _weighted(_PR_STATUS)
    return dict(
        # Receptor status — always ER+/HER2+ for this disease type
        estrogen_receptor_status='Positive',
        her2_status='Positive',
        progesterone_receptor_status=pr,
        tnbc_status=False,
        androgen_receptor_status=random.choice(['Positive', 'Negative']),
        # Staging
        nodes_stage=_weighted(_NODES_STAGES),
        distant_metastasis_stage=m_stage,
        metastasis_status=_METASTASIS_STATUS_MAP[m_stage],
        # Ensure disease fields are correct
        disease='ER|ERBB2 Breast cancer',
        disease_slug='breast-cancer',
    )


class Command(BaseCommand):
    help = 'Replace contaminated MM data on ABC Foundation BC patients with plausible ER+/HER2+ breast cancer data'

    def add_arguments(self, parser):
        parser.add_argument(
            '--org-slug', default='abc-foundation',
            help='Organization slug to fix (default: abc-foundation)',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print what would change without writing to the DB',
        )
        parser.add_argument(
            '--confirm', action='store_true',
            help='Actually write changes to the DB',
        )

    def handle(self, *args, **options):
        if not options['dry_run'] and not options['confirm']:
            self.stderr.write('Pass --dry-run to preview or --confirm to apply.')
            return

        try:
            org = Organization.objects.get(slug=options['org_slug'])
        except Organization.DoesNotExist:
            self.stderr.write(f"Organization '{options['org_slug']}' not found.")
            return

        records = list(PatientRecord.objects.filter(organization=org))
        self.stdout.write(f"Found {len(records)} PatientRecord(s) for {org.name}")

        if options['dry_run']:
            self.stdout.write('[DRY RUN] No changes will be written.')
            sample = records[:3]
            for r in sample:
                stage = r.stage or 'Stage II'
                therapy = _build_therapy_lines(stage)
                diag = _build_bc_diagnostics(r)
                self.stdout.write(
                    f"  person_id={r.person_id}  stage={stage}"
                    f"  L1={therapy['first_line_therapy']}"
                    f"  L2={therapy['second_line_therapy']}"
                    f"  L3={therapy['later_therapy']}"
                    f"  ER={diag['estrogen_receptor_status']}"
                    f"  HER2={diag['her2_status']}"
                )
            return

        # --- Apply ---
        from django.db import transaction

        count = 0
        with transaction.atomic():
            for rec in records:
                stage = rec.stage or 'Stage II'
                therapy = _build_therapy_lines(stage)
                diag = _build_bc_diagnostics(rec)

                mm_clear = dict(
                    stem_cell_transplant_history=[],
                    sct_date=None,
                    sct_eligibility=[],
                    plasma_cell_leukemia=None,
                    line_of_therapy=None,
                )

                updates = {**therapy, **diag, **mm_clear}
                PatientRecord.objects.filter(pk=rec.pk).update(**updates)
                count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Updated {count} patients with plausible ER+/HER2+ BC therapy and diagnostics."
            )
        )
