"""Which PatientRecord fields a client may write, and the OMOP fact to write instead.

`PatientRecord` is a derived read model with no writable clinical columns, so an
editor cannot patch it. It has to write the underlying OMOP fact and let derivation
follow. Doing that needs per-field knowledge the client does not have: which table,
which concept, which unit.

Serving that from the server rather than hardcoding it in the client is deliberate.
`concept_id` is resolved from the vocabulary tables and moves with vocabulary
releases, so a TypeScript copy would drift silently and start writing facts against
stale concepts. It also means a field becomes editable the moment its mapping lands
here — no frontend release.

Coverage is partial and openly reported: most projection fields have no reviewed
concept set yet (see docs/omop_to_patientrecord.md) and are returned as not
writable with a reason, rather than omitted. A client that only sees writable
fields cannot tell "you may not edit this" from "I forgot to send it".
"""

from omop_core.models import Concept, PatientRecord
from omop_core.services.demographics import choices as demographic_choices
from omop_core.services.mappings import (
    CONCEPT_EHR_TYPE, CONCEPT_LAB_TYPE, DERIVED_FIELD_TO_CODE, LAB_FIELD_TO_LOINC,
)
from omop_core.services.patient_record_service import (
    PATIENT_RECORD_OMOP_MAPPED_FIELDS,
    _LAB_FIELD_ALIASES,
)

# Every field resolves to exactly one kind. A binary writable/not left a third of
# the record looking broken — a field is not "unwritable" because it is a unit
# picker or because it is height and weight multiplied together.
_NO_MAPPING_REASON = (
    'No reviewed concept set for this field yet — it cannot be written as a '
    'complete OMOP fact. See docs/omop_to_patientrecord.md.'
)

KIND_EDITABLE = 'editable'      # write an OMOP fact; derivation follows
KIND_SELECTABLE = 'selectable'  # choose from a bounded set, carried on the fact
KIND_COMPUTED = 'computed'      # derived from other fields; never authored alone
KIND_ALIAS = 'alias'            # mirrors a canonical field; edit that one instead
KIND_PROFILE = 'profile'        # a Person attribute, written at the persons endpoint
KIND_UNMAPPED = 'unmapped'      # no write path yet — grouped by WHY, not lumped
KIND_AUTHORED = 'authored'      # written by authoring a different resource entirely

# Why a field has no write path. Reported rather than omitted so the descriptor
# documents the whole record: a reader can see every column and what stands
# between it and being editable, instead of inferring it from an absence.
GROUP_THERAPY = 'therapy-inference'
GROUP_WEARABLE_META = 'wearable-metadata'
GROUP_NEEDS_CONCEPT = 'needs-concept-set'

# Written at the persons endpoint, which upserts the OMOP Location row that
# Person.location points at. The projection name and the CDM column differ for
# two of them, so the descriptor reports the column the write lands in.
_PROFILE_LOCATION = {
    'city': 'Location.city',
    'region': 'Location.state',
    'postal_code': 'Location.zip',
    'country': 'Location.country',
    'latitude': 'Location.latitude',
    'longitude': 'Location.longitude',
}

# Values inferred across many DrugExposure/Episode rows by regimen detection.
# There is no single fact to write: authoring one means writing a therapy
# episode, which is a different endpoint and a different design.
_THERAPY_PREFIXES = (
    'first_line', 'second_line', 'later_', 'supportive_', 'prior_therapy',
    'therapy_', 'line_of_therapy', 'planned_', 'relapse_',
    'treatment_refractory', 'reason_for_disc', 'washout', 'last_treatment',
)

