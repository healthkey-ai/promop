"""Descriptor-driven single-field OMOP write (CB #2omop-federated-UI Phase 2 / A2).

`build_writable_field_descriptor()` says WHAT a PatientRecord field is and HOW it is
written; this module APPLIES it. It is the clean, projection-name-keyed write path the
federated PatientInfo editor uses, distinct from the CB-interim `omop_write_service`
(`sync_to_omop`, keyed on CB field names) which it will eventually replace (Phase 4d).

- KIND_EDITABLE  → write the OMOP Measurement/Observation fact the descriptor names
  (its own `concept_id` / `unit_concept_id` / `type_concept_id` / `source_value` /
  `target`), then re-derive the PatientRecord once. This is the new code here.
- KIND_PROFILE   → a Person attribute (not a clinical fact). The demographic
  (gender/race/ethnicity, concept-resolved) and location (city/region/postal_code/
  country/latitude/longitude → the Person's OMOP Location row) groups are written here,
  mirroring the persons endpoint's own logic (`patient_portal` PersonViewSet), so the
  federated editor saves them in one PATCH. The remaining `_PROFILE_REPLACEABLE`
  identity/admin fields (email/phone/validated/…) are surfaced in `result.profile` for a
  caller that wants to route them elsewhere — this clinical applier does not write them.
- everything else (computed / alias / authored / unmapped / unit picker / a field the
  descriptor marks `writable: False`) is rejected with the descriptor's own reason, so
  the caller can report exactly why rather than guessing.

Call inside the caller's `transaction.atomic()`: a failed fact write must roll back the
whole PATCH so the read model never diverges from the OMOP source of truth.
"""
import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from omop_core.models import Concept, Location, Measurement, Observation, Person
from omop_core.services.pk import next_pk
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.write_descriptor import (
    build_writable_field_descriptor,
    KIND_EDITABLE,
    KIND_PROFILE,
)

logger = logging.getLogger('audit')

# KIND_PROFILE groups this applier writes directly on Person (the rest — email/phone/
# validated/… — are identity/admin fields it leaves for result.profile).
_DEMOGRAPHIC_FIELDS = ('gender', 'race', 'ethnicity')
# projection field → OMOP Location column (mirrors the descriptor's _PROFILE_LOCATION and
# the derivation's Location read-back: region↔state, postal_code↔zip).
_LOCATION_COLUMN = {
    'city': 'city', 'region': 'state', 'postal_code': 'zip',
    'country': 'country', 'latitude': 'latitude', 'longitude': 'longitude',
}
_LOCATION_DECIMAL = ('latitude', 'longitude')
# Derived fields whose LOINC is shared and disambiguated ONLY by a qualifier_source_value: the
# fact must carry it or the derivation routes the value to the wrong projection (21889-1 is Size
# Tumor; a lymph-node reading needs 'lymph-node' or _get_cll_data won't read it back). The
# qualifier is part of the upsert key so it round-trips and never collides with the tumor_size row.
_FIELD_QUALIFIER = {'largest_lymph_node_size': 'lymph-node'}
# Same bounds the persons endpoint enforces (patient_portal PersonViewSet) — OMOP Location
# column widths and coordinate ranges. A violating value is a client error (→ ValueError →
# 400 at the caller), never a silent bad coordinate or a Postgres DataError/overflow (500).
_LOCATION_MAXLEN = {'city': 50, 'state': 2, 'zip': 9, 'country': 100}
_LOCATION_RANGE = {'latitude': (Decimal('-90'), Decimal('90')),
                   'longitude': (Decimal('-180'), Decimal('180'))}
# value_as_number is DecimalField(max_digits=15, decimal_places=5): at most 10 integer digits.
_VALUE_MAX = Decimal(10) ** 10

# Provenance: patient-authored facts get the OMOP 'Patient self-report' type concept (32865, the
# same one wearable rows use — see mappings.WEARABLE_TYPE_CONCEPT_ID). This is what makes the
# same-day upsert safe: it is scoped to rows of THIS type, so it never overwrites an imported Lab
# (32856) or EHR (32817) fact. The derivation reads facts by (person, is_erroneous=False) only —
# NOT by type — so a patient-reported fact is still projected. A profile write is, definitionally,
# a patient self-report.
PATIENT_REPORTED_TYPE_CONCEPT_ID = 32865

