"""Read-only cohort summaries and filters; never refresh PatientRecord on a list."""
from datetime import date, timedelta

from django.db.models import BooleanField, Case, F, IntegerField, OuterRef, Q, Subquery, TextField, Value, When
from django.db.models.functions import Coalesce, Greatest, Lower, Trim
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from omop_core.models import Measurement, Observation
from patient_portal.models import PatientUser


UNKNOWN = ('', 'unknown', 'not recorded', 'n/a', 'not available')
GAPS = {'stage': 'Stage', 'ecog': 'ECOG', 'genomics': 'Genomics'}
ORDER_FIELDS = {
    'updated': 'updated_at', 'age': 'date_of_birth', 'name': 'person__given_name',
    'disease': 'disease', 'stage': 'stage', 'ecog': 'ecog_performance_status',
    'lines': 'therapy_lines_count', 'status': 'list_disease_status',
    'freshness': 'latest_result_date', 'gaps': 'key_gap_count',
    'organization': 'organization__name',
}


def annotate_context(queryset):
    """One SQL query with indexed latest-result lookups, independent of page size."""
    today = timezone.localdate()
    measurements = Measurement.objects.filter(
        person_id=OuterRef('person_id'), is_erroneous=False, measurement_date__lte=today,
    ).exclude(value_source_value='PatientRecord:cleared').filter(
        Q(value_as_number__isnull=False) | Q(value_as_concept__isnull=False) |
        (Q(value_as_string__isnull=False) & ~Q(value_as_string='')),
    ).order_by('-measurement_date', '-measurement_id')
    observations = Observation.objects.filter(
        person_id=OuterRef('person_id'), is_erroneous=False, observation_date__lte=today,
    ).exclude(value_source_value='PatientRecord:cleared').filter(
        Q(value_as_number__isnull=False) | Q(value_as_concept__isnull=False) |
        (Q(value_as_string__isnull=False) & ~Q(value_as_string='')),
    ).order_by('-observation_date', '-observation_id')
    queryset = queryset.annotate(
        latest_result_date=Greatest(
            Subquery(measurements.values('measurement_date')[:1]),
            Subquery(observations.values('observation_date')[:1]),
        ),
        _condition_status=Lower(Trim(Coalesce('condition_clinical_status', Value(''), output_field=TextField()))),
        _progression_status=Lower(Trim(Coalesce('progression', Value(''), output_field=TextField()))),
        _stage=Lower(Trim(Coalesce('stage', Value(''), output_field=TextField()))),
        _molecular=Lower(Trim(Coalesce('molecular_markers', Value(''), output_field=TextField()))),
        _cytogenetic=Lower(Trim(Coalesce('cytogenetic_markers', Value(''), output_field=TextField()))),
    ).annotate(
        list_disease_status=Case(
            When(~Q(_condition_status__in=UNKNOWN), then=Trim('condition_clinical_status')),
            When(~Q(_progression_status__in=UNKNOWN), then=Trim('progression')),
            default=Value(None), output_field=TextField(),
        ),
        gap_stage=Case(When(_stage__in=UNKNOWN, then=True), default=False, output_field=BooleanField()),
        gap_ecog=Case(When(ecog_performance_status__isnull=True, then=True), default=False, output_field=BooleanField()),
        gap_genomics=Case(When(genetic_mutations=[], _molecular__in=UNKNOWN,
                              _cytogenetic__in=UNKNOWN, then=True), default=False, output_field=BooleanField()),
        contact_available=Case(When(
            (Q(email__isnull=False) & ~Q(email='')) |
            (Q(phone_number__isnull=False) & ~Q(phone_number='')),
            then=True), default=False, output_field=BooleanField()),
    )
    return queryset.annotate(key_gap_count=sum(
        (Case(When(**{f'gap_{key}': True}, then=1), default=0, output_field=IntegerField()) for key in GAPS),
        Value(0),
    ))


def visible_demographics(actor):
    """Use the same opt-out semantics as the patient detail serializer."""
    own = PatientUser.objects.filter(identity=actor).values('person_id') if actor and actor.is_authenticated else []
    return Q(suppress_demographics_for_others=False) | Q(person_id__in=own)


