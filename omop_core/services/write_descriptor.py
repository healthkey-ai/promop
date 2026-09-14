"""Which PatientRecord fields a client may write and how they project to OMOP.

User-entered values land on `PatientRecord` first. When a reviewed mapping exists,
the same PATCH also projects an OMOP fact with its table, concept, type, source and
unit metadata. Computed projections remain read-only and direct fields without a
complete mapping remain safely editable on PatientRecord.

Serving that from the server rather than hardcoding it in the client is deliberate.
`concept_id` is resolved from the vocabulary tables and moves with vocabulary
releases, so a TypeScript copy would drift silently and start writing facts against
stale concepts. It also means a field becomes editable the moment its mapping lands
here — no frontend release.

Coverage is openly reported: every projection field is classified, including
computed values, aliases and values authored through a dedicated resource.
"""

from omop_core.models import (
    Concept, ConditionOccurrence, DrugExposure, FieldChoice, Measurement,
    Observation, PatientRecord, ProcedureOccurrence,
)
from omop_core.services.demographics import choices as demographic_choices
from omop_core.services.mappings import (
    CONCEPT_EHR_TYPE, CONCEPT_LAB_TYPE, CONCEPT_PATIENT_REPORTED_TYPE,
    DERIVED_FIELD_TO_CODE, LAB_FIELD_TO_LOINC,
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
    'complete OMOP fact. See field_to_concept_mapping.md.'
)

KIND_EDITABLE = 'editable'      # write an OMOP fact; derivation follows
KIND_SELECTABLE = 'selectable'  # choose from a bounded set, carried on the fact
KIND_COMPUTED = 'computed'      # derived from other fields; never authored alone
KIND_ALIAS = 'alias'            # mirrors a canonical field; edit that one instead
KIND_PROFILE = 'profile'        # a Person attribute, written at the persons endpoint
KIND_UNMAPPED = 'unmapped'      # no write path yet — grouped by WHY, not lumped
KIND_AUTHORED = 'authored'      # written by authoring a different resource entirely
KIND_DIRECT = 'direct'          # write to PatientRecord; no OMOP fact required yet

# Athena Cancer Modifier: Dimension of Largest Lymph Node.  Unlike the generic
# Size Tumor LOINC, this is the specific standard concept for the CLL field.
CONCEPT_LARGEST_LYMPH_NODE_DIMENSION = 36769292

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

# Fields that match a therapy prefix but are user-entered data, not episode-derived.
# These should fall through to KIND_DIRECT rather than KIND_AUTHORED.
_THERAPY_PREFIX_EXCEPTIONS = frozenset({
    'planned_therapies', 'relapse_count', 'treatment_refractory_status',
    'supportive_therapies',
    'supportive_therapy_start_date',
    'supportive_therapy_end_date',
    'supportive_therapy_intent',
})