# genetic_mutations is a LIST field, not a scalar: each mutation is one Measurement keyed by its gene
# LOINC (patient_record_service._GENETIC_MUTATION_LOINCS), with variant→value_as_string, origin→
# qualifier_concept, interpretation→value_as_concept (SNOMED). The widget sends {gene, mutation (=the
# variant), origin, interpretation}. These SNOMED ids mirror the derivation's read maps.
_MUTATION_ORIGIN_SNOMED = {'germline': 255395001, 'somatic': 255461003}
_MUTATION_INTERPRETATION_SNOMED = {'pathogenic': 30166007, 'benign': 10828004, 'vus': 42425007}

# Provenance sentinel (measurement_source_value) marking a mutation Measurement as authored by the CB
# profile editor. The list-diff upsert & retire touch ONLY rows carrying it, so an imported genomic fact
# that legitimately also uses the shared 'Patient self-report' type (32865) — e.g. a FHIR
# patient-reported result — is never overwritten or entered-in-error'd. This is the explicit ownership
# marker the shared 32865 type cannot provide (the scalar path refuses clears for exactly this reason,
# #4833). The gene is read back from concept_code (a LOINC code), so this non-LOINC source_value does
# not disturb the derivation's gene resolution.
_GENETIC_MUTATION_SOURCE = 'cb-profile:genetic-mutation'


def _patient_reported_type():
    """The 'Patient self-report' type concept (32865, vocab 'Type Concept') all patient-authored facts
    carry. Fail closed if absent or a look-alike lives in another vocab (see the write comment)."""
    tc = Concept.objects.filter(
        concept_id=PATIENT_REPORTED_TYPE_CONCEPT_ID, vocabulary_id='Type Concept').first()
    if tc is None:
        raise ValueError(
            f"type concept {PATIENT_REPORTED_TYPE_CONCEPT_ID} (Patient self-report, vocab "
            "'Type Concept') is not loaded")
    return tc


class FieldWriteResult:
    """Outcome of an apply_field_writes call.

    applied  — projection fields written as OMOP facts here.
    profile  — {field: value} for KIND_PROFILE fields the caller must route to the
               persons endpoint (this module does not write Person attributes).
    rejected — {field: reason} for anything not writable, with the descriptor's reason.
    """

    def __init__(self):
        self.applied = []
        self.profile = {}
        self.rejected = {}

    @property
    def wrote_fact(self):
        return bool(self.applied)


