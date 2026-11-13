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

from decimal import Decimal, InvalidOperation

from django.core.exceptions import FieldDoesNotExist
from django.db import models as django_models
from django.db.models import Q

from rest_framework.fields import BooleanField

from omop_core.models import (
    Concept, ConditionOccurrence, DrugExposure, FieldChoice, Measurement,
    Observation, PatientRecord, PreExistingConditionCategory, ProcedureOccurrence,
)
from omop_core.services.boolean_answers import BOOLEAN_ANSWERS
from omop_core.services.demographics import choices as demographic_choices
from omop_core.services.mappings import (
    CONCEPT_EHR_TYPE, CONCEPT_LAB_TYPE, CONCEPT_PATIENT_REPORTED_TYPE,
    DERIVED_FIELD_TO_CODE, FIELD_COMMON_UNITS, LAB_FIELD_TO_LOINC,
)
from omop_core.services.patient_record_service import (
    DERIVED_LANGUAGE_FIELDS,
    PATIENT_RECORD_OMOP_MAPPED_FIELDS,
    _LAB_FIELD_ALIASES,
)
from omop_core.services.blood_count_source_pairs import (
    PAIRS as COUNT_SOURCE_PAIRS,
    UNIT_OPTIONS as COUNT_UNIT_OPTIONS,
)
from omop_core.services.chemistry_units import PAIRS as CHEMISTRY_SOURCE_PAIRS
from omop_core.services import direct_bilirubin_units

_NULLABLE_BOOLEAN = BooleanField(allow_null=True)

# Every field resolves to exactly one kind. A binary writable/not left a third of
# the record looking broken — a field is not "unwritable" because it is a unit
# picker or because it is height and weight multiplied together.
_NO_MAPPING_REASON = (
    'No reviewed concept set for this field yet — it cannot be written as a '
    'complete OMOP fact. See field_concept_mapping_architecture.md.'
)

KIND_EDITABLE = 'editable'      # write an OMOP fact; derivation follows
KIND_SELECTABLE = 'selectable'  # choose from a bounded set, carried on the fact
KIND_COMPUTED = 'computed'      # derived from other fields; never authored alone
KIND_ALIAS = 'alias'            # mirrors a canonical field; edit that one instead
KIND_NEGATED = 'negated'        # the opposite of a canonical boolean; write it inverted
KIND_EXPLICIT_ANSWER = 'explicit-answer'  # its own endpoint and snapshot; never a PATCH
KIND_PROFILE = 'profile'        # a Person attribute, written at the persons endpoint
KIND_UNMAPPED = 'unmapped'      # no write path yet — grouped by WHY, not lumped
KIND_AUTHORED = 'authored'      # written by authoring a different resource entirely
KIND_DIRECT = 'direct'          # write to PatientRecord; no OMOP fact required yet

#: Bumped only when a client-visible MEANING changes: a new kind, a renamed
#: capability key, different `writable`/`answerable` semantics, or a new write
#: or readback path. Adding a field, an option or a binding does NOT bump it.
#: A client that does not know this version must not guess: see
#: docs/writable-field-descriptor.md.
DESCRIPTOR_SCHEMA_VERSION = 1

#: Reserved key for the descriptor's own envelope. It is not a field: clients
#: look fields up by name, so it never collides with one, and iterators must
#: skip keys beginning with `__`.
DESCRIPTOR_META_KEY = '__descriptor__'

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

#: Flat shared-form fields that author one therapy line each. They stay
#: projections of an Episode, so the descriptor offers them as writable and the
#: write path routes them to the line authoring service instead of the column
#: (`omop_core/services/therapy_line_form.py`). Only the first line of each group
#: is flat: derivation reads `later_therapy`/`later_date`/`later_outcome` back
#: from the first 3L+ line, so a second flat later line would have no column to
#: land in.
_AUTHORABLE_LINE_FIELDS = {
    'first_line_therapy': (1, 'string', 'therapy'),
    'first_line_date': (1, 'date', 'start_date'),
    'first_line_outcome': (1, 'string', 'outcome'),
    'second_line_therapy': (2, 'string', 'therapy'),
    'second_line_date': (2, 'date', 'start_date'),
    'second_line_outcome': (2, 'string', 'outcome'),
    'later_therapy': (3, 'string', 'therapy'),
    'later_date': (3, 'date', 'start_date'),
    'later_outcome': (3, 'string', 'outcome'),
}


def _therapy_line_options():
    """The catalog choices the three line fields take.

    A regimen is stored by its catalog `code` and resolved to its HemOnc
    concept by the writer, so the option VALUE has to be PROMOP's code — a host
    slug would author a line whose regimen derivation cannot name. Outcomes are
    the disease-independent catalog; the writer narrows to the patient's disease.
    """
    from omop_core.models import TherapyRegimen
    from omop_core.services.treatment_catalog import OUTCOMES

    return {
        # `value` is the title because that is what the record's derived column
        # holds, so the control preselects it; `code` is what the writer
        # resolves. A regimen with no concept cannot author a line, so it is
        # not offered — a choice that always refuses is worse than none.
        'therapy': [
            {
                # `value` is the concept's own name because that is what
                # derivation writes into the column, so the control preselects
                # the record's value; `code` is what the writer resolves.
                'value': concept_name or title,
                'code': code,
                'label': title,
            }
            for code, title, concept_name in (
                TherapyRegimen.objects.filter(concept__isnull=False)
                .order_by('title')
                .values_list('code', 'title', 'concept__concept_name')
            )
        ],
        'outcome': [
            {'value': value, 'label': label} for _key, value, label in OUTCOMES
        ],
    }


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


# Columns whose vocabulary is a property of the disease, not of the field, so a
# single FieldChoice set cannot serve them: `stage` is Ann Arbor for follicular
# lymphoma and ISS/R-ISS for myeloma, `tumor_grade` is 1-3 for breast cancer and
# the centroblast grades for FL. The tab names the system and supplies its list;
# the seeds happen to be the follicular ones, so the descriptor must not attach
# them. Host and trial editors without a tab render these as text — no list is
# better than the wrong list.
_TAB_OWNED_ANSWER_SETS = frozenset({'stage', 'tumor_grade'})

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

