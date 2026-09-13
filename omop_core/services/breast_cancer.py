"""Breast staging semantics over existing dated OMOP facts."""
import re


STAGE_QUESTIONS = {
    'tumor_stage': {'21905-5': 'c', '21899-0': 'p'},
    'nodes_stage': {'21906-3': 'c', '21900-6': 'p'},
    'distant_metastasis_stage': {'21907-1': 'c', '21901-4': 'p'},
    'stage': {'21908-9': 'c', '21902-2': 'p'},
}


def staging_basis(value):
    """Accept the existing basis labels; never choose one from a mixed list."""
    if value is None or value == '':
        return None
    labels = {'c': 'clinical', 'p': 'pathological', 'yp': 'pathological after neoadjuvant therapy'}
    token = str(value).strip().casefold()
    for basis, label in labels.items():
        if token in (basis, label, f'{basis}: {label}', f'{basis} → {label}'):
            return basis
    raise ValueError('Select one clinical, pathological or post-neoadjuvant pathological basis')


def staging_projection(field, projection, *, basis=None):
    """Narrow TNM adapter under the approved canonical field recipe.

    Unscoped custom questions retain their existing recipe. A scoped answer is
    reviewed under BC:c, BC:p or BC:yp; an unspecified basis retains the default
    answer scope of the approved field mapping.
    """
    from omop_core.models import Concept
    from omop_core.services.field_values import standard_target
    codes = STAGE_QUESTIONS.get(field, {})
    question = Concept.objects.filter(pk=projection.get('concept_id')).first()
    if not question or question.vocabulary_id != 'LOINC' or question.concept_code not in codes:
        if projection.get('staging_basis'):
            raise ValueError('The approved field question has no staging recipe for the selected basis')
        return projection
    selected = staging_basis(projection.get('staging_basis')) if basis is None else basis
    explicit = selected is not None
    selected = selected or codes[question.concept_code]
    code = next(code for code, b in codes.items() if b == ('p' if selected == 'yp' else selected))
    target = Concept.objects.filter(vocabulary_id='LOINC', concept_code=code).first()
    if not standard_target(target, {'Measurement'}):
        raise ValueError('The selected staging question is unavailable in the current vocabulary')
    return {**projection, 'concept_id': target.pk, 'source_value': code, 'omop_table': 'measurement',
            'staging_field': field, 'qualifier_source_value': selected,
            'context_key': f'BC:{selected}' if explicit else ''}


def staging_clear_projections(field, projection):
    first = staging_projection(field, projection)
    if 'staging_field' not in first:
        return [first]
    # PatientRecord's flat-field clear suppresses every prior basis in the
    # aggregate view. Underlying dated/linked assessment history is retained.
    recipes = [first]
    for basis in ('c', 'p', 'yp'):
        recipe = staging_projection(field, projection, basis=basis)
        if recipe not in recipes:
            recipes.append(recipe)
    return recipes


def staging_history_key(row):
    for field, codes in STAGE_QUESTIONS.items():
        if matches(row, codes):
            concept = getattr(row, 'measurement_concept', None) or getattr(row, 'observation_concept', None)
            source = getattr(row, 'measurement_source_value', None) or getattr(row, 'observation_source_value', None)
            basis = codes.get(getattr(concept, 'concept_code', None)) or codes.get(source)
            if basis == 'p' and getattr(row, 'qualifier_source_value', None) == 'yp':
                basis = 'yp'
            return ('staging', field, basis)
    return None


def fact_date(row):
    from datetime import date
    value = getattr(row, 'measurement_date', None) or getattr(row, 'observation_date', None)
    return date.fromisoformat(value) if isinstance(value, str) else value