def apply_field_writes(person, changes, today=None, descriptor=None):
    """Apply {projection_field: value} edits to `person` as descriptor-driven OMOP facts.

    Writes KIND_EDITABLE facts, buckets KIND_PROFILE for the persons endpoint, rejects the
    rest. Re-derives the PatientRecord ONCE after all fact writes (the per-fact refresh
    signal is suppressed on each write). Returns a FieldWriteResult. Raises on a genuine
    write failure — the caller's atomic() must roll the PATCH back.
    """
    if today is None:
        today = date.today()
    if descriptor is None:
        descriptor = build_writable_field_descriptor()

    result = FieldWriteResult()
    person_dirty = set()      # Person columns changed by demographic writes
    location_updates = {}     # OMOP Location column -> value, applied once as an upsert

    # Serialize concurrent writes for this person: two PATCHes for the same person/field/day could
    # otherwise both miss the same-day upsert row and each INSERT a duplicate (there is no DB
    # uniqueness constraint on these keys). Hold a row lock on the Person for the caller's
    # transaction. Only meaningful inside one — the applier's documented contract — so guard on it.
    from django.db import connection
    if changes and connection.in_atomic_block:
        list(Person.objects.select_for_update().filter(pk=person.pk))

    for field, value in changes.items():
        if field == 'genetic_mutations':
            # A LIST field (one Measurement per gene), so it takes the list-diff path rather than the
            # scalar descriptor kinds below. An empty list is a valid "no mutations" (clears our rows),
            # so it is handled here BEFORE the None/'' clear-guard that rejects scalar clears.
            if isinstance(value, list):
                _write_genetic_mutations(person, value, today)
                result.applied.append(field)
            else:
                result.rejected[field] = 'genetic_mutations must be a list of mutations.'
            continue
        d = descriptor.get(field)
        if d is None:
            result.rejected[field] = 'Unknown field; not part of the writable record.'
            continue
        if value is None or (isinstance(value, str) and value.strip() == ''):
            # A clear, not a write (the widget's inputs emit '' on clear, not null). There is no
            # provenance marker distinguishing a profile-write fact from an imported one, so
            # deleting the backing fact could destroy real imported data — a clear is refused, not
            # silently applied (safe-delete is deferred, #4833). Reported (never raised) so a batch
            # PATCH carrying an emptied field does not fail the whole request.
            result.rejected[field] = 'Clearing a value is not supported yet (#4833).'
            continue
        kind = d.get('kind')
        if kind == KIND_EDITABLE and d.get('writable'):
            _write_editable_fact(person, field, d, value, today)
            result.applied.append(field)
            continue
        if kind == KIND_PROFILE and d.get('writable'):
            if _stage_profile_write(person, field, value, person_dirty, location_updates):
                result.applied.append(field)
            else:
                # An identity/admin field (email/phone/validated/…) — not this applier's job.
                result.profile[field] = value
            continue
        # Writable but not a shape this applier writes, or writable=False.
        result.rejected[field] = d.get('reason') or f'{field} ({kind}) has no direct write path.'

    # Apply the staged Person/Location edits, then re-derive ONCE if anything landed (each
    # fact write suppresses its own refresh signal, so the read model is rebuilt from the
    # fully-applied state, never a half-applied one).
    if location_updates:
        _apply_location(person, location_updates, person_dirty)
    if person_dirty:
        person.save(update_fields=sorted(person_dirty))
    if result.applied:
        refresh_patient_record(person)
    return result


def owned_writable_fields(descriptor=None):
    """The set of projection fields THIS applier actually persists (so an editor can offer exactly
    those and keep the rest non-editable). It is narrower than the descriptor's `writable` flag:
    the descriptor marks the _PROFILE_REPLACEABLE identity fields (email/phone/…) writable at the
    persons endpoint, but apply_field_writes defers those — so they are NOT here. = KIND_EDITABLE
    facts + the demographic/location KIND_PROFILE groups this applier writes on the Person."""
    if descriptor is None:
        descriptor = build_writable_field_descriptor()
    owned = set(_DEMOGRAPHIC_FIELDS) | set(_LOCATION_COLUMN)
    fields = {
        f for f, e in descriptor.items()
        if e.get('writable') and (e.get('kind') == KIND_EDITABLE or f in owned)
    }
    # genetic_mutations is a list field written via the list-diff path (not a descriptor kind), so add
    # it here — but only when its vocab is loaded, mirroring KIND_EDITABLE's "writable iff concept resolves".
    if _genetic_mutations_writable():
        fields.add('genetic_mutations')
    return fields


def _genetic_mutations_writable():
    """True when genetic_mutations can round-trip: the patient-report type concept AND at least one
    reviewed gene LOINC are loaded. Keeps editable_fields honest on a stack with a partial vocab."""
    from omop_core.services.patient_record_service import _GENETIC_MUTATION_LOINCS
    if not Concept.objects.filter(
            concept_id=PATIENT_REPORTED_TYPE_CONCEPT_ID, vocabulary_id='Type Concept').exists():
        return False
    return Concept.objects.filter(
        concept_code__in=list(_GENETIC_MUTATION_LOINCS), vocabulary_id='LOINC').exists()


