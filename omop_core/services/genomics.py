"""Variant CRUD over OMOP facts, with PatientRecord as a derived projection.

Components use the installed vocabulary's standard domain where available.
Unmapped source terms retain concept 0 and their source code, never fabricated
standard concept IDs. Event fields link every component to its parent variant.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils.dateparse import parse_date
from django.utils.timezone import localdate
from rest_framework.exceptions import NotFound, ValidationError

from omop_core.models import Concept, ConceptRelationship, FieldConceptMapping, Measurement, Note, Observation, PatientRecord
from omop_core.services.genomics_catalog import catalog, marker_for_variant, patient_fields
from omop_core.services.pk import next_pk
from omop_core.signals import suppress_patient_record_refresh

PREFIX = 'genomics:'
PARENT_CODE = '81252-9'
_CDM_TEXT_WIDTH = 60  # CDM conformant column width for value_as_string.
# field: (source code, fallback OMOP domain). LOINCs from the discrete
# genetic variant panel; local narrative fields explicitly remain unmapped.
FIELDS = {a['key']: (a['code'], a['table'].title()) for a in catalog()['attributes']}
# Correct catalog domains that disagree with LOINC (the catalog is frozen).
# The seed migration resolves without domain filtering, so the mapping's
# omop_table is correct; these fix the FIELDS fallback domain.
FIELDS['genomic_dna_change'] = ('81290-9', 'Observation')
FIELDS['variant_analysis_method_type'] = ('81304-8', 'Observation')
FIELDS['variant_category'] = ('83005-9', 'Observation')
# Components added after the frozen v1 catalog; seeded by later migrations.
# Fallback domains match LOINC's standard domain where a LOINC exists.
FIELDS['status'] = ('genomics:status', 'Measurement')          # LOINC 69548-6 → Measurement
FIELDS['clone_fraction'] = ('genomics:clone_fraction', 'Measurement')  # no LOINC
FIELDS['transcript_dna_change'] = ('genomics:transcript_dna_change', 'Measurement')  # LOINC 48004-6 → Measurement
FIELDS['coverage_depth'] = ('genomics:coverage_depth', 'Observation')  # LOINC 82121-5 → Observation
FIELDS['amino_acid_change_type'] = ('genomics:amino_acid_change_type', 'Measurement')  # LOINC 48006-1 → Measurement

# Variant-level components that do not apply to absent findings.
_VARIANT_LEVEL_FIELDS = frozenset({
    'amino_acid_change', 'allelic_frequency', 'genomic_dna_change',
    'transcript_reference_sequence_id',
})


def approved_mapping(field_name):
    mapping = FieldConceptMapping.objects.filter(field_name=field_name, status='approved').select_related('concept').first()
    if mapping is None or mapping.omop_table not in ('measurement', 'observation') or not mapping.source_value or len(mapping.source_value) > 50:
        raise ValidationError({field_name: 'A complete approved genomics field mapping is required.'})
    return mapping


def mapped_concept(mapping):
    if mapping.concept_id and mapping.concept.standard_concept == 'S':
        if mapping.concept.domain_id.lower() == mapping.omop_table:
            return mapping.concept_id, mapping.concept_id
        # Domain drifted in a newer Athena release — fall through to LOINC
        # resolution which handles mismatches gracefully (concept 0 + source).
    if mapping.vocabulary_id == 'LOINC' and mapping.concept_code:
        concept, source, domain = _resolve(mapping.concept_code, mapping.omop_table.title())
        return (concept if domain.lower() == mapping.omop_table else 0), source
    return 0, None


def normalize_variant(payload, existing=None):
    if not isinstance(payload, dict):
        raise ValidationError({'variant': 'Expected an object.'})
    allowed = set(FIELDS) | {'id', 'variant', 'mutation', 'test_date', 'assay_method', 'allelic_frequency_unit', 'clone_fraction_unit', 'marker_key'}
    unknown = set(payload) - allowed
    if unknown:
        raise ValidationError({key: 'Unknown variant field.' for key in sorted(unknown)})
    data = {**(existing or {}), **payload}
    if 'mutation' in payload and 'variant' not in payload:
        data['variant'] = payload['mutation']
    if 'assay_method' in payload and 'variant_analysis_method_type' not in payload:
        data['variant_analysis_method_type'] = payload['assay_method']
    _NUMERIC_FIELDS = {'allelic_frequency', 'clone_fraction', 'coverage_depth'}
    for key in set(FIELDS) | {'variant'}:
        if key in _NUMERIC_FIELDS:
            continue
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValidationError({key: 'Expected text.'})
        data[key] = (value or '') if key == 'variant_description' else (value or '').strip()
        if len(data[key]) > 10000:
            raise ValidationError({key: 'Use at most 10000 characters.'})
    data['gene'] = data['gene'].upper()
    if not data['gene'] or len(data['gene']) > 50:
        raise ValidationError({'gene': 'Enter a gene symbol (at most 50 characters).'})
    # A gene-only result is allowed (e.g. a test with no specific variant).
    raw_date = data.get('test_date')
    try:
        test_date = parse_date(raw_date) if isinstance(raw_date, str) else None
    except ValueError:
        test_date = None
    if raw_date and test_date is None:
        raise ValidationError({'test_date': 'Use a valid YYYY-MM-DD date.'})
    data['test_date'] = (test_date or localdate()).isoformat()
    for key in ('collection_date', 'interpretation_date'):
        try:
            if data.get(key) and parse_date(data[key]) is None:
                raise ValueError()
        except ValueError:
            raise ValidationError({key: 'Use a valid YYYY-MM-DD date.'})
    if data.get('assessment') not in ('', 'present', 'absent', 'not_tested', 'no_call', 'indeterminate'):
        raise ValidationError({'assessment': 'Use present, absent, not_tested, no_call or indeterminate.'})
    status = data.get('status', '')
    if status not in ('', 'present', 'absent', 'indeterminate'):
        raise ValidationError({'status': 'Use present, absent or indeterminate.'})
    # Default to present when omitted, preserving every existing caller.
    if not status:
        data['status'] = 'present'
    # Variant-level components do not apply to absent findings.
    if data['status'] == 'absent':
        supplied_variant_fields = _VARIANT_LEVEL_FIELDS & set(payload or {})
        nonempty = {k for k in supplied_variant_fields if payload.get(k) not in (None, '')}
        if nonempty:
            raise ValidationError({k: 'Variant-level components are not accepted on an absent finding.' for k in sorted(nonempty)})
        # Clear any inherited variant-level values from an existing record.
        for k in _VARIANT_LEVEL_FIELDS:
            if k == 'allelic_frequency':
                data[k] = None
            else:
                data[k] = ''
    frequency = data.get('allelic_frequency')
    unit = data.get('allelic_frequency_unit') or '%'
    if unit not in ('%', '1'):
        raise ValidationError({'allelic_frequency_unit': 'Use % or 1 (fraction).'})
    data['allelic_frequency_unit'] = unit
    if frequency is None or frequency == '':
        data['allelic_frequency'] = None
    else:
        try:
            number = Decimal(str(frequency))
        except (InvalidOperation, ValueError):
            number = Decimal('NaN')
        if not number.is_finite() or not 0 <= number <= (100 if unit == '%' else 1):
            raise ValidationError({'allelic_frequency': 'Enter 0–100 for % or 0–1 for a fraction.'})
        if number != number.quantize(Decimal('0.00001')):
            raise ValidationError({'allelic_frequency': 'Use at most five decimal places.'})
        data['allelic_frequency'] = number
    # Clone fraction: same unit discipline as allelic_frequency.
    clone_frac = data.get('clone_fraction')
    cf_unit = data.get('clone_fraction_unit') or '%'
    if cf_unit not in ('%', '1'):
        raise ValidationError({'clone_fraction_unit': 'Use % or 1 (fraction).'})
    data['clone_fraction_unit'] = cf_unit
    if clone_frac is None or clone_frac == '':
        data['clone_fraction'] = None
    else:
        try:
            number = Decimal(str(clone_frac))
        except (InvalidOperation, ValueError):
            number = Decimal('NaN')
        if not number.is_finite() or not 0 <= number <= (100 if cf_unit == '%' else 1):
            raise ValidationError({'clone_fraction': 'Enter 0–100 for % or 0–1 for a fraction.'})
        if number != number.quantize(Decimal('0.00001')):
            raise ValidationError({'clone_fraction': 'Use at most five decimal places.'})
        data['clone_fraction'] = number
    # Coverage depth: non-negative integer or decimal.
    cov = data.get('coverage_depth')
    if cov is None or cov == '':
        data['coverage_depth'] = None
    else:
        try:
            number = Decimal(str(cov))
        except (InvalidOperation, ValueError):
            number = Decimal('NaN')
        if not number.is_finite() or number < 0:
            raise ValidationError({'coverage_depth': 'Enter a non-negative number.'})
        data['coverage_depth'] = number
    return data


def _resolve(code, domain):
    source = Concept.objects.filter(
        vocabulary_id='LOINC', concept_code=code, invalid_reason__isnull=True,
    ).first() if not code.startswith(PREFIX) else None
    standard = source if source and source.standard_concept == 'S' else None
    if source and standard is None:
        mapping = ConceptRelationship.objects.filter(
            concept_1=source, relationship_id='Maps to', invalid_reason__isnull=True,
            concept_2__standard_concept='S', concept_2__invalid_reason__isnull=True,
            concept_2__domain_id__in=['Measurement', 'Observation'],
        ).select_related('concept_2').first()
        standard = mapping.concept_2 if mapping else None
    if standard and standard.domain_id in ('Measurement', 'Observation'):
        domain = standard.domain_id
    else:
        standard = None
    return (standard.pk if standard else 0), (source.pk if source else None), domain


def _measurement_event_concepts():
    # Athena uses CDM### codes; older imports use table.column as the code.
    # Share this identity rule across writes, reads, edits and deletes.
    return Concept.objects.filter(vocabulary_id='CDM').filter(
        Q(concept_code='measurement.measurement_id')
        | Q(concept_name='measurement.measurement_id', standard_concept='S')
    )


def _event_concept():
    concept = _measurement_event_concepts().filter(invalid_reason__isnull=True).first()
    if concept is None:
        raise ValidationError({'variant': 'Load the OMOP CDM vocabulary (measurement.measurement_id) before saving variants.'})
    return concept.pk


def _store_text(value, person, date, type_concept_id, parent_pk):
    """Store text, overflow to a linked NOTE row if it exceeds CDM width.

    Returns the string to store in value_as_string (truncated with note
    reference if overflow) and the created Note pk (or None).
    """
    if not value or len(value) <= _CDM_TEXT_WIDTH:
        return value, None
    note = Note.objects.create(
        note_id=next_pk(Note, 'note_id'),
        person=person,
        note_date=date,
        note_type_concept_id=0,
        note_text=value,
        note_source_value=f'genomics:overflow:{parent_pk}',
    )
    reference = f'[note:{note.pk}]'
    return value[:_CDM_TEXT_WIDTH - len(reference)] + reference, note.pk


def _read_note_text(value):
    """Retrieve full text from a linked NOTE if the value contains a note reference."""
    if not value or '[note:' not in value:
        return value
    import re
    match = re.search(r'\[note:(\d+)\]$', value)
    if not match:
        return value
    try:
        note = Note.objects.get(pk=int(match.group(1)))
        return note.note_text
    except Note.DoesNotExist:
        return value


def _components(person, parent_id):
    # Event type is essential: IDs can coincide across different OMOP tables.
    return (
        Measurement.objects.filter(person=person, measurement_event_id=parent_id,
            meas_event_field_concept__in=_measurement_event_concepts(), is_erroneous=False),
        Observation.objects.filter(person=person, observation_event_id=parent_id,
            obs_event_field_concept__in=_measurement_event_concepts(), is_erroneous=False),
    )


def enrich_variants(variants, snapshot):
    """Overlay linked components in one snapshot pass, including imported facts."""
    by_id = {v['id']: v for v in variants}
    codes = {code: key for key, (code, _) in FIELDS.items()}
    # Reads survive withdrawn approval; only writes require current approval.
    codes.update({m.source_value: m.field_name.split('.', 1)[1]
        for m in FieldConceptMapping.objects.filter(field_name__startswith='genetic_mutations.')})
    for rows, prefix, event_field in (
        (snapshot.measurements, 'measurement', 'meas_event_field_concept_id'),
        (snapshot.observations, 'observation', 'obs_event_field_concept_id'),
    ):
        # Resolve metadata in bulk; never mistake another table's ID for ours.
        field_ids = {getattr(row, event_field) for row in rows if getattr(row, event_field)}
        valid_ids = set(_measurement_event_concepts().filter(pk__in=field_ids).values_list('pk', flat=True))
        for row in reversed(rows):  # snapshot is newest-first: newest wins
            target = by_id.get(getattr(row, f'{prefix}_event_id'))
            if target is None or getattr(row, event_field) not in valid_ids or row.is_erroneous:
                continue
            field = codes.get(getattr(row, f'{prefix}_source_value'))
            if not field:
                concept = getattr(row, f'{prefix}_concept')
                field = codes.get(concept.concept_code) if concept.vocabulary_id == 'LOINC' else None
            if field == 'allelic_frequency':
                target[field] = float(row.value_as_number) if row.value_as_number is not None else None
                target['allelic_frequency_unit'] = row.unit_source_value or '1'
            elif field == 'clone_fraction':
                target[field] = float(row.value_as_number) if row.value_as_number is not None else None
                target['clone_fraction_unit'] = row.unit_source_value or '1'
            elif field == 'coverage_depth':
                target[field] = float(row.value_as_number) if row.value_as_number is not None else None
            elif field:
                value = _read_note_text(row.value_as_string)
                if value is None and row.value_as_concept_id:
                    value = row.value_as_concept.concept_name
                target[field] = value
    return variants


def list_variants(person):
    from omop_core.services.patient_record_service import _get_genetic_mutations
    return _get_genetic_mutations(person)['genetic_mutations']


def _find(person, variant_id):
    variant = next((v for v in list_variants(person) if v['id'] == variant_id), None)
    if variant is None:
        raise NotFound('Variant not found for this patient.')
    return variant


@transaction.atomic
@suppress_patient_record_refresh()
def save_variant(person, payload, variant_id=None, type_concept_id=32817, skip_refresh=False):
    PatientRecord.objects.select_for_update().get(person=person)
    previous = _find(person, variant_id) if variant_id is not None else None
    data = normalize_variant(payload, previous)
    marker = marker_for_variant(data)
    if data.get('marker_key') and marker is None:
        raise ValidationError({'marker_key': 'Unknown priority marker.'})
    if marker and data['gene'].upper() != marker['gene'].upper():
        raise ValidationError({'gene': 'The gene must match the selected priority marker.'})
    parent_mapping = approved_mapping(marker['field_name']) if marker else None
    if parent_mapping and parent_mapping.omop_table != 'measurement':
        raise ValidationError({marker['field_name']: 'Variant parents must map to Measurement.'})
    component_mappings = {key: approved_mapping('genetic_mutations.' + key)
        for key in FIELDS if key in payload or (data.get(key) is not None and data.get(key) != '')}
    if payload.get('id') is not None and payload['id'] != variant_id:
        raise ValidationError({'id': 'Use the variant ID in the URL; new IDs are assigned by the server.'})
    event_concept_id = _event_concept()
    if not Concept.objects.filter(pk=type_concept_id).exists() or not Concept.objects.filter(pk=0).exists():
        raise ValidationError({'variant': 'Required OMOP type and unmapped concepts are unavailable.'})
    concept_id, source_id, domain = _resolve(PARENT_CODE, 'Measurement')
    if parent_mapping:
        concept_id, source_id = mapped_concept(parent_mapping)
        domain = 'Measurement'
    # Never assign an Observation-domain standard concept to a Measurement.
    # Keep the source code and source concept if this vocabulary release has
    # no appropriate standard Measurement concept for the parent.
    if domain != 'Measurement':
        concept_id = 0
    if variant_id is None:
        parent = Measurement(
            measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
            measurement_concept_id=concept_id, measurement_source_concept_id=source_id,
            measurement_type_concept_id=type_concept_id, measurement_source_value=PARENT_CODE,
        )
    else:
        parent = Measurement.objects.get(person=person, pk=variant_id, is_erroneous=False)
    parent.measurement_date = data['test_date']
    parent.qualifier_source_value = data['gene']
    raw_variant = data['variant'] or data['variant_name'] or data['genomic_dna_change'] or data['amino_acid_change']
    stored, _ = _store_text(raw_variant, person, data['test_date'], type_concept_id, parent.measurement_id)
    parent.value_as_string = stored
    # Origin and interpretation are separate facts. Do not leave stale legacy
    # qualifiers after the corresponding component has been cleared.
    parent.qualifier_concept_id = None
    parent.value_as_concept_id = None
    # Imported gene-specific questions become the generic question if edited:
    # otherwise the old question would keep projecting its original gene.
    if previous:
        parent.measurement_concept_id, parent.measurement_source_concept_id = concept_id, source_id
        parent.measurement_source_value = PARENT_CODE
    if parent_mapping:
        parent.measurement_source_value = parent_mapping.source_value
    parent.save()
    for rows in _components(person, parent.pk):
        # Only replace known component fields; unrelated linked facts survive.
        source_field = f'{rows.model._meta.model_name}_source_value'
        codes = [code for code, _ in FIELDS.values()] + list(FieldConceptMapping.objects.filter(
            field_name__startswith='genetic_mutations.').values_list('source_value', flat=True))
        concept_field = f'{rows.model._meta.model_name}_concept'
        rows.filter(Q(**{f'{source_field}__in': codes}) | Q(**{
            f'{concept_field}__vocabulary_id': 'LOINC', f'{concept_field}__concept_code__in': codes,
        })).update(
            is_erroneous=True, erroneous_reason='Superseded in Genomics editor')
    for key, (code, fallback_domain) in FIELDS.items():
        value = data.get(key)
        if value is None or value == '':
            continue
        mapping = component_mappings[key]
        concept_id, source_id = mapped_concept(mapping)
        domain = mapping.omop_table.title()
        code = mapping.source_value
        model, prefix, event_field = ((Measurement, 'measurement', 'meas_event_field_concept_id')
            if domain == 'Measurement' else (Observation, 'observation', 'obs_event_field_concept_id'))
        attrs = {
            f'{prefix}_id': next_pk(model, f'{prefix}_id'), 'person': person,
            f'{prefix}_concept_id': concept_id, f'{prefix}_source_concept_id': source_id,
            f'{prefix}_source_value': code, f'{prefix}_date': data['test_date'],
            f'{prefix}_type_concept_id': type_concept_id,
            f'{prefix}_event_id': parent.pk, event_field: event_concept_id,
        }
        if key == 'allelic_frequency':
            attrs['value_as_number'] = value
            attrs['unit_source_value'] = data['allelic_frequency_unit']
            unit = Concept.objects.filter(vocabulary_id='UCUM', concept_code=data['allelic_frequency_unit'],
                standard_concept='S', invalid_reason__isnull=True).first()
            attrs['unit_concept_id'] = unit.pk if unit else 0
        elif key == 'clone_fraction':
            attrs['value_as_number'] = value
            attrs['unit_source_value'] = data['clone_fraction_unit']
            unit = Concept.objects.filter(vocabulary_id='UCUM', concept_code=data['clone_fraction_unit'],
                standard_concept='S', invalid_reason__isnull=True).first()
            attrs['unit_concept_id'] = unit.pk if unit else 0
        elif key == 'coverage_depth':
            attrs['value_as_number'] = value
        else:
            stored_text, _ = _store_text(str(value), person, data['test_date'], type_concept_id, parent.pk)
            attrs['value_as_string'] = stored_text
        model.objects.create(**attrs)
    if not skip_refresh:
        from omop_core.services.patient_record_service import refresh_patient_record
        refresh_patient_record(person)
    return _find(person, parent.pk)


@transaction.atomic
@suppress_patient_record_refresh()
def delete_variant(person, variant_id, skip_refresh=False):
    PatientRecord.objects.select_for_update().get(person=person)
    previous = _find(person, variant_id)
    marker = marker_for_variant(previous)
    if marker:
        approved_mapping(marker['field_name'])
    for rows in _components(person, variant_id):
        rows.update(is_erroneous=True, erroneous_reason='Removed in Genomics editor')
    Measurement.objects.filter(person=person, pk=variant_id).update(
        is_erroneous=True, erroneous_reason='Removed in Genomics editor')
    if not skip_refresh:
        from omop_core.services.patient_record_service import refresh_patient_record
        refresh_patient_record(person)


@transaction.atomic
@suppress_patient_record_refresh()
def replace_variants(person, payload):
    """Compatibility for PatientRecord PATCH and existing import callers."""
    if not isinstance(payload, list):
        raise ValidationError({'genetic_mutations': 'Expected a list of variants.'})
    PatientRecord.objects.select_for_update().get(person=person)
    current = {row['id']: row for row in list_variants(person)}
    keep = set()
    for row in payload:
        if not isinstance(row, dict):
            raise ValidationError({'genetic_mutations': 'Each variant must be an object.'})
        variant_id = row.get('id')
        if variant_id is not None and (isinstance(variant_id, bool) or not isinstance(variant_id, int) or variant_id not in current or variant_id in keep):
            raise ValidationError({'id': 'Variant ID must be unique and belong to this patient.'})
        # An unchanged projection echo must preserve imported facts as-is.
        if variant_id is not None and row == current[variant_id]:
            keep.add(variant_id)
            continue
        saved = save_variant(person, row, variant_id, type_concept_id=32865)
        keep.add(saved['id'])
    for variant_id in current.keys() - keep:
        delete_variant(person, variant_id)


@transaction.atomic
@suppress_patient_record_refresh()
def replace_priority_fields(person, values, type_concept_id=32817):
    """PatientRecord edits replace only the named marker's list of findings."""
    PatientRecord.objects.select_for_update().get(person=person)
    for field, rows in values.items():
        marker = patient_fields()[field]
        approved_mapping(field)
        if not isinstance(rows, list):
            raise ValidationError({field: 'Expected a list of findings; [] clears this marker.'})
        before = {v['id']: v for v in list_variants(person) if marker_for_variant(v) == marker}
        keep = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValidationError({field: 'Each finding must be an object.'})
            variant_id = row.get('id')
            if variant_id is not None and (isinstance(variant_id, bool) or not isinstance(variant_id, int) or variant_id not in before or variant_id in keep):
                raise ValidationError({field: 'Finding ID must belong to this marker and patient, without duplicates.'})
            result = save_variant(person, {**row, 'gene': row.get('gene') or marker['gene'], 'marker_key': marker['key']}, variant_id, type_concept_id)
            keep.add(result['id'])
        for variant_id in before.keys() - keep:
            delete_variant(person, variant_id)