# How a therapy line is authored. Not a missing mapping — the write path exists
# and works; it is simply not a single fact, so no concept could describe it. A
# line is an Episode grouping the DrugExposures given during it, and derivation
# reads that back into every first_line_*/second_line_*/later_* field.
_THERAPY_RECIPE = {
    'target': 'episode',
    'endpoint': 'POST /api/v1/episodes/',
    'steps': [
        'POST /api/v1/drug-exposures/ for each drug given in the line',
        'POST /api/v1/episodes/ with episode_concept=32531 (Treatment Regimen), '
        'episode_number=<line number>, and the line start/end dates',
        'POST /api/v1/episode-events/ linking each drug_exposure_id to the '
        'episode with episode_event_field_concept=1147094',
    ],
    # Optional but worth setting: it makes the regimen an asserted fact rather
    # than one inferred from the drug set, and is what populates *_therapy_id.
    'asserted_regimen_field': 'episode_source_concept',
}

_UNMAPPED_GROUP_REASONS = {
    GROUP_WEARABLE_META: (
        'Bookkeeping about the device feed rather than a reading; follows from '
        'ingesting wearable data.'
    ),
    GROUP_NEEDS_CONCEPT: _NO_MAPPING_REASON,
}


def _unmapped_group(field):
    if field.startswith('wearable_'):
        return GROUP_WEARABLE_META
    if field.startswith(_THERAPY_PREFIXES):
        return GROUP_THERAPY
    return GROUP_NEEDS_CONCEPT

# PatientRecord field → the Person field the persons endpoint accepts.
#
# These are not clinical facts and never were: they describe the person, not an
# event, so they have no concept and no date. PATCH /api/v1/persons/{person_id}/
# already writes them and derivation copies them forward.
_PROFILE_REPLACEABLE = {
    'email': 'email',
    'phone_number': 'phone_number',
    'facility_name': 'facility_name',
    'validated': 'validated',
    'validated_by': 'validated_by',
    'validation_date': 'validation_date',
    'suppress_demographics_for_others': 'suppress_demographics_for_others',
}

# Written as a coded answer plus the raw text, both at once: derivation reads the
# concept before the source value, so a correction that set only text would be
# outranked by the concept already stored.
_PROFILE_DEMOGRAPHIC = {
    'gender': ('gender_concept + gender_source_value', 'gender'),
    'race': ('race_concept + race_source_value', 'race'),
    'ethnicity': ('ethnicity_concept + ethnicity_source_value', 'ethnicity'),
}

# Same endpoint, but fill-if-empty: it populates a blank and refuses to clobber an
# existing value. Reported separately because "writable" would be a lie — a
# clinician cannot correct one here, only supply a missing one.
_PROFILE_FILL_IF_EMPTY = {
    'date_of_birth': 'year_of_birth / month_of_birth / day_of_birth',
}

# Thirty-day aggregates over a stream of device readings. A clinician does not
# type a median: the reading is the fact, and the aggregate follows from it.
# Read-only serializer fields whose `source` is another PatientRecord column.
# Same relationship as _ALIAS_TO_CANONICAL, one layer up: the alias exists only in
# the API representation, so the loop over model-backed fields never sees it.
_SERIALIZER_ALIASES = {
    'refractory_status': 'treatment_refractory_status',
}

# SerializerMethodFields. Read-only by construction — DRF has nowhere to write a
# method — and each is assembled at serialization time from data that lives
# elsewhere.
_SERIALIZER_COMPUTED = {
    'age': 'Calculated from the date of birth on the Person record.',
    'name': 'Assembled from the given and family names on the Person record.',
    'person_id': 'The Person identifier this record derives from.',
    'lines_of_therapy': (
        'Counted from the therapy episodes. Author a line as an Episode grouping '
        'its drug exposures and this follows.'
    ),
    'first_line_therapy_display': (
        'Rendered from the first therapy episode; author the episode instead.'
    ),
    'second_line_therapy_display': (
        'Rendered from the second therapy episode; author the episode instead.'
    ),
    'later_therapy_display': (
        'Rendered from the third and later therapy episodes; author the episodes '
        'instead.'
    ),
    'therapy_release_id': (
        'Identifies the regimen-detection release that produced the therapy '
        'fields; set by that process, not by hand.'
    ),
}



