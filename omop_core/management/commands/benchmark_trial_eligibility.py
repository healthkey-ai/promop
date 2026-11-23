"""
Management command: benchmark_trial_eligibility

Benchmarks a 20-criterion trial eligibility pull from raw OMOP tables versus
the same 20 fields materialized on PatientRecord.

This is narrower than benchmark_patient_record.py: it does not re-derive the
full PatientRecord projection. Instead it measures the query shape that trial
matching cares about most:

    raw OMOP tables -> one patient eligibility row
    PatientRecord    -> the same row from a flat projection

The command times the two reads and counts populated fields; it does not
compare values. The raw row follows the derivation's row selection for stage,
the receptors and the coded labs, and is known to differ from the derived
record elsewhere (#2708):

- age is the birth-year difference;
- disease is the newest name-matched condition, not the primary cancer;
- ECOG and Karnofsky are name-matched Observations, entered-in-error included;
- lab codes are resolved once per run, numeric labs other than the unit pairs
  are read as stored, and AST, ALT and clearance rows named only by display
  text are not read;
- a row hidden by a later clear still counts, and stage has no uncoded
  fallback (FHIR Condition.stage, sample-patient stage);
- the record scopes facts to the primary cancer and applies curated and
  pending values, which no raw-table read sees.

Usage:
    python manage.py benchmark_trial_eligibility --org-slugs synthea-mm --limit 10
    python manage.py benchmark_trial_eligibility --person-ids 123,456
    python manage.py benchmark_trial_eligibility --output results.json
"""
import json
import statistics
import time
from datetime import date
from types import SimpleNamespace

