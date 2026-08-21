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

from omop_core.models import Concept
from omop_core.services.mappings import CONCEPT_LAB_TYPE, LAB_FIELD_TO_LOINC
from omop_core.services.patient_record_service import (
    PATIENT_RECORD_OMOP_MAPPED_FIELDS,
    _LAB_FIELD_ALIASES,
)

# Every field resolves to exactly one kind. A binary writable/not left a third of
# the record looking broken — a field is not "unwritable" because it is a unit
# picker or because it is height and weight multiplied together.
KIND_EDITABLE = 'editable'      # write an OMOP fact; derivation follows
KIND_SELECTABLE = 'selectable'  # choose from a bounded set, carried on the fact
KIND_COMPUTED = 'computed'      # derived from other fields; never authored alone
KIND_ALIAS = 'alias'            # mirrors a canonical field; edit that one instead

_NO_MAPPING_REASON = (
    'No reviewed concept set for this field yet — it cannot be written as a '
    'complete OMOP fact. See docs/omop_to_patientrecord.md.'
)

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

        mapping = LAB_FIELD_TO_LOINC.get(field)
        if mapping is None:
            descriptor[field] = {
                'kind': None, 'writable': False, 'reason': _NO_MAPPING_REASON,
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
    return descriptor