_WEARABLE_METRIC = {
    'median_daily_steps_30d': 'steps',
    'activity_trend_30d': 'steps',
    'active_minutes_per_day_30d': 'active_minutes',
    'resting_heart_rate_avg_30d': 'resting_hr',
    'hrv_sdnn_avg_30d': 'hrv_sdnn',
    'hrv_rmssd_avg_30d': 'hrv_rmssd',
    'oxygen_saturation_min_30d': 'spo2',
    'oxygen_saturation_avg_30d': 'spo2',
    'respiratory_rate_avg_30d': 'respiratory_rate',
    'sleep_duration_hours_avg_30d': 'sleep_duration',
    'vo2_max_avg_30d': 'vo2_max',
    'distance_km_per_day_30d': 'distance',
    'walking_speed_avg_30d': 'walking_speed',
    'walking_step_length_avg_30d': 'walking_step_length',
    'walking_double_support_pct_avg_30d': 'walking_double_support_pct',
    'walking_hr_avg_30d': 'walking_hr_avg',
    'flights_climbed_per_day_30d': 'flights_climbed',
    'active_energy_per_day_30d': 'active_energy',
    'basal_energy_per_day_30d': 'basal_energy',
    'body_mass_avg_30d': 'body_mass',
}


# field → the field it mirrors. Writing an alias directly would collide with its
# canonical on the same LOINC row, which is the failure #471 removed.
_ALIAS_TO_CANONICAL = {
    alias: canonical
    for canonical, aliases in _LAB_FIELD_ALIASES.items()
    for alias in aliases
}

# Values computed during derivation from other projected fields, with the inputs
# a UI needs in order to say why the box is not typeable.
_COMPUTED_INPUTS = {
    'bmi': ['height', 'weight'],
    'tnbc_status': [
        'estrogen_receptor_status', 'progesterone_receptor_status', 'her2_status',
    ],
    'tp53_disruption': ['genetic_mutations'],
    'free_light_chain_ratio': ['kappa_flc', 'lambda_flc'],
    'molecular_markers': ['genetic_mutations'],
    'liver_enzyme_levels': [
        'liver_enzyme_levels_ast', 'liver_enzyme_levels_alt',
        'liver_enzyme_levels_alp',
    ],
}

# Lifecycle columns are not clinical data and are never writable regardless of
# mapping state; they are excluded rather than reported as unwritable fields.
_LIFECYCLE_FIELDS = frozenset({
    'id', 'person', 'organization', 'created_at', 'updated_at',
    'derived_at', 'derivation_version', 'user_edited_fields',
})


def _resolve_concept_ids(codes, vocabulary_id):
    """Map concept_code → concept_id for one vocabulary, in a single query."""
    if not codes:
        return {}
    rows = Concept.objects.filter(
        vocabulary_id=vocabulary_id, concept_code__in=list(codes)
    ).values_list('concept_code', 'concept_id')
    return dict(rows)


_FIELD_TYPE_TO_VALUE_KIND = {
    'BooleanField': 'boolean',
    'IntegerField': 'number', 'SmallIntegerField': 'number',
    'BigIntegerField': 'number', 'FloatField': 'number',
    'DecimalField': 'number',
    'DateField': 'date', 'DateTimeField': 'datetime',
}


def _value_kind(field_name):
    """Derive the value kind from the model column rather than restating it.

    Keeps the descriptor honest when a column's type changes: a field that becomes
    numeric stops being advertised as free text without anyone editing a table.
    """
    for f in PatientRecord._meta.fields:
        if f.name == field_name:
            return _FIELD_TYPE_TO_VALUE_KIND.get(type(f).__name__, 'string')
    return 'string'


def _resolve_concepts(pairs):
    """(vocabulary_id, concept_code) → Concept, in one query.

    Scoped by vocabulary because a bare concept_code is ambiguous — codes are
    reused across vocabularies, which is why WEARABLE_CONCEPT_VOCAB exists.
    """
    if not pairs:
        return {}
    codes = {c for _v, c in pairs}
    vocabs = {v for v, _c in pairs}
    rows = Concept.objects.filter(
        concept_code__in=codes, vocabulary_id__in=vocabs
    ).only('concept_id', 'concept_code', 'vocabulary_id', 'domain_id')
    return {(c.vocabulary_id, c.concept_code): c for c in rows}