from django.core.management.base import BaseCommand, CommandError
from django.db.models import (
    BigIntegerField,
    BooleanField,
    Case,
    CharField,
    DecimalField,
    F,
    Func,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce, JSONObject, Lower, NullIf

from omop_core.models import ConditionOccurrence, Measurement, Observation, PatientRecord, Person
from omop_core.services.chemistry_units import (
    PAIRS as CHEMISTRY_PAIRS,
    canonical_from_pair as chemistry_canonical_from_pair,
    canonical_in_range as chemistry_canonical_in_range,
    normalize_unit as normalize_chemistry_unit,
)
from omop_core.services.canonical_units import value_in_source_unit
from omop_core.services.clinical_units import blood_count_to_canonical, count_unit
from omop_core.services.creatinine_units import (
    DEFAULT_UNIT as CREATININE_DEFAULT_UNIT,
    canonical_from_pair as creatinine_canonical_from_pair,
    canonical_in_range as creatinine_canonical_in_range,
    normalize_unit as normalize_creatinine_unit,
)
from omop_core.services.hemoglobin_units import (
    DEFAULT_UNIT as HEMOGLOBIN_DEFAULT_UNIT,
    canonical_from_pair as hemoglobin_canonical_from_pair,
    canonical_in_range as hemoglobin_canonical_in_range,
    normalize_unit as normalize_hemoglobin_unit,
)
from omop_core.services.patient_record_service import (
    STAGE_BOOLEAN_ANSWERS,
    _LOINC_CODE_RE,
    _resolve_reported_unit,
    resolve_lab_code_fields,
)
from omop_core.services.stage_labels import normalise_stage_label


TRIAL_ELIGIBILITY_FIELDS = [
    'patient_age',
    'gender',
    'disease',
    'stage',
    'ecog_performance_status',
    'karnofsky_performance_score',
    'hemoglobin_g_dl',
    'platelet_count_thousand_per_ul',
    'anc_thousand_per_ul',
    'wbc_count_thousand_per_ul',
    'serum_creatinine_mg_dl',
    'creatinine_clearance_ml_min',
    'serum_calcium_mg_dl',
    'bilirubin_total_mg_dl',
    'ast_u_l',
    'alt_u_l',
    'albumin_g_dl',
    'her2_status',
    'estrogen_receptor_status',
    'progesterone_receptor_status',
]

_CANCER_NAME_FILTERS = (
    Q(condition_concept__concept_name__icontains='cancer')
    | Q(condition_concept__concept_name__icontains='neoplasm')
    | Q(condition_concept__concept_name__icontains='malignant')
    | Q(condition_concept__concept_name__icontains='lymphoma')
    | Q(condition_concept__concept_name__icontains='leukemia')
    | Q(condition_concept__concept_name__icontains='myeloma')
    | Q(condition_concept__concept_name__icontains='carcinoma')
    | Q(condition_concept__concept_name__icontains='sarcoma')
    | Q(condition_concept__concept_name__icontains='tumor')
)


def _stats(samples_sec):
    if not samples_sec:
        return {}
    ordered = sorted(samples_sec)
    p95_index = max(0, int(len(ordered) * 0.95) - 1)
    return {
        'n': len(ordered),
        'mean_ms': round(statistics.mean(ordered) * 1000, 3),
        'median_ms': round(statistics.median(ordered) * 1000, 3),
        'p95_ms': round(ordered[p95_index] * 1000, 3),
        'min_ms': round(ordered[0] * 1000, 3),
        'max_ms': round(ordered[-1] * 1000, 3),
    }


def _cohort_person_ids(org_slugs, person_ids_arg, limit):
    if person_ids_arg:
        return [int(x) for x in person_ids_arg.split(',') if x.strip()]

    qs = (
        PatientRecord.objects
        .filter(organization__slug__in=org_slugs)
        .order_by('person_id')
        .values_list('person_id', flat=True)
    )
    return list(qs[:limit]) if limit else list(qs)


def _effective_code():
    """The row's named code, with `_measurement_code`'s precedence.

    A LOINC-shaped concept code wins; otherwise the source value, else the
    concept code. Matching either column read a row whose concept names a
    different analyte through its source value (#2475/#2708).
    """
    return Case(
        When(measurement_concept__concept_code__regex=_LOINC_CODE_PATTERN,
             then=F('measurement_concept__concept_code')),
        default=Coalesce(
            NullIf(F('measurement_source_value'), Value('')),
            F('measurement_concept__concept_code')),
        output_field=CharField(),
    )


def _non_empty(field):
    return Q(**{f'{field}__isnull': False}) & ~Q(**{field: ''})


def _python_strip(expression):
    """``str.strip()`` in SQL: ``TRIM`` alone drops spaces only."""
    return Func(expression, Value(r'^\s+|\s+$'), Value(''), Value('g'),
                function='REGEXP_REPLACE', output_field=CharField())


def _stage_row_text():
    """`_stage_row_value`'s answer for one row, in SQL.

    The first non-empty of the string, the answer concept's name and the
    number truncated to an integer, then stripped. The field is chosen before
    the row is judged, so a whitespace-only string rejects the row even when an
    answer concept is present, as in the extractor. ``value_source_value`` is
    not an answer there, so it is not one here.
    """
    chosen = Case(
        When(_non_empty('value_as_string'), then=F('value_as_string')),
        When(_non_empty('value_as_concept__concept_name'),
             then=F('value_as_concept__concept_name')),
        When(value_as_number__isnull=False, then=Cast(
            Cast(Func(F('value_as_number'), function='TRUNC'), BigIntegerField()),
            CharField())),
        output_field=CharField(),
    )
    return _python_strip(chosen)


_STAGE_TABLES = {
    Measurement: ('measurement_concept__concept_code', 'measurement_source_value',
                  '-measurement_date', '-measurement_id'),
    Observation: ('observation_concept__concept_code', 'observation_source_value',
                  '-observation_date', '-observation_id'),
}


def _stage_subquery(model, code):
    """The stage `_stage_value` reads from one table for one code.

    The extractor walks the concept-coded rows, newest first, before the
    source-only rows, and stops at the first row with a usable answer: one
    that is not empty and not a boolean RECIST result (#2720 review). Its
    snapshot holds neither entered-in-error nor provisional rows.

    Not mirrored (#2708): a row hidden by a later clear in the snapshot's
    cleared history still counts here.
    """
    concept, source, *ordering = _STAGE_TABLES[model]
    qs = (
        model.objects
        .filter(person=OuterRef('pk'), is_erroneous=False, confidence__isnull=True)
        .filter(Q(**{concept: code}) | Q(**{source: code}))
        .annotate(
            _concept_match=Case(When(**{concept: code}, then=Value(0)), default=Value(1)),
            stage_text=_stage_row_text(),
        )
        .annotate(_stage_key=Lower('stage_text'))
        .filter(stage_text__gt='')
        .exclude(_stage_key__in=sorted(STAGE_BOOLEAN_ANSWERS))
        .order_by('_concept_match', *ordering)
        .values('stage_text')[:1]
    )
    return Subquery(qs, output_field=CharField())


def _stage_expression():
    """`_get_staging_data`'s coded overall stage: R-ISS before ISS, and within
    each code Measurement before Observation.

    Not mirrored (#2708): the extractor's uncoded fallbacks (a FHIR
    Condition.stage assertion, the sample-patient stage) when no coded row
    answers.
    """
    return Coalesce(*(
        _stage_subquery(model, code)
        for code in ('21908-9-riss', '21908-9')
        for model in (Measurement, Observation)
    ))


def _receptor_answer_subquery(concept_code):
    """The answer `_receptor_status` reads off `_first_by_code`'s row.

    The row is the newest real Measurement whose *concept* code is the
    receptor's; a source value alone does not name it. The newest row wins even
    when it holds no answer, as in the extractor. Its answer is the concept
    name, then the string, then the source value, each skipped when empty
    (#2720 review).
    """
    qs = (
        Measurement.objects
        .filter(person=OuterRef('pk'), is_erroneous=False, confidence__isnull=True,
                measurement_concept__concept_code=concept_code)
        .order_by('-measurement_date', '-measurement_id')
        .annotate(raw_text=Coalesce(
            NullIf(F('value_as_concept__concept_name'), Value('')),
            NullIf(F('value_as_string'), Value('')),
            NullIf(F('value_source_value'), Value('')),
            output_field=CharField(),
        ))
        .values('raw_text')[:1]
    )
    return Subquery(qs, output_field=CharField())


def _latest_measurement_number_subquery(concept_codes, output_field):
    qs = (
        Measurement.objects
        .filter(person=OuterRef('pk'), is_erroneous=False)
        .annotate(effective_code=_effective_code())
        .filter(effective_code__in=concept_codes)
        .exclude(value_as_number__isnull=True)
        .order_by('-measurement_date', '-measurement_id')
        .values('value_as_number')[:1]
    )
    return Subquery(qs, output_field=output_field)


def _latest_blood_count_subquery(concept_codes):
    """Keep the source unit alongside the newest count, including unknowns.

    The selection follows the derivation's blood-count projection, which takes
    the newest row across every code routed to the field and converts its unit.
    WBC keeps its single code: its derivation path converts ``6690-2`` only.
    """
    return _latest_measurement_pair_subquery(concept_codes)


#: A code that names the row's measurement, as ``_measurement_code`` decides it:
#: a LOINC-shaped concept code wins over the source value, so a row that carries
#: a different LOINC concept is not read through a LOINC-shaped source value.
#: The derivation's own pattern, so the two dialects cannot drift.
_LOINC_CODE_PATTERN = _LOINC_CODE_RE.pattern


def _codes_for(code_fields, field):
    """The LOINC codes the derivation routes to *field*.

    The curation tables are the authority (#1796): reading the benchmark's own
    hardcoded list would miss an alias a curator approved, and a row whose
    concept is that alias would silently drop out of the benchmark while the
    derivation still reads it (#2475).

    Resolved once per run without measurements, so the derivation's
    "no LOINC lab on this person means no curation" fallback is approximated
    here; #2708 tracks it.
    """
    return tuple(sorted(
        code for code, (target, _cast) in code_fields.items() if target == field))


def _latest_measurement_pair_subquery(concept_codes):
    """The newest measurement with its value, source unit and UCUM unit.

    The derivation reads a value in its canonical unit, so a benchmark that
    compared the raw ``value_as_number`` would report 221 umol/L creatinine as
    221 mg/dL (#2438 class). The payload carries the unit to convert with.

    The row is selected by its effective code, the same precedence
    ``_measurement_code`` uses: a LOINC-shaped concept code names the
    measurement, and only a concept that is not LOINC-shaped defers to the
    source value. Matching either column, as this query did before #2475, read a
    row whose concept is 59260-0 (moles/volume) through a 718-7 source value as
    g/dL hemoglobin.

    The number builders use the same effective code. The text fields follow
    their own extractors instead: stage reads concept or source value, the
    receptors read the concept code only. This does not make the benchmark row
    equal the derived record; the module docstring lists what still differs
    (#2708).
    """
    return Subquery(
        Measurement.objects.filter(person=OuterRef('pk'), is_erroneous=False)
        .annotate(effective_code=_effective_code())
        .filter(effective_code__in=concept_codes)
        .exclude(value_as_number__isnull=True)
        .order_by('-measurement_date', '-measurement_id').annotate(
            measurement_payload=JSONObject(
                value='value_as_number',
                unit='unit_source_value',
                ucum=Case(When(unit_concept__vocabulary_id='UCUM', then='unit_concept__concept_code')),
                concept_id='unit_concept_id',
                concept_code='unit_concept__concept_code',
                concept_vocabulary='unit_concept__vocabulary_id',
                source_value='value_source_value',
            ),
        ).values('measurement_payload')[:1]
    )


def _reading(payload):
    """The measurement shape ``canonical_units`` and ``_resolve_reported_unit`` read."""
    concept_id = payload.get('concept_id')
    return SimpleNamespace(
        value_as_number=payload['value'],
        unit_source_value=payload['unit'],
        unit_concept_id=concept_id,
        unit_concept=(SimpleNamespace(
            vocabulary_id=payload.get('concept_vocabulary'),
            concept_code=payload.get('concept_code'),
        ) if concept_id is not None else None),
        value_source_value=payload.get('source_value'),
    )


def _canonical_pair(payload, normalize, default_unit, from_pair, in_range):
    """A measurement payload in its canonical unit, or None (no evidence).

    Read exactly as the derivation does: first put the number back in
    ``unit_source_value``'s unit (a row converted on write holds it in
    ``unit_concept``'s, #2249), then resolve the reported unit and convert.
    A unit that cannot be proven is no evidence, not a number to guess, and so
    is a canonical the destination column cannot hold (``in_range``, the same
    predicate the derivation applies after conversion, #2475).
    """
    if not payload:
        return None
    reading = _reading(payload)
    sent = value_in_source_unit(reading)
    if sent is None:
        return None
    _reported, code = _resolve_reported_unit(reading, normalize, default_unit)
    if code is None:
        return None
    recognised, canonical = from_pair(sent, code)
    if not recognised or not in_range(canonical):
        return None
    return canonical


def _canonical_blood_count(payload):
    """A count in thousands/µL, read the way the derivation does.

    A converted row holds the stored count in ``unit_concept``'s unit and the
    sent one in ``value_source_value``; ``count_unit`` then names the source
    unit. Without a proven sent value the count is no evidence.
    """
    if not payload:
        return None
    sent = value_in_source_unit(_reading(payload))
    if sent is None:
        return None
    return blood_count_to_canonical(sent, count_unit(payload['unit'], payload['ucum']))


def _latest_observation_number_subquery(concept_codes, output_field):
    qs = (
        Observation.objects
        .filter(person=OuterRef('pk'))
        .filter(
            Q(observation_concept__concept_code__in=concept_codes)
            | Q(observation_source_value__in=concept_codes)
        )
        .exclude(value_as_number__isnull=True)
        .order_by('-observation_date', '-observation_id')
        .values('value_as_number')[:1]
    )
    return Subquery(qs, output_field=output_field)


def _latest_observation_number_name_subquery(name_terms, output_field):
    name_filter = Q()
    for term in name_terms:
        name_filter |= Q(observation_concept__concept_name__icontains=term)

    qs = (
        Observation.objects
        .filter(person=OuterRef('pk'))
        .filter(name_filter)
        .exclude(value_as_number__isnull=True)
        .order_by('-observation_date', '-observation_id')
        .values('value_as_number')[:1]
    )
    return Subquery(qs, output_field=output_field)


def _latest_cancer_condition_subquery():
    qs = (
        ConditionOccurrence.objects
        .filter(person=OuterRef('pk'))
        .filter(_CANCER_NAME_FILTERS)
        .order_by('-condition_start_date', '-condition_occurrence_id')
        .values('condition_concept__concept_name')[:1]
    )
    return Subquery(qs, output_field=CharField())


def _normalize_gender(raw):
    if not raw:
        return None
    value = str(raw).lower()
    if 'female' in value:
        return 'F'
    if 'male' in value and 'female' not in value:
        return 'M'
    return 'U'


def _normalize_receptor_status(raw):
    if not raw:
        return None
    value = str(raw).lower()
    if 'positive' in value:
        return 'Positive'
    if 'negative' in value:
        return 'Negative'
    if 'equivocal' in value:
        return 'Equivocal'
    return str(raw).strip().title() or None


def _normalize_disease(raw):
    if not raw:
        return None
    value = str(raw).strip()
    if value.lower() == 'myeloma':
        return 'multiple myeloma'
    return value


def _fetch_patient_record_trial_row(person_id):
    return (
        PatientRecord.objects
        .filter(person_id=person_id)
        .values(*TRIAL_ELIGIBILITY_FIELDS)
        .get()
    )


def _fetch_omop_trial_row(person_id, code_fields=None):
    code_fields = code_fields or resolve_lab_code_fields()
    today = date.today()
    qs = (
        Person.objects
        .filter(person_id=person_id)
        .annotate(
            patient_age=Value(today.year, output_field=IntegerField()) - F('year_of_birth'),
            gender_raw=Coalesce('gender_concept__concept_name', 'gender_source_value'),
            disease=_latest_cancer_condition_subquery(),
            stage=_stage_expression(),
            ecog_performance_status=_latest_observation_number_name_subquery(['ecog'], IntegerField()),
            karnofsky_performance_score=_latest_observation_number_name_subquery(['karnofsky'], IntegerField()),
            # Not 59260-0: it is moles/volume, the canonical field does not read
            # it, and a unitless one in the g/dL default is a false anemia.
            hemoglobin_g_dl=_latest_measurement_pair_subquery(
                _codes_for(code_fields, 'hemoglobin_g_dl')),
            platelet_count_thousand_per_ul=_latest_blood_count_subquery(
                _codes_for(code_fields, 'platelet_count_thousand_per_ul')),
            anc_thousand_per_ul=_latest_blood_count_subquery(
                _codes_for(code_fields, 'anc_thousand_per_ul')),
            wbc_count_thousand_per_ul=_latest_blood_count_subquery(['6690-2']),
            serum_creatinine_mg_dl=_latest_measurement_pair_subquery(
                _codes_for(code_fields, 'serum_creatinine_mg_dl')),
            creatinine_clearance_ml_min=_latest_measurement_number_subquery(
                _codes_for(code_fields, 'creatinine_clearance_ml_min'),
                DecimalField(max_digits=6, decimal_places=1)),
            serum_calcium_mg_dl=_latest_measurement_pair_subquery(
                _codes_for(code_fields, 'serum_calcium_mg_dl')),
            bilirubin_total_mg_dl=_latest_measurement_pair_subquery(
                _codes_for(code_fields, 'bilirubin_total_mg_dl')),
            ast_u_l=_latest_measurement_number_subquery(
                _codes_for(code_fields, 'ast_u_l'), IntegerField()),
            alt_u_l=_latest_measurement_number_subquery(
                _codes_for(code_fields, 'alt_u_l'), IntegerField()),
            albumin_g_dl=_latest_measurement_pair_subquery(
                _codes_for(code_fields, 'albumin_g_dl')),
            her2_raw=_receptor_answer_subquery('48676-1'),
            estrogen_receptor_raw=_receptor_answer_subquery('16112-5'),
            progesterone_receptor_raw=_receptor_answer_subquery('16113-3'),
        )
        .values(
            'patient_age',
            'gender_raw',
            'disease',
            'stage',
            'ecog_performance_status',
            'karnofsky_performance_score',
            'hemoglobin_g_dl',
            'platelet_count_thousand_per_ul',
            'anc_thousand_per_ul',
            'wbc_count_thousand_per_ul',
            'serum_creatinine_mg_dl',
            'creatinine_clearance_ml_min',
            'serum_calcium_mg_dl',
            'bilirubin_total_mg_dl',
            'ast_u_l',
            'alt_u_l',
            'albumin_g_dl',
            'her2_raw',
            'estrogen_receptor_raw',
            'progesterone_receptor_raw',
        )
    )

    row = qs.get()
    return {
        'patient_age': row['patient_age'],
        'gender': _normalize_gender(row['gender_raw']),
        'disease': _normalize_disease(row['disease']),
        'stage': normalise_stage_label(row['stage']),
        'ecog_performance_status': row['ecog_performance_status'],
        'karnofsky_performance_score': row['karnofsky_performance_score'],
        'hemoglobin_g_dl': _canonical_pair(
            row['hemoglobin_g_dl'], normalize_hemoglobin_unit,
            HEMOGLOBIN_DEFAULT_UNIT, hemoglobin_canonical_from_pair,
            hemoglobin_canonical_in_range),
        'platelet_count_thousand_per_ul': _canonical_blood_count(row['platelet_count_thousand_per_ul']),
        'anc_thousand_per_ul': _canonical_blood_count(row['anc_thousand_per_ul']),
        'wbc_count_thousand_per_ul': _canonical_blood_count(row['wbc_count_thousand_per_ul']),
        'serum_creatinine_mg_dl': _canonical_pair(
            row['serum_creatinine_mg_dl'], normalize_creatinine_unit,
            CREATININE_DEFAULT_UNIT, creatinine_canonical_from_pair,
            creatinine_canonical_in_range),
        'creatinine_clearance_ml_min': row['creatinine_clearance_ml_min'],
        'serum_calcium_mg_dl': _canonical_pair(
            row['serum_calcium_mg_dl'],
            lambda unit: normalize_chemistry_unit('serum_calcium_mg_dl', unit),
            CHEMISTRY_PAIRS['serum_calcium_mg_dl']['canonical_unit'],
            lambda value, unit: chemistry_canonical_from_pair(
                'serum_calcium_mg_dl', value, unit),
            lambda value: chemistry_canonical_in_range('serum_calcium_mg_dl', value)),
        'bilirubin_total_mg_dl': _canonical_pair(
            row['bilirubin_total_mg_dl'],
            lambda unit: normalize_chemistry_unit('bilirubin_total_mg_dl', unit),
            CHEMISTRY_PAIRS['bilirubin_total_mg_dl']['canonical_unit'],
            lambda value, unit: chemistry_canonical_from_pair(
                'bilirubin_total_mg_dl', value, unit),
            lambda value: chemistry_canonical_in_range('bilirubin_total_mg_dl', value)),
        'ast_u_l': row['ast_u_l'],
        'alt_u_l': row['alt_u_l'],
        'albumin_g_dl': _canonical_pair(
            row['albumin_g_dl'],
            lambda unit: normalize_chemistry_unit('albumin_g_dl', unit),
            CHEMISTRY_PAIRS['albumin_g_dl']['canonical_unit'],
            lambda value, unit: chemistry_canonical_from_pair(
                'albumin_g_dl', value, unit),
            lambda value: chemistry_canonical_in_range('albumin_g_dl', value)),
        'her2_status': _normalize_receptor_status(row['her2_raw']),
        'estrogen_receptor_status': _normalize_receptor_status(row['estrogen_receptor_raw']),
        'progesterone_receptor_status': _normalize_receptor_status(row['progesterone_receptor_raw']),
    }


def _count_populated_fields(row):
    return sum(1 for value in row.values() if value not in (None, '', [], {}))


class Command(BaseCommand):
    help = (
        'Benchmark a 20-criterion trial eligibility fetch from raw OMOP tables '
        'versus the same fields on PatientRecord.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--org-slugs',
            default='synthea-mm',
            help='Comma-separated organization slugs to scope the cohort to.',
        )
        parser.add_argument(
            '--person-ids',
            default='',
            help='Comma-separated person_ids — overrides --org-slugs cohort selection.',
        )
        parser.add_argument('--limit', type=int, default=None)
        parser.add_argument(
            '--repeat',
            type=int,
            default=1,
            help='Repeat each timed pass N times for more samples (default: 1).',
        )
        parser.add_argument('--output', default='', help='Write raw stats JSON to this path.')

    def handle(self, *args, **options):
        org_slugs = [s.strip() for s in options['org_slugs'].split(',') if s.strip()]
        person_ids = _cohort_person_ids(org_slugs, options['person_ids'], options['limit'])

        if not person_ids:
            raise CommandError('No matching patients found for the given cohort.')

        repeat = options['repeat']
        self.stdout.write(
            f'Benchmarking {len(person_ids)} patient(s), '
            f'{repeat} repeat pass(es) per path...'
        )

        # Warm-up: touch both paths once per patient so the timed runs do not
        # inherit a one-sided cold-cache penalty.
        code_fields = resolve_lab_code_fields()
        for pid in person_ids:
            try:
                _fetch_patient_record_trial_row(pid)
            except PatientRecord.DoesNotExist:
                pass
            try:
                _fetch_omop_trial_row(pid, code_fields)
            except Person.DoesNotExist:
                pass

        patient_record_times = []
        omop_times = []
        patient_record_populated = []
        omop_populated = []

        for _ in range(repeat):
            for pid in person_ids:
                try:
                    t0 = time.perf_counter()
                    row = _fetch_patient_record_trial_row(pid)
                    patient_record_times.append(time.perf_counter() - t0)
                    patient_record_populated.append(_count_populated_fields(row))
                except PatientRecord.DoesNotExist:
                    continue

        for _ in range(repeat):
            for pid in person_ids:
                try:
                    t0 = time.perf_counter()
                    row = _fetch_omop_trial_row(pid, code_fields)
                    omop_times.append(time.perf_counter() - t0)
                    omop_populated.append(_count_populated_fields(row))
                except Person.DoesNotExist:
                    continue

        if not patient_record_times and not omop_times:
            raise CommandError(
                f'None of the given person_ids {person_ids} resolved to an actual '
                f'Person/PatientRecord — no timing samples collected.'
            )

        pr_stats = _stats(patient_record_times)
        omop_stats = _stats(omop_times)

        self.stdout.write(self.style.SUCCESS(f'\npatient_record pull: {pr_stats}'))
        self.stdout.write(self.style.SUCCESS(f'OMOP pull:           {omop_stats}'))

        if pr_stats.get('mean_ms') and omop_stats.get('mean_ms'):
            speedup = omop_stats['mean_ms'] / pr_stats['mean_ms']
            self.stdout.write(self.style.SUCCESS(
                f'\npatient_record is ~{speedup:.1f}x faster than the OMOP pull '
                f'(mean {pr_stats["mean_ms"]}ms vs {omop_stats["mean_ms"]}ms)'
            ))

        if patient_record_populated and omop_populated:
            self.stdout.write(
                f'Avg populated eligibility fields: '
                f'patient_record={statistics.mean(patient_record_populated):.1f}/20, '
                f'OMOP={statistics.mean(omop_populated):.1f}/20'
            )

        if options['output']:
            with open(options['output'], 'w') as f:
                json.dump({
                    'patient_record': pr_stats,
                    'omop': omop_stats,
                    'person_ids': person_ids,
                    'repeat': repeat,
                    'org_slugs': org_slugs,
                    'criteria_fields': TRIAL_ELIGIBILITY_FIELDS,
                }, f, indent=2)
            self.stdout.write(self.style.SUCCESS(f'\nResults written to {options["output"]}'))