# Line projections are not writable PatientRecord facts.  ARTEMIS (or another
# episode producer) writes the Episode/EpisodeEvent evidence; refresh reads it
# back.  This list deliberately includes every current first/second/later line
# column, rather than only the therapy name, so the UI cannot offer a stale
# direct editor for a date, intent, outcome, or derived concept id.
_EPISODE_COMPUTED_FIELDS = frozenset(
    field.name
    for field in PatientRecord._meta.concrete_fields
    if field.name.startswith(('first_line_', 'second_line_', 'later_'))
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
    if field.startswith(_THERAPY_PREFIXES) and field not in _THERAPY_PREFIX_EXCEPTIONS:
        return GROUP_THERAPY
    return GROUP_NEEDS_CONCEPT


def _field_choice_options() -> dict[str, list[tuple[str, str | None]]]:
    """Return curator-managed displays and their preferred code for every field."""
    result: dict[str, list[tuple[str, str | None]]] = {}
    for choice in FieldChoice.objects.prefetch_related('codes').all():
        primary = next((code.code for code in choice.codes.all() if code.is_primary), None)
        result.setdefault(choice.field_name, []).append((choice.display, primary))
    return result

# PatientRecord field → the Person field the persons endpoint accepts.
#
# These are not clinical facts and never were: they describe the person, not an
# event, so they have no concept and no date. PATCH /api/v1/persons/{person_id}/
# already writes them and derivation copies them forward.
_PROFILE_REPLACEABLE = {
    'date_of_birth': 'year_of_birth / month_of_birth / day_of_birth / birth_datetime',
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
    'supportive_therapy_courses': 'Entered supportive treatment courses; edit through the supportive-therapies endpoint.',
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
    'gelf_criteria_status': ['gelf_criteria_options'],
    'flipi_score': ['flipi_score_options'],
    'flipi_risk_category': ['flipi_score_options'],
    'bmi': ['height', 'weight'],
    'tnbc_status': [
        'estrogen_receptor_status', 'progesterone_receptor_status', 'her2_status',
    ],
    'tp53_disruption': ['genetic_mutations'],
    'involved_uninvolved_ratio': ['kappa_flc', 'lambda_flc'],
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
    'derived_at', 'derivation_version', 'user_edited_fields', 'therapy_overrides',
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
    'condition': 'condition',
    'condition_occurrence': 'condition',
    'drug': 'drug_exposure',
    'drug_exposure': 'drug_exposure',
    'procedure': 'procedure',
    'procedure_occurrence': 'procedure',
}

_MAPPING_ENDPOINTS = {
    'measurement': 'POST /api/v1/measurements/',
    'observation': 'POST /api/v1/observations/',
    'condition': 'POST /api/v1/conditions/',
    'drug_exposure': 'POST /api/v1/drug-exposures/',
    'procedure': 'POST /api/v1/procedures/',
}

_MAPPING_SOURCE_FIELDS = {
    'measurement': Measurement._meta.get_field('measurement_source_value'),
    'observation': Observation._meta.get_field('observation_source_value'),
    'condition': ConditionOccurrence._meta.get_field('condition_source_value'),
    'drug_exposure': DrugExposure._meta.get_field('drug_source_value'),
    'procedure': ProcedureOccurrence._meta.get_field('procedure_source_value'),
}


def mapping_target_for(omop_table):
    return _MAPPING_TARGETS.get((omop_table or '').strip().lower())


def mapping_table_is_writable(omop_table):
    return mapping_target_for(omop_table) is not None


# Fields whose write recipe this cannot express, so they are reported read-only
# rather than offered.
#
# Found by writing every writable field and reading it back (#666). A descriptor
# entry is a claim that a value written to its recipe comes back; where the
# recipe is incomplete the claim is false, and a box that accepts input and
# silently drops it is worse than one that says why it is disabled.
_WRITE_RECIPE_INCOMPLETE = {
    'bone_only_metastasis_status': (
        'Derivation looks for an Observation whose concept name contains "bone '
        'only metastas", while the mapping here names a Measurement with a '
        'different concept. A write against it lands in the wrong table under '
        'the wrong concept and is not read back.'
    ),
}

def _curated_writes(choice_options=None):
    """Editable entries built from reviewer-approved concept mappings.

    The curation interface records a decision per field; this is what acts on
    it.  A row qualifies when it carries an approved status, a resolved concept,
    and an OMOP table.  The source value for derivation to match on defaults to
    the concept's own code when the curator hasn't set an explicit override —
    which is the common case for LOINC and SNOMED mappings.

    All fields write to PatientRecord (KIND_DIRECT, target='patient_record').
    Fields with a complete approved mapping carry a ``projection`` key with
    the OMOP routing info needed to project the value into a clinical table
    after the PATCH lands.
    """
    from omop_core.models import FieldConceptMapping

    entries = {}
    rows = list(
        FieldConceptMapping.objects
        .filter(status='approved')
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
    unit_ids = _resolve_concept_ids({row.unit for row in rows if row.unit}, 'UCUM')
    for row in rows:
        target = mapping_target_for(row.omop_table)
        concept_id = row.concept_id
        if target is None or concept_id is None:
            continue
        # Fall back to the concept's own code when the curator hasn't set an
        # explicit source_value.  Every hardcoded LOINC and DERIVED mapping
        # already uses the concept code; curated rows should work the same way.
        source_value = row.source_value or (
            row.concept.concept_code if row.concept else None
        )
        if not source_value:
            continue
        source_field = _MAPPING_SOURCE_FIELDS[target]
        width = source_field.max_length
        if width is not None and len(source_value) > width:
            # Preserve the curator's key: truncating it would break derivation's
            # exact source-value match. Keep an entry so no fallback write recipe
            # can hide this invalid approved mapping.
            entries[row.field_name] = {
                'kind': KIND_DIRECT,
                'writable': False,
                'curated': True,
                'reason': (
                    f'Approved mapping source_value exceeds the {width}-character '
                    f'limit of {source_field.model._meta.db_table}.{source_field.name}. '
                    'Shorten the mapping source_value before writing this field.'
                ),
            }
            continue
        type_concept_id = row.type_concept_id or CONCEPT_EHR_TYPE
        entry = {
            'kind': KIND_DIRECT,
            'writable': True,
            'target': 'patient_record',
            'value_kind': row.value_kind or _value_kind(row.field_name),
            'curated': True,
            'projection': {
                'omop_table': target,
                'endpoint': _MAPPING_ENDPOINTS[target],
                'concept_id': concept_id,
                'type_concept_id': type_concept_id,
                'source_value': source_value,
            },
        }
        if row.unit:
            entry['unit'] = row.unit
            entry['projection']['unit'] = row.unit
            entry['projection']['unit_concept_id'] = unit_ids.get(row.unit)
        if row.value_vocabulary:
            options = titles.get(row.value_vocabulary) or ()
            if options:
                entry['options'] = [{'value': t} for t in options]
                entry['multiple'] = row.multiple
        elif choice_options and row.field_name in choice_options:
            entry['options'] = choice_options[row.field_name]
            entry['multiple'] = row.multiple
        if (row.field_name == 'cytogenetic_markers' and row.multiple
                and row.vocabulary_id == 'SNOMED' and row.concept_code == '107675007'):
            from omop_core.services.cytogenetics import descriptor as cytogenetic_descriptor
            entry = cytogenetic_descriptor(mapping_approved=True)
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


# Process-lifetime cache.  Safe because inputs are static (model metadata +
# constant dicts, no database queries).  If a database-dependent rule is ever
# added, this must be replaced with per-request computation.
_CACHED_SERIALIZER_READ_ONLY: frozenset | None = None


def get_serializer_read_only_fields():
    """Field names that must be read-only on PatientRecordSerializer.

    Computable without database access from the field classification rules.
    A field is read-only here when its write target is not PatientRecord
    (profile fields target Person) or when it is computed/derived/authored.

    The result depends only on model metadata and static dicts, so it is
    computed once and cached for the lifetime of the process.
    """
    global _CACHED_SERIALIZER_READ_ONLY  # noqa: PLW0603
    if _CACHED_SERIALIZER_READ_ONLY is not None:
        return _CACHED_SERIALIZER_READ_ONLY

    read_only = set()
    # KIND_COMPUTED: episode-derived line fields
    read_only |= _EPISODE_COMPUTED_FIELDS
    # KIND_COMPUTED: wearable aggregates + KIND_UNMAPPED: wearable metadata
    read_only |= set(_WEARABLE_METRIC)
    read_only.add('wearable_coverage_ratio_30d')
    for field in PatientRecord._meta.concrete_fields:
        if field.name.startswith('wearable_') and field.name not in _WEARABLE_METRIC:
            read_only.add(field.name)
    # KIND_COMPUTED: derived from other fields (BMI, tnbc_status, etc.)
    read_only |= set(_COMPUTED_INPUTS)
    # KIND_ALIAS: mirrors of canonical fields
    read_only |= set(_ALIAS_TO_CANONICAL)
    # Profile fields now write through PatientRecord PATCH (KIND_DIRECT),
    # so they are no longer read-only on the serializer.
    # KIND_AUTHORED / KIND_UNMAPPED: therapy-prefix fields (minus exceptions)
    for field in PatientRecord._meta.concrete_fields:
        name = field.name
        if (name.startswith(_THERAPY_PREFIXES)
                and name not in _THERAPY_PREFIX_EXCEPTIONS
                and name not in _EPISODE_COMPUTED_FIELDS):
            read_only.add(name)
    # KIND_SELECTABLE: unit fields
    for field in PatientRecord._meta.concrete_fields:
        if field.name.endswith('_units'):
            read_only.add(field.name)
    read_only -= {'relapse_count', 'treatment_refractory_status'}
    _CACHED_SERIALIZER_READ_ONLY = frozenset(read_only)
    return _CACHED_SERIALIZER_READ_ONLY


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
    largest_lymph_node_concept = Concept.objects.filter(
        concept_id=CONCEPT_LARGEST_LYMPH_NODE_DIMENSION
    ).only('concept_id', 'concept_code', 'concept_name', 'vocabulary_id').first()

    choice_options = {
        field_name: [
            {'value': display, 'code': primary_code}
            for display, primary_code in choices
        ]
        for field_name, choices in _field_choice_options().items()
    }
    curated = _curated_writes(choice_options)

    descriptor = {}
    for field in sorted(PATIENT_RECORD_OMOP_MAPPED_FIELDS - _LIFECYCLE_FIELDS):
        if field.startswith('genomics_'):
            mapping = curated.get(field, {})
            writable = (mapping.get('writable', False) and mapping.get('value_kind') == 'json'
                        and mapping.get('projection', {}).get('omop_table') == 'measurement')
            descriptor[field] = {
                'kind': KIND_EDITABLE, 'writable': bool(writable), 'target': 'patient_record',
                'reason': 'Priority findings write through approved Genomics mappings.',
            }
            continue
        if field == 'cytogenetic_markers':
            descriptor[field] = {
                'kind': KIND_COMPUTED, 'writable': False, 'target': 'genomics',
                'reason': 'Legacy cytogenetic summary is read-only. Record individual findings in Genomics.',
            }
            continue
        if field == 'genetic_mutations':
            descriptor[field] = {
                'kind': KIND_EDITABLE, 'writable': True, 'target': 'genomics',
                'endpoint': '/api/v1/patient-records/{person_id}/genomics/',
                'reason': 'Edit individual variants in the Genomics tab; each variant writes linked OMOP facts.',
            }
            continue
        if field in _EPISODE_COMPUTED_FIELDS:
            descriptor[field] = {
                'kind': KIND_COMPUTED,
                'writable': False,
                'inputs': ['Episode', 'EpisodeEvent'],
                'source_tables': ['Episode', 'EpisodeEvent'],
                'authored_via': _THERAPY_RECIPE,
                'reason': (
                    'Code-computed from persisted Episode and EpisodeEvent '
                    'records. Author a therapy line as an Episode grouping its '
                    'events and this field follows.'
                ),
            }
            continue
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
                'kind': KIND_DIRECT,
                'writable': True,
                'target': 'patient_record',
                'projection_target': 'person',
                'person_field': person_field,
                'payload_field': field,
                'value_kind': 'string',
                'options': [
                    {'value': display, 'code': code}
                    for code, display in demographic_choices(kind)
                ],
            }
            continue

        if field in _PROFILE_LOCATION:
            descriptor[field] = {
                'kind': KIND_DIRECT,
                'writable': True,
                'target': 'patient_record',
                'projection_target': 'location',
                'person_field': _PROFILE_LOCATION[field],
                'payload_field': field,
                'value_kind': _value_kind(field),
            }
            continue

        if field in _PROFILE_REPLACEABLE:
            descriptor[field] = {
                'kind': KIND_DIRECT,
                'writable': True,
                'target': 'patient_record',
                'projection_target': 'person',
                'person_field': _PROFILE_REPLACEABLE[field],
                'payload_field': field,
                'value_kind': _value_kind(field),
            }
            continue

        if field == 'wearable_coverage_ratio_30d':
            descriptor[field] = {
                'kind': KIND_COMPUTED,
                'writable': False,
                'inputs': sorted(set(_WEARABLE_METRIC.values())),
                'window_days': 30,
                'reason': (
                    'Proportion of the 30-day window with any valid wearable '
                    'reading, counting each day once across all device metrics.'
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

        if field == 'largest_lymph_node_size':
            if largest_lymph_node_concept is None:
                descriptor[field] = {
                    'kind': KIND_DIRECT,
                    'writable': True,
                    'target': 'patient_record',
                    'value_kind': 'number',
                    'reason': (
                        'Written directly to PatientRecord. Cancer Modifier '
                        '36769292 (Dimension of Largest Lymph Node) is not '
                        'loaded — no OMOP projection available.'
                    ),
                }
                continue
            descriptor[field] = {
                'kind': KIND_DIRECT,
                'writable': True,
                'target': 'patient_record',
                'value_kind': 'number',
                'unit': 'cm',
                'projection': {
                    'omop_table': 'measurement',
                    'concept_id': largest_lymph_node_concept.concept_id,
                    'code': largest_lymph_node_concept.concept_code,
                    'vocabulary': largest_lymph_node_concept.vocabulary_id,
                    'display': largest_lymph_node_concept.concept_name,
                    'unit': 'cm',
                    'unit_concept_id': unit_ids.get('cm'),
                    'type_concept_id': CONCEPT_PATIENT_REPORTED_TYPE,
                    'source_value': largest_lymph_node_concept.concept_code,
                },
                'attributed_from': '_get_cll_data',
            }
            continue

        derived = DERIVED_FIELD_TO_CODE.get(field)
        if derived is not None:
            code, vocabulary, extractor = derived
            concept = derived_concepts.get((vocabulary, code))
            if concept is None:
                descriptor[field] = {
                    'kind': KIND_DIRECT,
                    'writable': True,
                    'target': 'patient_record',
                    'value_kind': _value_kind(field),
                    'reason': (
                        f'Written directly to PatientRecord. {vocabulary} {code} '
                        f'is not loaded — no OMOP projection available.'
                    ),
                    'attributed_from': extractor,
                }
                continue
            omop_table = (
                'observation' if concept.domain_id == 'Observation'
                else 'measurement'
            )
            descriptor[field] = {
                'kind': KIND_DIRECT,
                'writable': True,
                'target': 'patient_record',
                'value_kind': _value_kind(field),
                'projection': {
                    'omop_table': omop_table,
                    'concept_id': concept.concept_id,
                    'code': code,
                    'vocabulary': vocabulary,
                    'display': concept.concept_name,
                    'unit': None,
                    'unit_concept_id': None,
                    'type_concept_id': CONCEPT_LAB_TYPE,
                    'source_value': code,
                },
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
            if group == GROUP_NEEDS_CONCEPT:
                # No OMOP mapping yet, but the field is directly writable on
                # PatientRecord.  User edits land there and are preserved
                # across derivation until a FieldConceptMapping projects
                # them into an OMOP table.
                entry = {
                    'kind': KIND_DIRECT,
                    'writable': True,
                    'target': 'patient_record',
                    'value_kind': _value_kind(field),
                    'reason': 'Written directly to PatientRecord. No OMOP mapping yet.',
                }
                opts = choice_options.get(field, [])
                if opts:
                    entry['options'] = opts
                descriptor[field] = entry
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
            # The mapping exists but the vocabulary doesn't carry the concept.
            # Still directly writable on PatientRecord; no OMOP projection.
            descriptor[field] = {
                'kind': KIND_DIRECT,
                'writable': True,
                'target': 'patient_record',
                'value_kind': 'number',
                'reason': (
                    f'Written directly to PatientRecord. LOINC {code} is not '
                    f'loaded — no OMOP projection available.'
                ),
            }
            continue

        descriptor[field] = {
            'kind': KIND_DIRECT,
            'writable': True,
            'target': 'patient_record',
            'value_kind': 'number',
            'unit': unit,
            'projection': {
                'omop_table': 'measurement',
                'concept_id': concept_id,
                'code': code,
                'vocabulary': 'LOINC',
                'display': display,
                'unit': unit,
                'unit_concept_id': unit_ids.get(unit),
                # This descriptor drives a clinician/patient edit, not a lab
                # import. Keeping it distinct preserves same-day imported facts.
                'type_concept_id': CONCEPT_PATIENT_REPORTED_TYPE,
                'source_value': code,
            },
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

    # Fields whose OMOP write recipe is broken — the curated mapping writes to
    # the wrong table/concept.  They are still directly writable on PatientRecord;
    # the OMOP recipe is just not usable for clinical-fact writes.
    for field, reason in _WRITE_RECIPE_INCOMPLETE.items():
        if field in descriptor:
            descriptor[field] = {
                'kind': KIND_DIRECT,
                'writable': True,
                'target': 'patient_record',
                'value_kind': _value_kind(field),
                'reason': f'Written directly to PatientRecord. OMOP recipe issue: {reason}',
            }

    for computed, reason in _SERIALIZER_COMPUTED.items():
        descriptor.setdefault(computed, {
            'kind': KIND_COMPUTED,
            'writable': False,
            'reason': reason,
        })

    from omop_core.services.treatment_catalog import REFRACTORY_STATUSES
    for field in ('relapse_count', 'treatment_refractory_status', 'refractory_status', 'death_date'):
        descriptor[field] = {
            'kind': KIND_DIRECT, 'writable': True, 'target': 'patient_record',
            'value_kind': 'number' if field == 'relapse_count' else 'date' if field == 'death_date' else 'string',
        }
        if field in ('treatment_refractory_status', 'refractory_status'):
            descriptor[field]['options'] = [{'value': value} for value in REFRACTORY_STATUSES]
    descriptor['refractory_status']['canonical'] = 'treatment_refractory_status'
    for field in ('relapse_count', 'treatment_refractory_status', 'death_date'):
        descriptor[field]['reason'] = 'Inferred by default; enter a value to override, or clear to use the inferred value.'
        descriptor[field]['projection'] = {
            'omop_table': 'observation', 'concept_id': 0,
            'type_concept_id': CONCEPT_EHR_TYPE,
            'source_value': 'patient-record:' + field,
        }
    descriptor['death_date']['reason'] = 'Corrections are dated OMOP observations; earlier facts remain as history.'
    return descriptor