_MAPPING_TARGETS = {
    'measurement': 'measurement',
    'observation': 'observation',
}


def _curated_writes():
    """Editable entries built from reviewer-approved concept mappings.

    The curation interface records a decision per field; this is what acts on
    it. A row qualifies only when it carries everything a write needs — an
    approved status, a resolved concept, an OMOP table this can write to, and a
    source value for derivation to match on. Anything short of that stays
    advisory rather than becoming a box that writes somewhere unfindable.
    """
    from omop_core.models import FieldConceptMapping

    entries = {}
    rows = list(
        FieldConceptMapping.objects
        .filter(status='approved')
        .exclude(source_value='')
        .exclude(omop_table='')
        .select_related('concept')
    )
    # One read per distinct answer vocabulary, not one per field that uses it.
    # Four eligibility fields sharing a set is one query, the same shape the
    # LOINC and UCUM lookups already have.
    titles = {
        name: _lookup_titles(name)
        for name in {r.value_vocabulary for r in rows if r.value_vocabulary}
    }
    for row in rows:
        target = _MAPPING_TARGETS.get(row.omop_table.strip().lower())
        concept_id = row.concept_id
        if target is None or concept_id is None:
            continue
        entry = {
            'kind': KIND_EDITABLE,
            'writable': True,
            'target': target,
            'concept_id': concept_id,
            'type_concept_id': row.type_concept_id or CONCEPT_LAB_TYPE,
            'source_value': row.source_value,
            'value_kind': row.value_kind or _value_kind(row.field_name),
            'curated': True,
        }
        if row.unit:
            entry['unit'] = row.unit
        if row.value_vocabulary:
            options = titles.get(row.value_vocabulary) or ()
            if options:
                entry['options'] = [{'value': t} for t in options]
                entry['multiple'] = row.multiple
        entries[row.field_name] = entry
    return entries


def _lookup_titles(model_name):
    """Titles from a VocabularyLookup table, or () when it is absent.

    Ingest filters incoming values against exactly these, so an option outside
    the set would promise a write that ingest drops.
    """
    from omop_core import models as _m

    model = getattr(_m, model_name, None)
    if model is None:
        return ()
    return list(model.objects.order_by('title').values_list('title', flat=True))