#: Negated views of a stored boolean. The record keeps the POSITIVE status; a
#: trial asks "No HIV required" / "No Hepatitis B required", so the row is the
#: opposite of the column and a write has to go to the column INVERTED (#5663).
#:
#: These are not aliases: reading `hiv_status = false` through an alias would
#: show "No" beside a trial that says "No HIV", which is the wrong answer, and
#: writing the displayed value unchanged would store the opposite of the
#: patient's answer. The dedicated kind makes that impossible to miss — a
#: client that does not understand `negated` must leave the field alone rather
#: than write the display value.
#:
#: The `no_*` columns were removed in #2119; nothing here resurrects them. The
#: entry is a description of the QUESTION, and `canonical` is the only field a
#: write may touch.
_NEGATED_ALIASES = {
    'no_hiv_status': 'hiv_status',
    'no_hepatitis_b_status': 'hepatitis_b_status',
    'no_hepatitis_c_status': 'hepatitis_c_status',
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

#: `inputs` that name a RESOURCE, not a PatientRecord field.
#:
#: A computed value may be built from an authored resource — therapy lines come
#: from Episode/EpisodeEvent, language skills from PersonLanguageSkill — or from
#: a wearable metric the device pipeline owns. They are still inputs a UI can
#: offer (by opening that resource's editor), so the contract test accepts them;
#: everything else an `inputs` list names must resolve to a descriptor entry.
DESCRIPTOR_RESOURCE_INPUTS = frozenset(
    {'Episode', 'EpisodeEvent', 'PersonLanguageSkill'} | set(_WEARABLE_METRIC.values())
)


# field → the field it mirrors. Writing an alias directly would collide with its
# canonical on the same LOINC row, which is the failure #471 removed.
_ALIAS_TO_CANONICAL = {
    alias: canonical
    for canonical, aliases in _LAB_FIELD_ALIASES.items()
    for alias in aliases
}

#: An alias the MATCHER recomputes from other record fields.
#:
#: eGFR is a stored lab fact on the record, but EXACT derives it from the
#: CKD-EPI inputs before scoring, so a correction typed into the alias — or
#: into its canonical column — is replaced by the next derivation (#5664).
#: Naming the inputs is what lets a client offer the fields that actually move
#: the value instead of a box that silently does nothing.
_ALIAS_INPUTS = {
    'estimated_glomerular_filtration_rate': [
        'serum_creatinine_level', 'patient_age', 'gender',
    ],
}

# Values computed during derivation from other projected fields, with the inputs
# a UI needs in order to say why the box is not typeable. A field belongs here
# when `refresh_patient_record`/`recompute_patient_record_fields` owns its value:
# offering it as a direct write would let a caller replace a verdict that the
# next save recomputes, and would hide the facts that actually drive it.
_COMPUTED_INPUTS = {
    'gelf_criteria_status': ['gelf_criteria_options'],
    'flipi_score': ['flipi_score_options'],
    'flipi_risk_category': ['flipi_score_options'],
    'patient_age': ['date_of_birth'],
    'bmi': ['height', 'weight'],
    'meets_crab': [
        'serum_calcium_mg_dl', 'creatinine_clearance_ml_min',
        'serum_creatinine_mg_dl', 'hemoglobin_g_dl', 'bone_lesions',
    ],
    'tnbc_status': [
        'estrogen_receptor_status', 'progesterone_receptor_status', 'her2_status',
    ],
    'hr_status': ['estrogen_receptor_status', 'progesterone_receptor_status'],
    'tp53_disruption': ['genetic_mutations'],
    'involved_uninvolved_ratio': ['kappa_flc', 'lambda_flc'],
    'molecular_markers': ['genetic_mutations'],
    'liver_enzyme_levels': [
        'liver_enzyme_levels_ast', 'liver_enzyme_levels_alt',
        'liver_enzyme_levels_alp',
    ],
    'measurable_disease_imwg': [
        'monoclonal_protein_serum', 'monoclonal_protein_urine',
        'kappa_flc', 'lambda_flc',
    ],
    'measurable_disease_iwcll': [
        'alc_thousand_per_ul', 'largest_lymph_node_size',
        'splenomegaly', 'hepatomegaly',
    ],
    'metastatic_status': ['distant_metastasis_stage'],
    'renal_adequacy_status': ['egfr_ml_min_173m2', 'serum_creatinine_mg_dl'],
    'no_active_infection_status': ['active_infection_status'],
    'no_other_active_malignancies': ['active_malignancies'],
    # The HIV/hepatitis negatives were derived from the paired status fact by
    # `_latest_result`, so a direct write was replaced by the next derivation:
    # offering them as editable reported `applied` and then silently reverted
    # (#2106). They were then removed as columns altogether (#2119). The positive
    # field is the one to edit; its two answers are labelled by the descriptor
    # (`BOOLEAN_ANSWERS` in patient_form), not by a second column.
    # Same class, found by writing each field and re-deriving (#2113). These four
    # are derived from record fields, so the inputs are the edit surface:
    # `recompute_patient_record_fields` owns the verdict, the components do not.
    'b_symptoms': [
        'b_symptom_fever', 'b_symptom_night_sweats', 'b_symptom_weight_loss',
    ],
    'meets_slim': [
        'clonal_plasma_cells', 'kappa_flc', 'lambda_flc', 'kappa_lambda_ratio',
    ],
    'disease_slug': ['disease'],
    # These two have no record component at all: they are read off OMOP condition
    # facts (SNOMED 40733004 / 363346000). Computed with no inputs, so a client
    # shows the value read-only instead of offering a field that cannot exist.
    'active_infection_status': [],
    'active_malignancies': [],
}

def computed_only_fields():
    """The one computed-only classification (#2135).

    A field belongs here when derivation owns its value; a caller assigning it
    directly would replace a verdict the next save recomputes. Fields merely
    populated from OMOP are not computed-only and stay editable.
    """
    return frozenset(_COMPUTED_INPUTS)


def computed_only_message(field_name):
    inputs = _COMPUTED_INPUTS.get(field_name) or []
    if inputs:
        return (f'{field_name} is computed from ' + ', '.join(inputs)
                + '; edit those fields instead.')
    return f'{field_name} is computed by the server from OMOP facts; it is read-only.'


def _same_computed_value(field_name, current, submitted):
    """Compare a requested value to the record without duplicating DRF coercion."""
    if current is None and submitted is None:
        return True
    try:
        model_field = PatientRecord._meta.get_field(field_name)
    except FieldDoesNotExist:
        return current == submitted
    if isinstance(model_field, django_models.BooleanField):
        # Use DRF's own field so the vocabulary (including null spellings and
        # the 0/1-only numeric rule) cannot drift. Invalid input is never an
        # echo; a changed verdict must always reach the refusal.
        from rest_framework.exceptions import ValidationError as DrfValidationError
        unknown = object()

        def coerce(value):
            try:
                return _NULLABLE_BOOLEAN.run_validation(value)
            except (DrfValidationError, TypeError, ValueError):
                return unknown

        left, right = coerce(current), coerce(submitted)
        if left is unknown or right is unknown:
            return False
        return left == right
    if isinstance(model_field, django_models.DecimalField):
        try:
            quantum = Decimal(1).scaleb(-model_field.decimal_places)
            return (Decimal(str(current)).quantize(quantum)
                    == Decimal(str(submitted)).quantize(quantum))
        except (InvalidOperation, TypeError, ValueError):
            return False
    if isinstance(model_field, (django_models.IntegerField, django_models.FloatField)):
        try:
            return float(current) == float(submitted)
        except (TypeError, ValueError):
            return False
    return current == submitted


def computed_only_assignments(values, record):
    """Computed-only fields the request actually assigns, with their messages.

    An unchanged echo of the current value is not an assignment: editors PATCH
    the whole GET representation. A different value (including a clear) is.
    """
    if not values:
        return {}
    rejected = {}
    for field_name in computed_only_fields():
        if field_name not in values:
            continue
        submitted = values.get(field_name)
        if record is None:
            if submitted in (None, '', [], {}):
                continue
        elif _same_computed_value(
                field_name, getattr(record, field_name, None), submitted):
            continue
        rejected[field_name] = computed_only_message(field_name)
    return rejected

# Current-diagnosis myeloma-related disorders (#5521). These are direct
# PatientRecord fields; an affirmative answer projects to the reviewed OMOP
# fact, while No/Unknown remains pending on PatientRecord because occurrence
# tables cannot express a negative. Concepts come from the clinical owner's
# list in CB #5521; IgM myeloma has no standard concept and stays direct-only.
_MYELOMA_RELATED_DISORDER_PROJECTIONS = {
    'mgus': {
        'concept_id': 4149022, 'omop_table': 'observation',
        'concept_code': '35601003', 'vocabulary': 'SNOMED',
        'label': 'MGUS',
    },
    'smoldering_multiple_myeloma': {
        'concept_id': 4184985, 'omop_table': 'condition',
        'concept_code': '413587002', 'vocabulary': 'SNOMED',
        'label': 'Smoldering multiple myeloma',
    },
    'waldenstrom_macroglobulinemia': {
        'concept_id': 4098597, 'omop_table': 'condition',
        'concept_code': '190818004', 'vocabulary': 'SNOMED',
        'label': 'Waldenström macroglobulinemia',
    },
    'igm_multiple_myeloma': {
        'label': 'IgM multiple myeloma',
    },
    'al_amyloidosis': {
        'concept_id': 4047607, 'omop_table': 'condition',
        'concept_code': '23132008', 'vocabulary': 'SNOMED',
        'label': 'Light-chain (AL) amyloidosis',
    },
    'poems_syndrome': {
        'concept_id': 4193776, 'omop_table': 'condition',
        'concept_code': '79268002', 'vocabulary': 'SNOMED',
        'label': 'POEMS syndrome',
    },
}

# Fields whose projection is fixed by a reviewed built-in recipe. A curator
# mapping must not replace their descriptor semantics or read OMOP facts back
# into a current answer (#5521).
PINNED_PROJECTION_FIELDS = frozenset(_MYELOMA_RELATED_DISORDER_PROJECTIONS)


def _dated_with_canonical(canonical_entry):
    """Copy the canonical's date marker onto its editable source pair (#2397).

    The pair is the fact a patient edits; the canonical is the read-model that
    carries the projection. A date the editor attaches to the pair must land on
    the same result, so the pair is only dated when its canonical is.
    """
    return {'dated': True} if (canonical_entry or {}).get('dated') else {}


def pinned_projection_source_value(field):
    """The source value that marks an OMOP row as this field's own answer.

    The rows a pinned recipe writes are told apart from imported history by
    this, never by the concept: an EHR row for the same SNOMED code is a
    diagnosis, this is a patient's current-state answer dated the day they
    gave it. Derivation uses it to keep such a row from becoming the record's
    primary cancer, and a later No/Unknown uses it to find the rows it retracts.
    """
    return 'patient-record:' + field


PINNED_PROJECTION_SOURCE_VALUES = frozenset(
    pinned_projection_source_value(field) for field in PINNED_PROJECTION_FIELDS
)

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

def _curated_writes(choice_options=None, genomics_recipes=None, clinical_mappings=None,
                    genetic_recipes=None):
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
    from omop_core.services.breast_mapping_safety import wrong_breast_field_mappings
    from omop_core.services.clinical_input_contract import CLINICAL_INPUT_FIELDS

    entries = {}
    # Genomics recipes ride along in the same read: they are writable whatever
    # their status, so they are collected into *genomics_recipes* when given.
    fetched = list(
        FieldConceptMapping.objects
        .filter(Q(status='approved') | Q(field_name__startswith='genomics_')
                | Q(field_name__in=CLINICAL_INPUT_FIELDS))
        .exclude(wrong_breast_field_mappings())
        .exclude(Q(omop_table='') & ~Q(field_name__in=CLINICAL_INPUT_FIELDS))
        .select_related('concept')
    )
    if genomics_recipes is not None:
        genomics_recipes.update({r.field_name: r for r in fetched if r.field_name.startswith('genomics_')})
    if genetic_recipes is not None:
        # The variant writer always normalises a status, so that recipe must be
        # usable even when the caller sends only a gene. Collected from the
        # same read as the rest.
        genetic_recipes.update(
            {r.field_name: r for r in fetched if r.field_name == 'genetic_mutations.status'})
    if clinical_mappings is not None:
        clinical_mappings.update({r.field_name: r for r in fetched if r.field_name in CLINICAL_INPUT_FIELDS})
    rows = [r for r in fetched if r.status == 'approved']
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
                'writable': True,
                'target': 'patient_record',
                'curated': True,
                'value_kind': row.value_kind or _value_kind(row.field_name),
                'reason': (
                    f'Written directly to PatientRecord. The approved mapping '
                    f'source_value exceeds the {width}-character limit of '
                    f'{source_field.model._meta.db_table}.{source_field.name}; '
                    'fix the mapping before projection is available.'
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
                'concept_code': row.concept.concept_code,
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
        elif choice_options and row.field_name in choice_options:
            entry['options'] = choice_options[row.field_name]
        # `multiple` describes the field's shape, not the presence of a picker
        # vocabulary. A list field whose vocabulary is not loaded (or that has
        # no vocabulary at all) is still a list: without this the client fell
        # back to a single select and wrote a scalar over the list.
        if row.multiple:
            entry['multiple'] = True
        if (row.field_name == 'cytogenetic_markers' and row.multiple
                and row.vocabulary_id == 'SNOMED' and row.concept_code == '107675007'):
            from omop_core.services.cytogenetics import descriptor as cytogenetic_descriptor
            entry = cytogenetic_descriptor(mapping_approved=True)
        entries[row.field_name] = entry

    # Attach value_concept_map for concept-valued fields so the projection
    # engine can write value_as_concept_id from the choice's coded answer.
    concept_fields = [
        fn for fn, e in entries.items()
        if e.get('value_kind') == 'concept' and e.get('projection')
    ]
    if concept_fields:
        from omop_core.models import FieldChoiceCode
        codes = list(
            FieldChoiceCode.objects
            .filter(choice__field_name__in=concept_fields, is_primary=True)
            .values_list('choice__field_name', 'choice__display',
                         'vocabulary_id', 'code')
        )
        if codes:
            # Resolve (vocabulary_id, code) pairs → concept_id.  Group by
            # vocabulary so the existing helper works.
            by_vocab: dict[str, set[str]] = {}
            for _, _, vid, code in codes:
                by_vocab.setdefault(vid, set()).add(code)
            resolved: dict[tuple[str, str], int] = {}
            for vid, code_set in by_vocab.items():
                for code, cid in _resolve_concept_ids(code_set, vid).items():
                    resolved[(vid, code)] = cid
            for fn, display, vid, code in codes:
                pk = resolved.get((vid, code))
                if pk is not None:
                    proj = entries[fn].get('projection')
                    if proj is not None:
                        proj.setdefault('value_concept_map', {})[display] = pk
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
    # Server-managed bookkeeping, never a clinical value (#2426): the day a
    # pending source pair was entered. The pair write paths maintain it.
    from omop_core.services.source_pair_dates import FIELD as _PAIR_DATES_FIELD
    read_only.add(_PAIR_DATES_FIELD)
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
    # The language summary and eight capabilities belong to PersonLanguageSkill.
    read_only |= DERIVED_LANGUAGE_FIELDS
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
    # A source pair's own value is the write target even though the canonical it
    # feeds lists it as a legacy alias (#2318 follow-up). Hemoglobin is not in
    # `_LAB_FIELD_ALIASES` for the same reason; creatinine needs the alias entry
    # so a canonical import still propagates back into the pair. The subtraction
    # is after the unit loop on purpose: that loop re-adds every `*_units`.
    read_only -= {'serum_creatinine_level', 'serum_creatinine_level_units'}
    read_only -= {
        field
        for spec in CHEMISTRY_SOURCE_PAIRS.values()
        for field in (spec['value'], spec['unit'])
    }
    # Direct bilirubin's pair is the write target too (#2318); the unitless
    # alias stays read-only below.
    read_only -= {direct_bilirubin_units.PAIR_FIELD, direct_bilirubin_units.UNIT_FIELD}
    # The unitless direct-bilirubin mirror is derived from its source pair
    # (#2318); a direct write would be overwritten by the next recompute.
    read_only.add('serum_bilirubin_level')
    read_only -= {'relapse_count', 'treatment_refractory_status'}
    _CACHED_SERIALIZER_READ_ONLY = frozenset(read_only)
    return _CACHED_SERIALIZER_READ_ONLY


def _descriptor_unit(unit):
    """The unit to show, or None when the mapping has only a placeholder.

    `LAB_FIELD_TO_LOINC` spells "no physical unit" as `{score}` for ECOG,
    Karnofsky and the other ordinal scales. Sending that to a client put the
    literal `{score}` beside an edit box (and in the projection metadata).
    A placeholder is not a unit; the field is a score, and the value needs no
    suffix.
    """
    if isinstance(unit, str):
        stripped = unit.strip()
        if stripped.startswith('{') and stripped.endswith('}'):
            return None
    return unit


def build_writable_field_descriptor():
    """Return {field: descriptor} for every mapped PatientRecord clinical field.

    Query cost is flat: two lookups total, not one per field.
    """
    loinc_ids = _resolve_concept_ids(
        {code for code, _unit, _display in LAB_FIELD_TO_LOINC.values()}, 'LOINC'
    )
    unit_ids = _resolve_concept_ids(
        {unit for _code, unit, _display in LAB_FIELD_TO_LOINC.values()}
        | {'umol/L'}, 'UCUM'
    )
    derived_concepts = _resolve_concepts(
        {(vocab, code) for code, vocab, _fn in DERIVED_FIELD_TO_CODE.values()}
    )
    # The reviewed myeloma concepts come from the same vocabulary tables and are
    # resolved in this one query with the largest-lymph-node concept, so adding
    # them does not add a query per field or a new query overall (#5521).
    myeloma_concept_ids = {
        spec['concept_id'] for spec in _MYELOMA_RELATED_DISORDER_PROJECTIONS.values()
        if spec.get('concept_id')
    }
    concept_by_id = {
        concept.concept_id: concept
        for concept in Concept.objects.filter(
            concept_id__in=myeloma_concept_ids
            | {CONCEPT_LARGEST_LYMPH_NODE_DIMENSION, CONCEPT_EHR_TYPE},
        ).only('concept_id', 'concept_code', 'concept_name', 'vocabulary_id')
    }
    largest_lymph_node_concept = concept_by_id.get(CONCEPT_LARGEST_LYMPH_NODE_DIMENSION)
    ehr_type_loaded = CONCEPT_EHR_TYPE in concept_by_id
    myeloma_concepts = {
        field: concept_by_id.get(spec['concept_id'])
        for field, spec in _MYELOMA_RELATED_DISORDER_PROJECTIONS.items()
        if spec.get('concept_id')
    }

    # One FieldChoice set is one meaning, but these columns carry a different
    # vocabulary per disease and the tab names it (`_TAB_OWNED_ANSWER_SETS`).
    # Dropping them before both the curated-mapping path and the fallback
    # attachment below means an approved mapping cannot reintroduce them either.
    choice_options = {
        field_name: [
            {'value': display, 'code': primary_code}
            for display, primary_code in choices
        ]
        for field_name, choices in _field_choice_options().items()
        if field_name not in _TAB_OWNED_ANSWER_SETS
    }
    from omop_core.services.genomics import mapping_is_usable
    genomics_recipes = {}
    genetic_recipes = {}
    clinical_mappings = {}
    curated = _curated_writes(
        choice_options, genomics_recipes, clinical_mappings, genetic_recipes)

    descriptor = {}
    line_options = None
    for field in sorted(PATIENT_RECORD_OMOP_MAPPED_FIELDS - _LIFECYCLE_FIELDS):
        if field in DERIVED_LANGUAGE_FIELDS:
            descriptor[field] = {
                'kind': KIND_COMPUTED,
                'writable': False,
                'inputs': ['PersonLanguageSkill'],
                'reason': 'Derived from PersonLanguageSkill; write via the language-skills API.',
            }
            continue
        if field.startswith('genomics_'):
            # The recipe decides whether a finding can be stored; approval only
            # decides whether its concept flows forward. So review status is
            # not consulted here, unlike curated (approved-only) projections.
            mapping = genomics_recipes.get(field)
            writable = (mapping_is_usable(mapping, parent=True)
                        and mapping.value_kind == 'json' and mapping.multiple)
            descriptor[field] = {
                'kind': KIND_EDITABLE, 'writable': bool(writable), 'target': 'patient_record',
                'reason': 'Priority findings write through their Genomics field recipes.',
            }
            continue
        if field == 'cytogenetic_markers':
            descriptor[field] = {
                'kind': KIND_COMPUTED, 'writable': False, 'target': 'genomics',
                'reason': 'Legacy cytogenetic summary is read-only. Record individual findings in Genomics.',
            }
            continue
        if field == 'genetic_mutations':
            # The entry advertises the Genomics CRUD endpoint, so ask that
            # writer what it needs: concept 0, a CDM measurement event concept
            # and the actor type concepts. The retired list-diff writer's gene
            # LOINC requirement is a different path (`field_write_service`).
            # Advertising Add regardless offered a control whose save could
            # only 400 (#2441/#2433).
            from omop_core.services.genomics import variants_writable
            writable = variants_writable(
                status_recipe=genetic_recipes.get('genetic_mutations.status'))
            descriptor[field] = {
                'kind': KIND_EDITABLE, 'writable': writable, 'target': 'genomics',
                'endpoint': '/api/v1/patient-records/{person_id}/genomics/',
                'reason': (
                    'Edit individual variants in the Genomics tab; each variant '
                    'writes linked OMOP facts.'
                    if writable else
                    'The OMOP CDM and type vocabularies needed to store variants '
                    'are not loaded.'
                ),
            }
            continue
        if field in _AUTHORABLE_LINE_FIELDS:
            line_number, value_kind, part = _AUTHORABLE_LINE_FIELDS[field]
            if line_options is None:
                line_options = _therapy_line_options()
            descriptor[field] = {
                'kind': KIND_DIRECT,
                'writable': True,
                'target': 'patient_record',
                'value_kind': value_kind,
                # Which line the write authors. The client shows the control;
                # the writer owns the conversion to Episode/EpisodeEvent and the
                # derivation that fills this column back.
                'authored_episode': {'line_number': line_number, 'part': part},
                # The published recipe stays: a host that explains how to
                # author a line keeps its answer, and this control is the
                # in-form route to exactly that write.
                'authored_via': _THERAPY_RECIPE,
                'reason': (
                    'Written as a therapy line (an Episode grouping its facts); '
                    'this column is derived from that line.'
                ),
            }
            if line_options.get(part):
                descriptor[field]['options'] = line_options[part]
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
            if field in _ALIAS_INPUTS:
                descriptor[field]['inputs'] = _ALIAS_INPUTS[field]
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

        if field in _MYELOMA_RELATED_DISORDER_PROJECTIONS:
            entry = {
                'kind': KIND_DIRECT,
                'writable': True,
                'target': 'patient_record',
                'value_kind': 'boolean',
                'label': _MYELOMA_RELATED_DISORDER_PROJECTIONS[field]['label'],
                # Yes / No / Unknown are three distinct answers here; the
                # shared form must offer Unknown explicitly instead of relying
                # on a generic clear (#5521).
                'tri_state': True,
            }
            spec = _MYELOMA_RELATED_DISORDER_PROJECTIONS[field]
            concept = myeloma_concepts.get(field)
            if concept is not None and ehr_type_loaded:
                entry['projection'] = {
                    'omop_table': spec['omop_table'],
                    'concept_id': concept.concept_id,
                    'code': concept.concept_code,
                    'vocabulary': concept.vocabulary_id,
                    'display': concept.concept_name,
                    'type_concept_id': CONCEPT_EHR_TYPE,
                    'source_value': pinned_projection_source_value(field),
                    # No/Unknown must not become a negative occurrence; the
                    # answer stays pending on PatientRecord, and the rows an
                    # earlier Yes wrote are retracted.
                    'affirmative_only': True,
                    # The shared form writes PatientRecord first and treats this
                    # fact as a side effect: an occurrence cannot be derived back
                    # into a current answer, so it must not be the only write.
                    'best_effort': True,
                }
                entry['reason'] = (
                    'Written directly to PatientRecord; an affirmative answer '
                    'projects to the reviewed OMOP fact. No/Unknown stays pending '
                    'because occurrence tables cannot express a negative.'
                )
            elif not spec.get('concept_id'):
                entry['reason'] = (
                    'Written directly to PatientRecord. No standard OMOP concept '
                    'is available for IgM myeloma; no projection is attempted.'
                )
            elif not ehr_type_loaded:
                entry['reason'] = (
                    'Written directly to PatientRecord. The EHR type concept '
                    f'{CONCEPT_EHR_TYPE} is not loaded — no OMOP projection available.'
                )
            else:
                entry['reason'] = (
                    f"Written directly to PatientRecord. {spec['vocabulary']} "
                    f"{spec['concept_code']} is not loaded — no OMOP projection available."
                )
            descriptor[field] = entry
            continue

        if field in _COMPUTED_INPUTS:
            inputs = _COMPUTED_INPUTS[field]
            descriptor[field] = {
                'kind': KIND_COMPUTED,
                'writable': False,
                'inputs': inputs,
                'reason': (
                    'Computed from ' + ', '.join(inputs) + '.' if inputs else
                    'Computed from OMOP facts; there is no record field to edit.'
                ),
            }
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

        if field in curated:
            descriptor[field] = curated[field]
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
        unit = _descriptor_unit(unit)
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
    # A curator-managed answer set applies to every field that stores one, not
    # only to the unmapped KIND_NEEDS_CONCEPT branch above. `bone_lesions` is
    # mapped to SNOMED 363817008 and still needs its Yes/No/Unknown choices: a
    # shared editor that renders from the descriptor (and ignores a tab's local
    # options) otherwise shows an empty picker (#5603). An existing entry's
    # options win, so curated concept-valued sets are never replaced here.
    #
    # `stage` and `tumor_grade` are excluded where the set is built, because
    # their vocabulary is a property of the disease and the tab names it. Host
    # and trial editors without a tab render them as text: no list is better
    # than the wrong list.
    for field, options in choice_options.items():
        entry = descriptor.get(field)
        if entry is not None and options and 'options' not in entry:
            entry['options'] = options
    # The five-step toxicity scale is a closed vocabulary. Without it the
    # shared editor falls back to a free-text number box; the values are the
    # grades the trial rows already use, and the column takes the number.
    if 'toxicity_grade' in descriptor and 'options' not in descriptor['toxicity_grade']:
        descriptor['toxicity_grade']['options'] = [
            {'value': grade, 'label': label}
            for grade, label in (
                (0, 'Grade 0 (None)'),
                (1, 'Grade 1 (Mild)'),
                (2, 'Grade 2 (Moderate)'),
                (3, 'Grade 3 (Severe)'),
                (4, 'Grade 4 (Life-Threatening)'),
            )
        ]
    # A scalar measurement/observation may carry the clinical date it was
    # observed (docs/clinical-event-time-policy.md). The editor offers the date
    # control only when this marker is present; a best-effort side effect is not
    # read back from the fact, and JSON values keep their dedicated contract.
    # Only the numeric laboratory fields whose value a shared derivation selects
    # get a date control: categorical, occurrence-scoped and non-lab measurements
    # need their own winning-fact rules before an editor can correct them here.
    for field, entry in descriptor.items():
        projection = entry.get('projection') or {}
        if ((entry.get('writable') or entry.get('normalised_read_model'))
                and not projection.get('best_effort')
                and field in LAB_FIELD_TO_LOINC
                and projection.get('omop_table') == 'measurement'
                and entry.get('value_kind') == 'number'):
            entry['dated'] = True
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
    # The trial catalog still spells the legacy summary `cytogenic_markers`
    # (no second "t"); the record and the serializer alias use
    # `cytogenetic_markers`. Describe that spelling rather than leaving an
    # editor with no entry at all (healthkey-ai/exact#755).
    descriptor.setdefault('cytogenic_markers', {
        'kind': KIND_ALIAS,
        'writable': False,
        'canonical': 'cytogenetic_markers',
        'reason': (
            'Legacy summary of cytogenetic_markers; that column is read-only. '
            'Record individual findings in Genomics.'
        ),
    })

    # The trial vocabulary asks the negated question ("No Hepatitis B
    # required"), while the record keeps the positive answer. The descriptor
    # carries BOTH halves — the field to write and the fact that the value
    # must be inverted — because a client that guessed one without the other
    # would store the opposite of what the reader answered (#5663).
    for negated, positive in _NEGATED_ALIASES.items():
        descriptor.setdefault(negated, {
            'kind': KIND_NEGATED,
            'writable': False,
            'canonical': positive,
            'inverted': True,
            'value_kind': 'boolean',
            # Navigation only: it tells an editor which field the answer comes
            # from, so the dialog can open on the canonical control instead of
            # guessing. It is NOT evidence that the value was present — see
            # EXACT's raw-boundary provenance (`available`/`value`), which is
            # the only thing that may say whether the source was supplied
            # (#5667). A client that treats `inputs` as presence shows a
            # confident answer for a patient who never answered.
            'inputs': [positive],
            'reason': (
                f'Inverted view of {positive}: the record stores the positive '
                f'status, and this row asks the negated question. Write '
                f'{positive} with the opposite value.'
            ),
        })

    # The words the dedicated dialog asks in (#5667). `answer_labels` is
    # already what the form descriptor shows; the trial writable descriptor
    # carries it too, so a federated client reading only this endpoint can say
    # "HIV Positive" / "No HIV" instead of a generic Yes/No over a polarity
    # boundary. Metadata only: it changes no write path.
    for positive in _NEGATED_ALIASES.values():
        labels = BOOLEAN_ANSWERS.get(positive)
        if labels and positive in descriptor:
            descriptor[positive]['answer_labels'] = {
                'true': labels[0],
                'false': labels[1],
            }

    # The birth date is stored at one of three precisions. A full date typed
    # into a year-only record must not silently promote it (or be truncated
    # to January 1st) — the editor needs the precision as its own control
    # (#5664). `normalize_birth_date` treats an empty value as the default
    # `day`, so a client that sends only the date still gets a full date.
    if 'date_of_birth_precision' in descriptor:
        descriptor['date_of_birth_precision'].update({
            'value_kind': 'string',
            'options': [
                {'value': 'year', 'label': 'Year only'},
                {'value': 'month', 'label': 'Month and year'},
                {'value': 'day', 'label': 'Full date'},
            ],
        })

    # A pre-existing-conditions answer is an ATTESTATION, not a column write:
    # it has its own endpoint, its own audit rows, and a snapshot that refuses
    # an answer whose starting point has moved (#1988). A generic PATCH here
    # would race another tab's answer, and a stale dialog would silently
    # overwrite it. The descriptor names the endpoint, the actions and the
    # vocabulary so a host can build the editor without hardcoding any of the
    # three; `writable: false` is the truth — the field is answered, not
    # written.
    category_options = [
        {'value': code, 'label': title}
        for code, title in (
            PreExistingConditionCategory.objects
            .order_by('title')
            .values_list('code', 'title')
        )
    ]
    descriptor['pre_existing_condition_categories'] = {
        'kind': KIND_EXPLICIT_ANSWER,
        'writable': False,
        'target': 'patient_record',
        'endpoint': 'preexisting-conditions-answer',
        'actions': ['confirm_none', 'set_categories', 'withdraw'],
        # Every offered action needs its label: a client that refuses to
        # invent clinical wording drops an unlabelled action, so an incomplete
        # set turns into an empty dialog (exact#750 follow-up, #2396).
        'action_labels': {
            'confirm_none': 'No pre-existing conditions',
            'set_categories': 'Save these conditions',
            'withdraw': 'Withdraw answer',
        },
        'value_kind': 'string',
        'multiple': True,
        # `writable: false` is about the PATCH; the endpoint is a separate
        # permission. A read-only caller must not be offered the dialog at
        # all, so the caller-aware view clears this flag with the rest.
        'answerable': True,
        'options': category_options,
        'reason': (
            'Answered explicitly through the patient-record '
            'pre-existing-conditions endpoint. The answer is an attestation '
            'with a snapshot, not a field PATCH, so a concurrent answer '
            'cannot be overwritten.'
        ),
    }

    # #5666: the two derived negatives are answers too. The formula is
    # deactivated; while an attestation exists it wins, and withdrawing it
    # hands the value back to the source facts. Only the negative is
    # confirmed here — a recorded active infection or a second active
    # malignancy is the fact to correct, not a value to overwrite.
    for field_name, label, confirm_label in (
        ('no_active_infection_status', 'No Active Infection',
         'Confirm no active infection'),
        ('no_other_active_malignancies', 'No Other Active Malignancies',
         'Confirm no other active malignancies'),
    ):
        descriptor[field_name] = {
            'kind': KIND_EXPLICIT_ANSWER,
            'writable': False,
            'target': 'patient_record',
            'endpoint': 'negative-answer',
            'actions': ['confirm_negative', 'withdraw'],
            'action_labels': {
                'confirm_negative': confirm_label,
                'withdraw': 'Withdraw confirmation',
            },
            'value_kind': 'boolean',
            'answerable': True,
            'inputs': list(_COMPUTED_INPUTS[field_name]),
            'reason': (
                f'Answered explicitly through the patient-record negative-answer '
                f'endpoint as an attestation with a snapshot. While confirmed, '
                f'the answer wins over the {label.lower()} source fact; '
                f'withdraw it to return to the derived value.'
            ),
        }

    # A clinician's Lugano assessment has no PET/CT or bone-marrow result in
    # the record to derive it from, so it is an input the reader supplies.
    # The row was read-only with no door at all (#5667 follow-up).
    descriptor['meets_lugano'] = {
        'kind': KIND_DIRECT,
        'writable': True,
        'target': 'patient_record',
        'value_kind': 'boolean',
        'reason': (
            'Clinician-confirmed Lugano criteria (PET/CT and bone-marrow '
            'assessment), entered directly on the record.'
        ),
    }

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
    # The shared form stores the record answer first and mirrors it as a
    # best-effort side effect. Death date is that same dated assertion the
    # native PATCH writes (`project_single_value`); without this flag the
    # shared form saved the column and never recorded the observation.
    descriptor['death_date']['projection']['best_effort'] = True
    from omop_core.services.clinical_input_contract import NEW_INPUT_FIELDS, descriptors
    for field, entry in descriptors(mappings=clinical_mappings).items():
        row = clinical_mappings.get(field)
        if field in NEW_INPUT_FIELDS or (row is not None and row.concept_id == 0):
            descriptor[field] = entry

    # Hemoglobin's source pair (#2318). The patient records the number in the
    # unit their lab used; `hemoglobin_g_dl` becomes the normalised read-model
    # that matching and CRAB consume. The pair is the write target and the
    # canonical value is derived from it: the old entry pointed the other way,
    # so an editor wrote g/dL and derivation overwrote whatever unit the reader
    # had chosen.
    canonical_hemoglobin = descriptor.get('hemoglobin_g_dl') or {}
    descriptor['hemoglobin_level'] = {
        'kind': KIND_DIRECT,
        'writable': True,
        'target': 'patient_record',
        'value_kind': 'number',
        **_dated_with_canonical(canonical_hemoglobin),
    }
    descriptor['hemoglobin_level_units'] = {
        'kind': KIND_SELECTABLE,
        'writable': False,
        'qualifies': 'hemoglobin_level',
        'reason': (
            'Unit of hemoglobin_level; selected alongside that value and '
            'stored with it. The canonical g/dL value is derived from the pair.'
        ),
    }
    descriptor['hemoglobin_g_dl'] = {
        # Keep the canonical's diagnostics: a curated mapping, an overlong
        # source value, the projection recipe and the date marker all still
        # belong to this read-model.
        **canonical_hemoglobin,
        'kind': KIND_COMPUTED,
        'writable': False,
        'value_kind': 'number',
        'inputs': ['hemoglobin_level'],
        # A canonical import (an approved mapping, a plain-descriptor host,
        # an older client that speaks g/dL) may write this column directly;
        # the server then retires the pending pair. The shared form edits the
        # source pair; the plain writable-fields response reports this entry
        # writable for hosts without a pair editor (writable_fields view).
        'normalised_read_model': True,
        'reason': canonical_hemoglobin.get('reason') or (
            'Normalised from hemoglobin_level in the default unit (g/dL); '
            'the shared form edits that field and its unit.'
        ),
        'unit': canonical_hemoglobin.get('unit') or 'g/dL',
    }

    # Creatinine's source pair (#2318 follow-up). The CKD-EPI matcher reads the
    # normalised mg/dL value, and the record keeps what the lab reported. A host
    # that withholds the pair would leave the eGFR row with no editable source.
    canonical_creatinine = descriptor.get('serum_creatinine_mg_dl') or {}
    descriptor['serum_creatinine_level'] = {
        'kind': KIND_DIRECT,
        'writable': True,
        'target': 'patient_record',
        'value_kind': 'number',
        **_dated_with_canonical(canonical_creatinine),
    }
    descriptor['serum_creatinine_level_units'] = {
        'kind': KIND_SELECTABLE,
        'writable': False,
        'qualifies': 'serum_creatinine_level',
        'reason': (
            'Unit of serum_creatinine_level; selected alongside that value and '
            'stored with it. The canonical mg/dL value is derived from the pair.'
        ),
    }
    descriptor['serum_creatinine_mg_dl'] = {
        **canonical_creatinine,
        'kind': KIND_COMPUTED,
        'writable': False,
        'value_kind': 'number',
        'inputs': ['serum_creatinine_level'],
        'normalised_read_model': True,
        'reason': canonical_creatinine.get('reason') or (
            'Normalised from serum_creatinine_level in the default unit (mg/dL); '
            'the shared form edits that field and its unit.'
        ),
        'unit': canonical_creatinine.get('unit') or 'mg/dL',
    }

    # The chemistry source pairs (#2318): albumin, serum calcium and total
    # bilirubin. Same contract as creatinine — the record keeps what the lab
    # reported, the canonical is the normalised read-model, and an editor names
    # the source instead of the derived column.
    for canonical, spec in CHEMISTRY_SOURCE_PAIRS.items():
        pair = spec['value']
        unit = spec['unit']
        canonical_entry = descriptor.get(canonical) or {}
        descriptor[pair] = {
            'kind': KIND_DIRECT,
            'writable': True,
            'target': 'patient_record',
            'value_kind': 'number',
            **_dated_with_canonical(canonical_entry),
        }
        descriptor[unit] = {
            'kind': KIND_SELECTABLE,
            'writable': False,
            'qualifies': pair,
            'reason': (
                f'Unit of {pair}; selected alongside that value and stored '
                f'with it. The canonical {spec["canonical_unit"]} value is '
                'derived from the pair.'
            ),
        }
        descriptor[canonical] = {
            # Keep the canonical's diagnostics, as the creatinine read-model
            # does: the curated mapping, projection recipe and date marker are
            # still this column's.
            **canonical_entry,
            'kind': KIND_COMPUTED,
            'writable': False,
            'value_kind': 'number',
            'inputs': [pair],
            'normalised_read_model': True,
            'reason': canonical_entry.get('reason') or (
                f'Normalised from {pair} in the default unit '
                f'({spec["canonical_unit"]}); the shared form edits that field and its unit.'
            ),
            'unit': canonical_entry.get('unit') or spec['canonical_unit'],
        }

    # Direct bilirubin's source pair (#2318). It has no separate canonical
    # column: the pair is the stored fact and the matcher converts it, so the
    # OMOP projection must carry the unit the reader chose rather than the
    # recipe's static mg/dL. The unitless `serum_bilirubin_level` mirror is
    # normalised to mg/dL and is not editable.
    direct_pair = direct_bilirubin_units.PAIR_FIELD
    direct_unit = direct_bilirubin_units.UNIT_FIELD
    direct_entry = descriptor.get(direct_pair) or {}
    direct_projection = dict(direct_entry.get('projection') or {})
    direct_projection['unit_from'] = direct_unit
    direct_projection['unit_by_code'] = {
        code: {'label': label, 'concept_id': unit_ids.get(label)}
        for code, label in direct_bilirubin_units.UNIT_LABELS.items()
    }
    descriptor[direct_pair] = {
        **direct_entry,
        'kind': KIND_DIRECT,
        'writable': True,
        'target': 'patient_record',
        'value_kind': 'number',
        'unit_by_code': direct_projection['unit_by_code'],
        'projection': direct_projection,
    }
    descriptor[direct_unit] = {
        **(descriptor.get(direct_unit) or {}),
        'kind': KIND_SELECTABLE,
        'writable': False,
        'qualifies': direct_pair,
        'reason': (
            'Unit of serum_bilirubin_level_direct; selected alongside that '
            'value and stored with it. The OMOP fact carries this unit.'
        ),
    }
    descriptor[direct_bilirubin_units.ALIAS_FIELD] = {
        'kind': KIND_COMPUTED,
        'writable': False,
        'value_kind': 'number',
        'inputs': [direct_pair, direct_unit],
        'reason': (
            'Read-only mg/dL mirror of the direct bilirubin source pair; edit '
            'the pair and its unit instead.'
        ),
        'unit': 'mg/dL',
    }

    # The unit choices (#2318). `FIELD_COMMON_UNITS` is the curated list per
    # field, already ordered; the client draws its picker from it rather than
    # spelling the values a second time. Both the field a write lands in and the
    # `*_units` entry beside it carry the same list, so a client that resolves
    # either shape sees one answer.
    for name, units in FIELD_COMMON_UNITS.items():
        entry = descriptor.get(name)
        if entry is not None and units:
            entry['unit_options'] = [
                {'value': unit, 'label': unit} for unit in units
            ]
    for field, entry in descriptor.items():
        if not field.endswith('_units') or entry.get('kind') != KIND_SELECTABLE:
            continue
        units = FIELD_COMMON_UNITS.get(entry.get('qualifies') or '')
        if units:
            entry['unit_options'] = [
                {'value': unit, 'label': unit} for unit in units
            ]

    # Stored unit codes vs display labels (#2318 follow-up). The model columns
    # are `TextChoices` whose values are the stored codes (`MG/DL`, `G/DL`, `lb`),
    # while `FIELD_COMMON_UNITS` holds the display notation a curator writes
    # (`mg/dL`, `g/dL`, `lbs`). Emitting the display string as the option's value
    # advertised a choice the write side refuses: a client that sent it got a
    # validation error, and `weight` even advertised `lbs` for a column whose
    # code is `lb`. Each pair below names the stored code and the display the
    # picker shows, and the test asserts every value is a real model choice.
    unit_code_options = {
        'weight_units': [('kg', 'kg'), ('lb', 'lb')],
        'height_units': [('cm', 'cm'), ('in', 'in')],
        'albumin_level_units': [('G/DL', 'g/dL'), ('G/L', 'g/L')],
        'serum_calcium_level_units': [('MG/DL', 'mg/dL'), ('MMOL/L', 'mmol/L')],
        'serum_creatinine_level_units': [('MG/DL', 'mg/dL'), ('UMOL/L', 'umol/L')],
        'serum_bilirubin_level_total_units': [('MG/DL', 'mg/dL'), ('UMOL/L', 'umol/L')],
        'serum_bilirubin_level_direct_units': [('MG/DL', 'mg/dL'), ('UMOL/L', 'umol/L')],
    }
    for units_field, code_pairs in unit_code_options.items():
        entry = descriptor.get(units_field)
        if entry is None:
            continue
        options = [{'value': code, 'label': label} for code, label in code_pairs]
        entry['unit_options'] = options
        qualified = descriptor.get(entry.get('qualifies') or '')
        if qualified is not None:
            qualified['unit_options'] = options

    # Hemoglobin's slice (#2318) offers the two scales it can convert today.
    # The value is the stored code (`G/L`), the label the display text — the
    # model's `TextChoices`, not the curated display strings, because the
    # write side validates against the column's choices. mmol/L waits for a
    # reviewed factor.
    hemoglobin_units = [
        {'value': 'G/DL', 'label': 'g/dL'},
        {'value': 'G/L', 'label': 'g/L'},
    ]
    for field in ('hemoglobin_level', 'hemoglobin_level_units'):
        entry = descriptor.get(field)
        if entry is not None:
            entry['unit_options'] = hemoglobin_units

    # Blood-count source pairs (#5700 follow-up). EXACT's rows speak cells/µL
    # (`absolute_neutrophile_count`, ...), so the legacy column is the write
    # target; the canonical thousands/µL column becomes the computed
    # read-model that carries the projection. The old alias direction sent the
    # typed 1550 straight into a thousands field and derivation read it back as
    # 1,550,000 cells/µL.
    for source, spec in COUNT_SOURCE_PAIRS.items():
        canonical = spec['canonical']
        source_entry = descriptor.get(source) or {}
        # Without a unit column there is nowhere to store a chosen scale, so
        # the field advertises only the cells/µL it actually reads and writes.
        source_units = (COUNT_UNIT_OPTIONS if spec.get('unit_field')
                        else [{'value': spec['unit'], 'label': 'cells/µL'}])
        descriptor[source] = {
            **{key: value for key, value in source_entry.items()
               if key not in ('canonical', 'reason', 'unit_options')},
            'kind': KIND_DIRECT,
            'writable': True,
            'target': 'patient_record',
            'value_kind': 'number',
            'unit': spec['unit'],
            'unit_options': source_units,
            **_dated_with_canonical(descriptor.get(canonical)),
        }
        if spec.get('unit_field'):
            descriptor[spec['unit_field']] = {
                'kind': KIND_SELECTABLE,
                'writable': False,
                'qualifies': source,
                'unit_options': COUNT_UNIT_OPTIONS,
                'reason': (
                    f'Unit of {source}; selected alongside that value and '
                    f'stored with it. The canonical {spec["canonical_unit"]} '
                    'value is derived from the pair.'
                ),
            }
        canonical_entry = descriptor.get(canonical) or {}
        descriptor[canonical] = {
            **canonical_entry,
            'kind': KIND_COMPUTED,
            'writable': False,
            'value_kind': 'number',
            'inputs': [source],
            'normalised_read_model': True,
            'reason': (
                f'Normalised from {source} in {spec["canonical_unit"]}; '
                'the shared form edits that field and its unit.'
            ),
            'unit': spec['canonical_unit'],
        }
    # These values are inferred by the matcher and can be overridden by the
    # patient; the flag tells a federated client to offer the editor rather
    # than treating every recomputed attribute as read-only (#751). Applied
    # last: later blocks rebuild some of these entries.
    for field in (
        'treatment_refractory_status',
        'progression',
        'stem_cell_transplant_history',
        # A stored fact, but EXACT drops it when prior therapy is 'None' — the
        # one case where a transplant date cannot be true. The patient may
        # still correct it, so offer the editor beside the derived day count.
        'sct_date',
    ):
        entry = descriptor.get(field)
        if entry is not None and entry.get('writable'):
            entry['overridable'] = True

    # A unit choice the column cannot store is worse than no picker: a client
    # that sends it is refused. Keep only stored codes; a pair left with none
    # (RBC: curated 10*6/uL and 10*12/L, column stores CELLS/UL and CELLS/L)
    # advertises no list until its codes and migration land.
    record_fields = {field.name: field for field in PatientRecord._meta.fields}
    for units_field, entry in list(descriptor.items()):
        if not units_field.endswith('_units') or 'unit_options' not in entry:
            continue
        column = record_fields.get(units_field)
        if column is None or not column.choices:
            continue
        stored = {code for code, _label in column.choices}
        kept = [option for option in entry['unit_options'] if option['value'] in stored]
        qualified = descriptor.get(entry.get('qualifies') or '')
        for target in (entry, qualified):
            if target is None or 'unit_options' not in target:
                continue
            if kept:
                target['unit_options'] = kept
            else:
                del target['unit_options']

    # The envelope goes last so no field block can overwrite it. It is a dict
    # like any entry, but it is not one: `kind` is absent on purpose, and the
    # form enrichment skips reserved keys.
    descriptor[DESCRIPTOR_META_KEY] = {
        'schema_version': DESCRIPTOR_SCHEMA_VERSION,
        'documentation': 'docs/writable-field-descriptor.md',
    }

    return descriptor