def filter_context(queryset, params, actor):
    queryset = annotate_context(queryset)
    status = params.get('clinical_status', '').strip()
    if status and status != 'all':
        queryset = queryset.filter(list_disease_status__isnull=True) if status == '__unknown__' else queryset.filter(list_disease_status__iexact=status)
    ecog = params.get('ecog', 'all')
    if ecog != 'all':
        if ecog == 'unknown':
            queryset = queryset.filter(ecog_performance_status__isnull=True)
        elif ecog in {'0', '1', '2', '3', '4', '5'}:
            queryset = queryset.filter(ecog_performance_status=int(ecog))
        else:
            raise ValidationError({'ecog': 'Choose 0–5, unknown, or all.'})
    gap = params.get('data_gap', 'all')
    if gap == 'any':
        queryset = queryset.filter(key_gap_count__gt=0)
    elif gap == 'none':
        queryset = queryset.filter(key_gap_count=0)
    elif gap in GAPS:
        queryset = queryset.filter(**{f'gap_{gap}': True})
    elif gap != 'all':
        raise ValidationError({'data_gap': 'Unknown data gap filter.'})
    freshness = params.get('freshness', 'all')
    if freshness == 'unknown':
        queryset = queryset.filter(latest_result_date__isnull=True)
    elif freshness in {'30d', '90d', 'older'}:
        cutoff = timezone.localdate() - timedelta(days=30 if freshness == '30d' else 90)
        queryset = queryset.filter(latest_result_date__lt=cutoff) if freshness == 'older' else queryset.filter(latest_result_date__gte=cutoff)
    elif freshness != 'all':
        raise ValidationError({'freshness': 'Unknown clinical result age filter.'})
    for param, fields in {
        'treatment': ('first_line_therapy', 'second_line_therapy', 'later_therapy', 'later_therapies'),
        'biomarker': ('histologic_type', 'myeloma_type', 'hr_status', 'her2_status',
                      'estrogen_receptor_status', 'progesterone_receptor_status',
                      'molecular_markers', 'cytogenetic_markers', 'genetic_mutations'),
        'location': ('city', 'region', 'country'),
    }.items():
        term = params.get(param, '').strip()
        if term:
            query = Q()
            for field in fields:
                query |= Q(**{f'{field}__icontains': term})
            queryset = queryset.filter(query)
            if param == 'location':
                queryset = queryset.filter(visible_demographics(actor))
    contact = params.get('contact', 'all')
    if contact in {'available', 'missing'}:
        queryset = queryset.filter(contact_available=contact == 'available')
    elif contact != 'all':
        raise ValidationError({'contact': 'Unknown contact filter.'})
    return queryset


def order_context(queryset, ordering, actor=None):
    key = ordering.lstrip('-')
    if key not in ORDER_FIELDS:
        raise ValidationError({'ordering': 'Unknown patient list ordering.'})
    descending = ordering.startswith('-')
    # Age increases as birth date decreases.
    if key == 'age':
        descending = not descending
    expression = F(ORDER_FIELDS[key])
    if key in {'age', 'name'}:
        expression = Case(When(visible_demographics(actor), then=expression), default=Value(None))
    return queryset.order_by(expression.desc(nulls_last=True) if descending else expression.asc(nulls_last=True), 'person_id')


def recorded(value):
    return value is not None and str(value).strip().lower() not in UNKNOWN


def latest_treatment(record):
    """Summarize stored lines; an absent end date never implies active treatment."""
    candidates = []
    for number, prefix in ((1, 'first_line'), (2, 'second_line'), (3, 'later')):
        name = getattr(record, f'{prefix}_therapy')
        if recorded(name):
            candidates.append({'name': name, 'line': number,
                               'start_date': getattr(record, f'{prefix}_start_date'),
                               'end_date': getattr(record, f'{prefix}_end_date')})
    for row in record.later_therapies or []:
        if isinstance(row, dict) and recorded(row.get('therapy')):
            try:
                line = int(row.get('lineNumber', 3))
            except (ValueError, TypeError):
                line = 3
            candidates.append({'name': row['therapy'], 'line': line,
                               'start_date': row.get('startDate'), 'end_date': row.get('endDate')})
    def parsed(value):
        try:
            return date.fromisoformat(str(value)[:10])
        except (ValueError, TypeError):
            return None
    for row in candidates:
        row['start_date'] = parsed(row['start_date'])
        row['end_date'] = parsed(row['end_date'])
    return max(candidates, key=lambda row: (row['line'], row['start_date'] or date.min), default=None)


def subtype_biomarkers(record):
    values = []
    for field, label in (('histologic_type', ''), ('myeloma_type', ''), ('hr_status', 'HR'),
                         ('estrogen_receptor_status', 'ER'), ('progesterone_receptor_status', 'PR'),
                         ('her2_status', 'HER2')):
        value = getattr(record, field)
        if recorded(value):
            text = f'{label}: {value}' if label else value
            if text not in values:
                values.append(text)
    return '; '.join(values)