def build_writable_field_descriptor():
    """Return {field: descriptor} for every mapped PatientRecord clinical field.

    Query cost is flat: two lookups total, not one per field.
    """
    loinc_ids = _resolve_concept_ids(
        {code for code, _unit, _display in LAB_FIELD_TO_LOINC.values()}, 'LOINC'
    )
    unit_ids = _resolve_concept_ids(
        {unit for _code, unit, _display in LAB_FIELD_TO_LOINC.values()}, 'UCUM'
    )
    derived_concepts = _resolve_concepts(
        {(vocab, code) for code, vocab, _fn in DERIVED_FIELD_TO_CODE.values()}
    )

    curated = _curated_writes()

    descriptor = {}
    for field in sorted(PATIENT_RECORD_OMOP_MAPPED_FIELDS - _LIFECYCLE_FIELDS):
        if field in _ALIAS_TO_CANONICAL:
            canonical = _ALIAS_TO_CANONICAL[field]
            descriptor[field] = {
                'kind': KIND_ALIAS,
                'writable': False,
                'canonical': canonical,
                'reason': f'Mirrors {canonical}; edit that field instead.',
            }
            continue

        if field in _PROFILE_DEMOGRAPHIC:
            person_field, kind = _PROFILE_DEMOGRAPHIC[field]
            descriptor[field] = {
                'kind': KIND_PROFILE,
                'writable': True,
                'target': 'person',
                'endpoint': 'PATCH /api/v1/persons/{person_id}/',
                'person_field': person_field,
                # What to actually send. `person_field` documents the Person
                # columns behind this and reads as prose ("gender_concept +
                # gender_source_value", "Location.city"); the endpoint keys every
                # profile field on the PatientRecord field name and resolves the
                # rest itself. A client guessing from the prose would send neither.
                'payload_field': field,
                'value_kind': 'string',
                # A curated set, not the whole vocabulary: OMOP's Race holds 1,409
                # concepts and Ethnicity 150 nationality-style entries, which is
                # not the question a clinical form asks. Anything sent is still
                # preserved verbatim in the source value.
                'options': [
                    {'value': display, 'code': code}
                    for code, display in demographic_choices(kind)
                ],
            }
            continue

        if field in _PROFILE_LOCATION:
            descriptor[field] = {
                'kind': KIND_PROFILE,
                'writable': True,
                'target': 'person',
                'endpoint': 'PATCH /api/v1/persons/{person_id}/',
                'person_field': _PROFILE_LOCATION[field],
                'payload_field': field,
                'value_kind': _value_kind(field),
            }
            continue

        if field in _PROFILE_REPLACEABLE:
            descriptor[field] = {
                'kind': KIND_PROFILE,
                'writable': True,
                'target': 'person',
                'endpoint': 'PATCH /api/v1/persons/{person_id}/',
                'person_field': _PROFILE_REPLACEABLE[field],
                'payload_field': field,
                'value_kind': _value_kind(field),
            }
            continue

        if field in _PROFILE_FILL_IF_EMPTY:
            descriptor[field] = {
                'kind': KIND_PROFILE,
                # Not writable in the sense the editor means. The endpoint fills a
                # blank and silently leaves an existing value alone, so offering a
                # box that appears to accept a correction would lie about the
                # outcome — the save would succeed and change nothing.
                'writable': False,
                'fill_if_empty': True,
                'target': 'person',
                'endpoint': 'PATCH /api/v1/persons/{person_id}/',
                'person_field': _PROFILE_FILL_IF_EMPTY[field],
                'payload_field': field,
                'value_kind': _value_kind(field),
                'reason': (
                    'Set on the Person record, and only while it is empty — this '
                    'endpoint never overwrites an existing value.'
                ),
            }
            continue

        if field in curated:
            descriptor[field] = curated[field]
            continue

        if field in _WEARABLE_METRIC:
            metric = _WEARABLE_METRIC[field]
            descriptor[field] = {
                'kind': KIND_COMPUTED,
                'writable': False,
                'inputs': [metric],
                'window_days': 30,
                'reason': (
                    f'A 30-day aggregate of {metric} readings. Upload device data '
                    'rather than entering a summary value.'
                ),
            }
            continue

        if field in _COMPUTED_INPUTS:
            descriptor[field] = {
                'kind': KIND_COMPUTED,
                'writable': False,
                'inputs': _COMPUTED_INPUTS[field],
                'reason': (
                    'Computed from ' + ', '.join(_COMPUTED_INPUTS[field]) + '.'
                ),
            }
            continue

        if field.endswith('_units'):
            # A unit is not a fact of its own — it is the unit_concept carried on
            # the measurement whose value it qualifies. The picker belongs beside
            # that value, and selecting one rewrites the fact, not this column.
            descriptor[field] = {
                'kind': KIND_SELECTABLE,
                'writable': False,
                'qualifies': field[: -len('_units')],
                'reason': (
                    f'Unit of {field[: -len("_units")]}; selected alongside that '
                    'value and stored on the measurement.'
                ),
            }
            continue

        derived = DERIVED_FIELD_TO_CODE.get(field)
        if derived is not None:
            code, vocabulary, extractor = derived
            concept = derived_concepts.get((vocabulary, code))
            if concept is None:
                descriptor[field] = {
                    'kind': KIND_EDITABLE,
                    'writable': False,
                    'reason': (
                        f'{vocabulary} {code} is not loaded in this deployment\'s '
                        'vocabulary.'
                    ),
                    'code': code,
                    'vocabulary': vocabulary,
                    'attributed_from': extractor,
                }
                continue
            descriptor[field] = {
                'kind': KIND_EDITABLE,
                'writable': True,
                # From the concept's own domain, not a guess: a code that moves
                # domain in a vocabulary release moves table with it.
                'target': (
                    'observation' if concept.domain_id == 'Observation'
                    else 'measurement'
                ),
                'concept_id': concept.concept_id,
                'code': code,
                'vocabulary': vocabulary,
                'display': concept.concept_name,
                'value_kind': _value_kind(field),
                'unit': None,
                'unit_concept_id': None,
                'type_concept_id': CONCEPT_LAB_TYPE,
                'source_value': code,
                # Provenance for review: the extractor this attribution came from.
                'attributed_from': extractor,
            }
            continue

        mapping = LAB_FIELD_TO_LOINC.get(field)
        if mapping is None:
            group = _unmapped_group(field)
            if group == GROUP_THERAPY:
                # These are authored, not unmapped. Reporting them as unmapped
                # reads as "nothing you can do", when in fact the write path
                # exists and derivation already reads it back.
                descriptor[field] = {
                    'kind': KIND_AUTHORED,
                    'writable': False,
                    'group': group,
                    'authored_via': _THERAPY_RECIPE,
                    'reason': (
                        'Derived from the therapy episodes, not from one fact. '
                        'Author a line as an Episode grouping its drug exposures '
                        'and this field follows.'
                    ),
                }
                continue
            descriptor[field] = {
                'kind': KIND_UNMAPPED,
                'writable': False,
                'group': group,
                'reason': _UNMAPPED_GROUP_REASONS[group],
            }
            continue

        code, unit, display = mapping
        concept_id = loinc_ids.get(code)
        if concept_id is None:
            # The mapping exists but this deployment's vocabulary does not carry
            # the concept. Writing the fact would strand it against a concept that
            # cannot be resolved, so report it as not writable here rather than
            # letting the client discover it as a failed write.
            descriptor[field] = {
                'kind': KIND_EDITABLE,
                'writable': False,
                'reason': (
                    f'LOINC {code} is not loaded in this deployment\'s vocabulary.'
                ),
                'code': code,
                'vocabulary': 'LOINC',
            }
            continue

        descriptor[field] = {
            'kind': KIND_EDITABLE,
            'writable': True,
            'target': 'measurement',
            'concept_id': concept_id,
            'code': code,
            'vocabulary': 'LOINC',
            'display': display,
            'value_kind': 'number',
            'unit': unit,
            'unit_concept_id': unit_ids.get(unit),
            'type_concept_id': CONCEPT_LAB_TYPE,
            'source_value': code,
        }

    # Fields the serializer adds that no PatientRecord column backs.
    #
    # The loop above walks PATIENT_RECORD_OMOP_MAPPED_FIELDS, so it can only
    # describe columns. These are SerializerMethodFields and read-only aliases,
    # which means an editor asking "may I write this?" got no entry at all and
    # had to guess — and guessing "yes" is how a select over `refractory_status`
    # came to be offered on the treatment tab for a value derived from therapy
    # episodes. Every one of them is read-only server-side, so describing them
    # closes the gap for every client rather than one tab.
    #
    # patient_name is deliberately absent: it is popped and applied to Person
    # before the serializer sees it, so it really is writable on that endpoint,
    # and marking it read-only here would stop renames.
    for alias, canonical in _SERIALIZER_ALIASES.items():
        descriptor.setdefault(alias, {
            'kind': KIND_ALIAS,
            'writable': False,
            'canonical': canonical,
            'reason': f'Mirrors {canonical}; edit that field instead.',
        })

    for computed, reason in _SERIALIZER_COMPUTED.items():
        descriptor.setdefault(computed, {
            'kind': KIND_COMPUTED,
            'writable': False,
            'reason': reason,
        })

    return descriptor