def fact_order(row):
    from datetime import datetime, time, timezone
    moment = getattr(row, 'measurement_datetime', None) or getattr(row, 'observation_datetime', None)
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    moment = moment or datetime.combine(fact_date(row), time.min, tzinfo=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (fact_date(row), moment, row.pk, row._meta.db_table)


def without_cleared_staging_history(measurements, observations):
    """Apply staging clears across current and legacy OMOP destinations."""
    from omop_core.services.omop_projection import CLEAR_VALUE
    clears = {}
    for row in [*measurements, *observations]:
        key = staging_history_key(row)
        if key and row.value_source_value == CLEAR_VALUE:
            if key not in clears or fact_order(row) > fact_order(clears[key]):
                clears[key] = row

    def visible(row):
        clear = clears.get(staging_history_key(row))
        if clear is None or row.value_source_value == CLEAR_VALUE:
            return True
        # Cross-table PKs do not establish same-day chronology. An undated
        # legacy row cannot override a clear merely because its ID is larger.
        if (fact_date(row) == fact_date(clear) and row._meta.db_table != clear._meta.db_table
                and not (getattr(row, 'measurement_datetime', None) or getattr(row, 'observation_datetime', None))):
            return False
        return fact_order(row) > fact_order(clear)

    return [r for r in measurements if visible(r)], [r for r in observations if visible(r)]


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
    selected_rows = snapshot.genomics_cache.setdefault('field_value_native_rows', {})
    selected_rows.update({field: None for field in STAGE_QUESTIONS})
    overrides = {}
    for (field, context), choices in resolver.read_choices.items():
        if field not in STAGE_QUESTIONS or context not in ('', 'BC:c', 'BC:p', 'BC:yp'):
            continue
        for choice in choices:
            mapping = resolver.mapping(choice, for_read=True)
            if mapping and mapping.question_concept_id:
                overrides.setdefault((field, mapping.question_concept_id), set()).add(context.removeprefix('BC:'))

    def field_basis(field, row):
        native = staging_history_key(row)
        if native and native[1] == field:
            return native[2]
        question_id = getattr(row, 'measurement_concept_id', None) or getattr(row, 'observation_concept_id', None)
        bases = overrides.get((field, question_id), set())
        qualifier = getattr(row, 'qualifier_source_value', None)
        if qualifier in ('c', 'p', 'yp') and (qualifier in bases or '' in bases):
            return qualifier
        return next(iter(bases)) if len(bases) == 1 and '' not in bases else None

    def stage_value(field, row):
        basis = field_basis(field, row)
        context = f'BC:{basis}' if basis else ''
        if (field, context) not in resolver.read_choices:
            context = ''
        value = resolver.reverse(field, row, context)
        if value is None:
            value = _coded_value(row)
        if value is None or str(value).strip().casefold() in ('', 'true', 'false', 'yes', 'no'):
            return None
        return str(value).strip()

    rows = sorted([*snapshot.measurements, *snapshot.observations], key=fact_order, reverse=True)
    candidates = [r for r in rows if any(field_basis(field, r) and stage_value(field, r) is not None
                                        for field in STAGE_QUESTIONS)]
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
        row = next((r for r in candidates if field_basis(field, r) and stage_value(field, r) is not None), None)
        if row is None:
            continue
        basis = field_basis(field, row)
        data[field] = stage_value(field, row)
        selected_rows[field] = row
        bases.add(basis)
    if bases:
        data['staging_modalities'] = ', '.join(sorted(bases))
    # Preserve source-only historical stage assertions without claiming a TNM basis.
    if 'stage' not in data:
        row = next((r for r in rows if getattr(r, 'observation_source_value', None) == legacy_source), None)
        if row and _coded_value(row):
            data['stage'] = _coded_value(row)
            selected_rows['stage'] = row
    riss = next((r for r in rows if (getattr(r, 'measurement_source_value', None)
        or getattr(r, 'observation_source_value', None)) == '21908-9-riss'), None)
    if riss and _coded_value(riss) and ('stage' not in data or data['stage'].upper().startswith('ISS ')):
        data['stage'] = _coded_value(riss)
        selected_rows['stage'] = riss
    if 'nodes_stage' in data:
        present = stage_presence(data['nodes_stage'], 'N')
        if present is not None:
            data['lymph_node_status'] = 'Positive' if present else 'Negative'
    if 'distant_metastasis_stage' in data:
        present = stage_presence(data['distant_metastasis_stage'], 'M')
        if present is not None:
            data['metastasis_status'] = 'Positive' if present else 'Negative'
    return data
