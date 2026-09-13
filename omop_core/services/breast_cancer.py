"""Breast staging semantics over existing dated OMOP facts."""
import re


STAGE_QUESTIONS = {
    'tumor_stage': {'21905-5': 'c', '21899-0': 'p'},
    'nodes_stage': {'21906-3': 'c', '21900-6': 'p'},
    'distant_metastasis_stage': {'21907-1': 'c', '21901-4': 'p'},
    'stage': {'21908-9': 'c', '21902-2': 'p'},
}


def fact_date(row):
    return getattr(row, 'measurement_date', None) or getattr(row, 'observation_date', None)


def event_key(row):
    return ((getattr(row, 'meas_event_field_concept_id', None) or getattr(row, 'obs_event_field_concept_id', None)),
            (getattr(row, 'measurement_event_id', None) or getattr(row, 'observation_event_id', None)))


def matches(row, codes, vocabulary='LOINC'):
    concept = getattr(row, 'measurement_concept', None) or getattr(row, 'observation_concept', None)
    source = getattr(row, 'measurement_source_value', None) or getattr(row, 'observation_source_value', None)
    # The standard target can be in a different vocabulary after Maps to.
    # Known legacy source keys remain readable; concept-code matching itself
    # must be namespaced and never accept an unrelated vocabulary's code.
    return (bool(concept) and concept.vocabulary_id == vocabulary and concept.concept_code in codes) or source in codes


def stage_presence(value, axis):
    token = str(value or '').strip().upper()
    token = re.sub(r'^(?:YP|CP|C|P|Y)(?=[TNM])', '', token)
    token = token.split(':', 1)[0].strip()
    if re.fullmatch(axis + r'0(?:\(I\+\)|I\+)?', token):
        return False
    if re.fullmatch(axis + (r'1(?:[A-C])?' if axis == 'M' else r'[1-3](?:MI|[A-C])?'), token):
        return True
    return None


def staging_data(snapshot, legacy_source):
    from omop_core.services.field_values import ValueResolver
    from omop_core.services.patient_record_service import _coded_value

    resolver = ValueResolver.for_snapshot(snapshot)
    rows = sorted([*snapshot.measurements, *snapshot.observations],
                  key=lambda r: (fact_date(r), r.pk, r._meta.db_table), reverse=True)
    candidates = [r for r in rows if any(matches(r, codes) for codes in STAGE_QUESTIONS.values())]
    # Never combine facts explicitly linked to different tumors/assessments.
    if candidates:
        latest_link = event_key(candidates[0])
        if all(latest_link):
            candidates = [r for r in candidates if event_key(r) == latest_link]
        else:
            # An older linked assessment must not outrank a newer unlinked
            # result. Nor can its other axes be attributed to the new result.
            candidates = [r for r in candidates if not all(event_key(r))]
    data, bases = {}, set()
    for field, codes in STAGE_QUESTIONS.items():
        row = next((r for r in candidates if matches(r, codes)), None)
        if row is None:
            continue
        concept = getattr(row, 'measurement_concept', None) or getattr(row, 'observation_concept', None)
        source = getattr(row, 'measurement_source_value', None) or getattr(row, 'observation_source_value', None)
        basis = codes.get(getattr(concept, 'concept_code', None)) or codes.get(source)
        if getattr(row, 'qualifier_source_value', None) == 'yp' and basis == 'p':
            basis = 'yp'
        value = resolver.reverse(field, row)
        if value is None:
            value = _coded_value(row)
        if value is None:
            continue
        data[field] = str(value)
        bases.add(basis)
    if bases:
        data['staging_modalities'] = ', '.join(sorted(bases))
    # Preserve source-only historical stage assertions without claiming a TNM basis.
    if 'stage' not in data:
        row = next((r for r in rows if getattr(r, 'observation_source_value', None) == legacy_source), None)
        if row and _coded_value(row):
            data['stage'] = _coded_value(row)
    riss = next((r for r in rows if (getattr(r, 'measurement_source_value', None)
        or getattr(r, 'observation_source_value', None)) == '21908-9-riss'), None)
    if riss and _coded_value(riss) and 'stage' not in data:
        data['stage'] = _coded_value(riss)
    if 'nodes_stage' in data:
        present = stage_presence(data['nodes_stage'], 'N')
        if present is not None:
            data['lymph_node_status'] = 'Positive' if present else 'Negative'
    if 'distant_metastasis_stage' in data:
        present = stage_presence(data['distant_metastasis_stage'], 'M')
        if present is not None:
            data['metastasis_status'] = 'Positive' if present else 'Negative'
    return data