def _stage_profile_write(person, field, value, person_dirty, location_updates):
    """Stage a KIND_PROFILE write onto `person`. Returns True if this applier owns the field
    (demographic or location — staged for the batched save/upsert), False for identity/admin
    fields it deliberately leaves to the caller (bucketed into result.profile)."""
    if field in _DEMOGRAPHIC_FIELDS:
        from omop_core.services.demographics import resolve_concept
        # resolve_concept returns None for a non-curated value OR an unloaded vocabulary; per its
        # contract the caller must then clear the stale concept and keep the raw text in
        # *_source_value (which still derives), so an unmapped value never keeps an old concept.
        setattr(person, f'{field}_concept', resolve_concept(field, value))
        setattr(person, f'{field}_source_value', None if value is None else str(value)[:50])
        person_dirty.update({f'{field}_concept', f'{field}_source_value'})
        return True
    if field in _LOCATION_COLUMN:
        column = _LOCATION_COLUMN[field]
        if field in _LOCATION_DECIMAL:
            try:
                value = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                raise ValueError(f'{field}: expected a number, got {value!r}')
            # NaN parses as a Decimal but makes the range comparison raise InvalidOperation — reject
            # non-finite (NaN/Inf) as a 400, not a 500.
            if not value.is_finite():
                raise ValueError(f'{field}: expected a finite number, got {value!r}')
            lo, hi = _LOCATION_RANGE[field]
            if not (lo <= value <= hi):
                raise ValueError(f'{field}: must be between {lo} and {hi}')
        else:
            value = str(value).strip()
            max_len = _LOCATION_MAXLEN[column]
            if len(value) > max_len:
                # Match the persons endpoint: a too-long value is a 400, not a truncated write
                # or a Postgres DataError (e.g. a full state name into varchar(2)).
                raise ValueError(f'{field}: at most {max_len} characters (OMOP Location.{column})')
        location_updates[column] = value
        return True
    return False


def _apply_location(person, location_updates, person_dirty):
    """Upsert the Person's OMOP Location row with the staged columns, linking by id (Person.
    location_id is a plain IntegerField, not the FK). Reuse the linked row when it belongs to
    this Person alone; if the OMOP ETL deduplicated it across several Persons, CLONE it and
    relink only this Person so editing one patient's address never rewrites a co-located
    patient's (the persons endpoint mutates in place — this is stricter on purpose)."""
    location = None
    if person.location_id:
        location = Location.objects.filter(location_id=person.location_id).first()
    shared = (
        location is not None
        and Person.objects.filter(location_id=location.location_id)
        .exclude(person_id=person.person_id).exists()
    )
    if location is None or shared:
        # Start from the shared row's FULL set of columns (street/county/source_value/coords, not
        # just the projected six) so a partial edit keeps the rest of the address, then give this
        # Person their own copy.
        base = {}
        if shared:
            base = {
                f.name: getattr(location, f.name)
                for f in Location._meta.concrete_fields if f.name != 'location_id'
            }
        location = Location(location_id=next_pk(Location, 'location_id'), **base)
    changed = [c for c, v in location_updates.items() if getattr(location, c) != v]
    for column, value in location_updates.items():
        setattr(location, column, value)
    if location._state.adding:
        location.save()
        person.location_id = location.location_id
        person_dirty.add('location_id')
    elif changed:
        location.save(update_fields=changed)


def _coerce_value(value, value_kind):
    """Return (value_as_number, value_as_string) for the fact, per the descriptor value_kind.

    A numeric field stores value_as_number; anything else stores value_as_string. A number
    field that receives a non-numeric value is a client error, surfaced as ValueError so the
    PATCH fails closed rather than silently storing a null-valued fact.
    """
    if value is None:
        raise ValueError('cannot write a null value as a fact (clears are not a fact write)')
    if value_kind == 'number':
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(f'expected a number, got {value!r}')
        # value_as_number is DecimalField(15,5): guard the integer magnitude so an out-of-range
        # value is a 400, not a Postgres numeric overflow (500). Fractional scale beyond 5 places
        # is harmlessly rounded by PG.
        if not number.is_finite() or abs(number) >= _VALUE_MAX:
            raise ValueError(f'value {value!r} is out of range for a measurement')
        return number, None
    return None, str(value)[:60]


def _write_editable_fact(person, field, d, value, today):
    """Upsert the single OMOP Measurement/Observation the descriptor names for `field`.

    Upsert key is (person, concept, date=today): a same-day re-edit updates the row rather
    than appending a duplicate, mirroring the interim path's _sync_measurement. The post_save
    refresh signal is suppressed here; apply_field_writes re-derives once at the end.
    """
    target = d.get('target', 'measurement')
    concept = Concept.objects.filter(concept_id=d['concept_id']).first()
    if concept is None:
        # The descriptor only marks a field writable when its concept resolves, so a miss
        # here means the vocabulary changed under us — fail closed, do not strand the fact.
        raise ValueError(f"{field}: concept_id {d['concept_id']} is not loaded")
    # Patient-authored facts carry the 'Patient self-report' type concept (NOT the descriptor's
    # generic Lab type) — this both records provenance and scopes the upsert below to our own rows.
    # It is NOT NULL and a real *type* concept, so require it to resolve (never fall back to the
    # LOINC concept, which would corrupt the type dimension); a miss is a vocab misconfig → fail closed.
    # Pin the vocabulary: a deployment that once used a shadow vocab could hold a different concept at
    # 32865, so require the genuine OMOP 'Type Concept' row and fail closed otherwise.
    type_concept = _patient_reported_type()
    unit_concept = (
        Concept.objects.filter(concept_id=d['unit_concept_id']).first()
        if d.get('unit_concept_id') is not None else None
    )
    num, string = _coerce_value(value, d.get('value_kind', 'number'))

    # Upsert key scopes to OUR rows only: (person, concept, date, is_erroneous=False, type=Patient
    # self-report). Scoping by type is what keeps a same-day IMPORTED Lab/EHR fact for the same
    # concept untouched — it has a different type_concept, so it never matches. is_erroneous=False
    # also keeps us off entered-in-error rows (which the derivation excludes anyway). The type is a
    # KEY (not a mutable field): a matched row already has it, and a new row is created with it.
    qualifier = _FIELD_QUALIFIER.get(field)
    if target == 'observation':
        model, pk_order = Observation, 'observation_id'
        keys = {'person': person, 'observation_concept': concept, 'observation_date': today,
                'is_erroneous': False, 'observation_type_concept': type_concept,
                'qualifier_source_value': qualifier}
        fields = {
            'value_as_number': num,
            'value_as_string': string,
            'unit_concept': unit_concept,
            'observation_source_value': d.get('source_value'),
            'unit_source_value': d.get('unit'),
        }
    else:
        model, pk_order = Measurement, 'measurement_id'
        keys = {'person': person, 'measurement_concept': concept, 'measurement_date': today,
                'is_erroneous': False, 'measurement_type_concept': type_concept,
                'qualifier_source_value': qualifier}
        fields = {
            'value_as_number': num,
            'value_as_string': string,
            'unit_concept': unit_concept,
            'measurement_source_value': d.get('source_value'),
            'unit_source_value': d.get('unit'),
        }

    # Deterministic target when several of OUR same-day rows exist (was `.first()` with no ordering).
    existing = model.objects.filter(**keys).order_by(pk_order).first()
    if existing is not None:
        for attr, val in fields.items():
            setattr(existing, attr, val)
        existing._skip_patient_record_refresh = True
        existing.save(update_fields=list(fields.keys()))
        del existing._skip_patient_record_refresh
        return

    row = model(**{pk_order: next_pk(model, pk_order)}, **keys, **fields)
    row._skip_patient_record_refresh = True
    row.save()
    del row._skip_patient_record_refresh


def _write_genetic_mutations(person, mutations, today):
    """Write the genetic_mutations LIST as one Measurement per gene (the first list-diff write). Each
    sent mutation upserts its gene's Measurement (gene LOINC concept + our 'Patient self-report' type),
    with variant→value_as_string, origin→qualifier_concept, interpretation→value_as_concept (SNOMED).
    Our own mutation rows for a gene NO LONGER in the list are marked entered-in-error — a safe delete
    because we only ever touch 32865-typed rows we authored (an imported mutation has a different type).
    A mutation whose gene is not one of the reviewed genes, or whose gene LOINC is not loaded, is skipped.
    Returns the genes written."""
    from omop_core.services.patient_record_service import _GENETIC_MUTATION_LOINCS
    gene_to_code = {gene.upper(): code for code, gene in _GENETIC_MUTATION_LOINCS.items()}
    all_gene_codes = set(_GENETIC_MUTATION_LOINCS)
    type_concept = _patient_reported_type()

    def _resolve_attr(raw, mapping, kind, gene):
        # origin/interpretation are OPTIONAL (blank → None, omitted from the record). But a NON-EMPTY
        # value that does not resolve to a loaded SNOMED concept must fail closed, not silently drop:
        # either it is unsupported, or the vocab is only partially loaded — either way the edit would
        # not round-trip, so reject it rather than accept a write that reads back missing the value.
        key = str(raw or '').strip().lower()
        if not key:
            return None
        concept = Concept.objects.filter(concept_id=mapping[key]).first() if key in mapping else None
        if concept is None:
            raise ValueError(
                f'genetic_mutations ({gene}): {kind} {raw!r} is not a supported/loaded value')
        return concept

    written_codes, written_genes = set(), []
    for mut in mutations or []:
        if not isinstance(mut, dict):
            continue
        gene = str(mut.get('gene') or '').strip()
        code = gene_to_code.get(gene.upper())
        if not code:
            continue
        concept = Concept.objects.filter(concept_code=code, vocabulary_id='LOINC').first()
        if concept is None:
            continue
        # A recognized gene with no variant would write value_as_string=None, which the derivation skips
        # — the mutation would vanish from the record silently. Require the variant; fail closed instead.
        variant = str(mut.get('mutation') or '').strip()
        if not variant:
            raise ValueError(f'genetic_mutations ({gene}): a variant value is required')
        # source_value is a KEY (the ownership sentinel), so the upsert & retire only ever match rows we
        # authored — never an imported gene fact that happens to share the 32865 type.
        keys = {'person': person, 'measurement_concept': concept,
                'measurement_type_concept': type_concept, 'is_erroneous': False,
                'measurement_source_value': _GENETIC_MUTATION_SOURCE}
        fields = {
            'measurement_date': today,
            'value_as_string': variant[:60],
            'qualifier_concept': _resolve_attr(mut.get('origin'), _MUTATION_ORIGIN_SNOMED, 'origin', gene),
            'value_as_concept': _resolve_attr(
                mut.get('interpretation'), _MUTATION_INTERPRETATION_SNOMED, 'interpretation', gene),
        }
        existing = Measurement.objects.filter(**keys).order_by('measurement_id').first()
        if existing is not None:
            for a, v in fields.items():
                setattr(existing, a, v)
            existing._skip_patient_record_refresh = True
            existing.save(update_fields=list(fields.keys()))
            del existing._skip_patient_record_refresh
        else:
            row = Measurement(measurement_id=next_pk(Measurement, 'measurement_id'), **keys, **fields)
            row._skip_patient_record_refresh = True
            row.save()
            del row._skip_patient_record_refresh
        written_codes.add(code)
        written_genes.append(str(mut.get('gene')))

    # Fail closed on a non-empty list that produced NO write (every entry an unknown gene / non-dict):
    # the reconcile below would otherwise read the empty written_codes as "remove everything" and wipe
    # the patient's existing mutations from a malformed request. An intentional clear is an empty list.
    if mutations and not written_codes:
        raise ValueError(
            'genetic_mutations: no recognized gene in the submitted list; send [] to clear all mutations')

    # Reconcile removals: OUR authored mutation rows (the ownership sentinel) for genes no longer sent →
    # entered-in-error, so the derivation drops them. Scoped by source_value (never touches an import),
    # then by the reviewed gene codes AND the LOINC vocabulary (a concept_code is unique only within a
    # vocabulary, so without the vocab bound a same-code fact in another vocabulary could be wrongly hit).
    stale = Measurement.objects.filter(
        person=person, measurement_type_concept=type_concept, is_erroneous=False,
        measurement_source_value=_GENETIC_MUTATION_SOURCE,
        measurement_concept__vocabulary_id='LOINC',
        measurement_concept__concept_code__in=all_gene_codes,
    ).exclude(measurement_concept__concept_code__in=written_codes)
    for m in stale:
        m.is_erroneous = True
        m.erroneous_reason = 'removed via patient profile edit'
        m._skip_patient_record_refresh = True
        m.save(update_fields=['is_erroneous', 'erroneous_reason'])
        del m._skip_patient_record_refresh
    return written_genes
