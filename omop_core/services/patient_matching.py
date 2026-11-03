"""In-process patient adapter for the EXACT `exact_matching` matcher (CB ADR 0001, Slice 2).

CancerBot embeds `omop_core` as a pip plugin and feeds a PROMOP-derived patient into
the extracted `exact_matching` matcher. That matcher reads a patient object as a
**Django-model duck type**: plain ``getattr(patient, attr)`` for the mapped fields, plus
``patient.__class__._meta.get_field(attr)`` to classify field types in ``is_attr_blank``.

PROMOP's ``PatientRecord`` (a Django model) already carries the matcher's fields as real
columns — including the pre-expanded ``therapy_component_ids`` / ``therapy_type_ids`` — so
it satisfies the ``_meta`` requirement natively. This module wraps a **persisted**
``PatientRecord`` in a read-only adapter that:

- delegates ordinary field reads (and ``_meta``) straight to the record, and
- explicitly provides the few matcher reads whose shape/name differs from a raw column:
  ``therapy_release_id`` (a scalar the matcher reads but PatientRecord stores only inside
  ``therapy_ids_provenance``), ``pre_existing_condition_categories`` (a related-manager the
  matcher calls ``.values_list('category__code', ...)`` on), and ``supportive_therapies``
  (the matcher iterates ``{'therapy': code}`` entries).

**Read-only / no derivation on the read path.** ``resolved_patient_for_matching`` reads the
last persisted ``PatientRecord`` and never calls ``refresh_patient_record`` (which writes +
bumps ``derived_at``/``derivation_version``). A trial search consumes the persisted
projection snapshot; refresh happens on ingestion/update, not during matching.

**Interim tri-state.** No explicit ``{value, state, authority, provenance}`` envelope yet
(EXACT ADR 0004 / CB ADR 0001). Absence stays implicit: a person with no OMOP
``DrugExposure`` leaves therapy columns NULL/`[]`, which the matcher reads as *unknown*
(``get_user_therapy_component_ids() is None`` → fail-closed), never ``ASSERTED_NONE``. The
full per-field envelope is a later hardening slice (CB's ``ResolvedPatient._raw`` grows
into it).
"""
from __future__ import annotations

def _release_of(prov, id_field):
    """release_id token from a `therapy_ids_provenance` entry, else None."""
    entry = prov.get(id_field)
    return entry.get('release_id') if isinstance(entry, dict) else None


def _iter_release_lines(record, prov):
    """Yield ``(release_id, regimen_resolved, type_ids)`` for every therapy line that
    contributes drug-class "type" ids — the release-relevant projection of
    ``patient_portal.api.serializers.PatientRecordSerializer.get_lines_of_therapy``.

    This is a faithful port (not the aggregate shortcut) because the plugin host has no
    ``patient_portal`` to import the serializer from (Slice 1: omop_core is patient_portal-
    free). Only the fields the ADR-0002 release reduction needs are projected; dates /
    outcomes / component_ids are omitted.

    The subtlety it must preserve: ``get_lines_of_therapy`` stamps EVERY 3L+ line with the
    *aggregate* ``later_therapy_type_ids`` as its class contribution. So when later classes
    exist, one unresolved later line (regimen_concept_id is None) is a class-contributing line
    with no per-line release → the whole later block is uncertified. Reading only the aggregate
    ``later_therapy_ids`` (the resolved subset) would miss that and fail OPEN (#393 smear).
    """
    # 1L / 2L: a single regimen each; resolved iff the *_therapy_id column is truthy.
    for id_field, type_field in (
        ('first_line_therapy_id', 'first_line_therapy_type_ids'),
        ('second_line_therapy_id', 'second_line_therapy_type_ids'),
    ):
        tids = getattr(record, type_field, None) or []
        if tids:
            yield _release_of(prov, id_field), bool(getattr(record, id_field, None)), tids

    # 3L+: every later line carries the aggregate later_therapy_type_ids as its class
    # contribution, so if later classes exist each later line must be resolved.
    later_tids = getattr(record, 'later_therapy_type_ids', None) or []
    if not later_tids:
        return
    later_rel = _release_of(prov, 'later_therapy_ids')
    later_ids = getattr(record, 'later_therapy_ids', None) or []
    later_therapies = [lt for lt in (getattr(record, 'later_therapies', None) or [])
                       if isinstance(lt, dict)]
    if later_therapies:
        # Per-line resolution, aligned exactly as get_lines_of_therapy aligns it.
        if any('concept_id' in lt for lt in later_therapies):
            resolved_flags = [lt.get('concept_id') is not None for lt in later_therapies]
        else:
            # Legacy shape: resolved ids align only when counts match; otherwise no
            # later line can be mapped to an id -> every later line is unresolved.
            aligned = later_ids if len(later_ids) == len(later_therapies) else [None] * len(later_therapies)
            resolved_flags = [a is not None for a in aligned]
        for resolved in resolved_flags:
            yield later_rel, resolved, later_tids
    elif later_ids:
        # Oldest rows with no per-line list: one resolved line per stored id.
        for cid in later_ids:
            yield later_rel, cid is not None, later_tids
    else:
        # Only flat later_therapy text, no resolved id -> one unresolved line.
        yield later_rel, False, later_tids


def _therapy_release_id(record):
    """Patient-level aggregate therapy-vocab release, unanimous-or-null / fail-closed.

    Faithful port of ``PatientRecordSerializer.get_therapy_release_id`` (EXACT #286 Gate 1 /
    ADR 0002): the matcher overlaps the *aggregate* ``therapy_type_ids`` against trial type
    criteria, so one release must certify that whole union. Return a release only when every
    class-contributing line (see ``_iter_release_lines``, incl. 3L+ smear handling) agrees on
    one non-null release AND its regimen resolved; anything weaker → ``None``.
    """
    prov = getattr(record, 'therapy_ids_provenance', None)
    # therapy_ids_provenance is untrusted persisted JSON: a legacy/hand-edited row may
    # hold a non-dict (e.g. `[]`, a bare list). Normalize to {} so the reducer fails
    # closed (returns None) rather than raising on `.get` mid-search.
    if not isinstance(prov, dict):
        prov = {}
    releases = []
    covered = set()
    for rel, regimen_resolved, tids in _iter_release_lines(record, prov):
        if not tids:
            continue  # no class ids -> not part of the overlap to certify
        # A valid release token is a non-empty string; a class-contributing line whose
        # regimen did not resolve is uncertified. Either -> fail closed.
        if not isinstance(rel, str) or not rel or not regimen_resolved:
            return None
        releases.append(rel)
        covered.update(int(t) for t in tids)
    if not releases or len(set(releases)) != 1:
        return None
    # Defense-in-depth: every id in the stored aggregate must be vouched for by a
    # certified line above (a hand-built/corrupt aggregate row -> fail closed).
    if not {int(t) for t in (getattr(record, 'therapy_type_ids', None) or [])}.issubset(covered):
        return None
    return releases[0]


class _CategoryCodeManager:
    """Minimal stand-in for the matcher's ``pre_existing_condition_categories`` related
    manager: it only ever calls ``.values_list('category__code', flat=True)``. PROMOP stores
    the categories as a plain list of code strings in ``PatientRecord.preexisting_conditions``.
    """

    def __init__(self, codes):
        self._codes = [c for c in (codes or []) if c not in (None, '')]

    def values_list(self, *fields, flat=False):
        # The matcher calls values_list('category__code', flat=True); return the codes.
        return list(self._codes) if flat else [(c,) for c in self._codes]

    def all(self):
        return list(self._codes)

    def exists(self):
        return bool(self._codes)


class _MetaFacade:
    """Class-level ``_meta`` stand-in for the matcher.

    ``is_attr_blank`` classifies a field via ``type(patient.__class__._meta.get_field(attr))``
    for any mapped attr lacking a ``computed_value_type`` — accessing ``_meta`` on the CLASS,
    not the instance. ``MatchingPatient`` is not a Django model, so it exposes this facade as a
    class attribute. ``get_field`` returns the real ``PatientRecord`` field where one exists;
    for a matcher attr that ``PatientRecord`` has no column for (CB-``PatientInfo``-only
    computed/derived fields — e.g. bulky_disease_criteria, mipi_risk), it returns a synthetic
    ``CharField``. That is safe under the interim contract: such an attr reads as ``None``
    (absent → unknown), and ``is_attr_blank`` already sets ``is_blank=True`` for ``None`` BEFORE
    the field-type checks run, so the synthetic type is only a non-crashing placeholder.
    """

    def get_field(self, name):
        from django.core.exceptions import FieldDoesNotExist
        from django.db import models
        from omop_core.models import PatientRecord
        try:
            return PatientRecord._meta.get_field(name)
        except FieldDoesNotExist:
            return models.CharField()  # value is None (unknown) -> type is a placeholder


_META_FACADE = _MetaFacade()


class MatchingPatient:
    """Read-only, matcher-consumable view over a persisted ``PatientRecord``.

    Instance attribute reads fall through to the wrapped record; a matcher attr that
    ``PatientRecord`` has no column for reads as ``None`` (interim: absent → unknown). A few
    matcher-specific shapes are provided explicitly. The class exposes ``_meta`` (a
    ``_MetaFacade``) because the matcher reads ``__class__._meta.get_field``. Never mutates the
    record and never triggers derivation.
    """

    # Class attribute — the matcher accesses `patient.__class__._meta`, not the instance.
    _meta = _META_FACADE

    def __init__(self, record):
        object.__setattr__(self, '_record', record)

    # --- explicit matcher-facing shapes (name/shape differ from a raw column) ---

    @property
    def therapy_release_id(self):
        return _therapy_release_id(self._record)

    @property
    def pre_existing_condition_categories(self):
        # PatientRecord.preexisting_conditions is a JSON list of category code strings.
        return _CategoryCodeManager(getattr(self._record, 'preexisting_conditions', None))

    @property
    def supportive_therapies(self):
        # The matcher iterates this as a list of {'therapy': code} dicts
        # (get_supportive_therapy_codes / _match_supportive_therapies) and calls
        # ``item.get('therapy')``. PROMOP's PatientRecord.supportive_therapies is a
        # legacy free-text ``TextField`` the derivation pipeline does NOT populate, so
        # delegating the raw column would hand the matcher a str and it would iterate
        # characters, crashing on ``str.get``. Normalize: only a stored JSON list (of
        # dicts) is passed through; anything else (None, free text, non-list) reads as
        # ``[]`` — blank, i.e. no supportive requirement (interim: absent -> unknown).
        # FOLLOW-UP: planned_therapies / concomitant_medications are also TextField columns
        # the matcher can read as lists via custom_search uvalue_functions (disease/trial-
        # gated, off the unconditional match path), and mutation_genes/mutation_variants have
        # no PatientRecord column (read as None). They are the same TextField-vs-list class as
        # this property; a later slice should give them the same normalization once PROMOP
        # derives them structurally. Left as raw/None for now (interim: absent -> unknown).
        raw = getattr(self._record, 'supportive_therapies', None)
        if isinstance(raw, list):
            items = raw
        elif not raw:
            return []
        else:
            import json
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                return []
            items = parsed if isinstance(parsed, list) else []
        # The matcher does `item.get('therapy')` on every entry, so keep ONLY dict
        # entries — a JSON array of primitive codes (e.g. ["bisphosphonate"]) would
        # otherwise crash on str.get. Unsupported shapes drop to [] (blank).
        return [it for it in items if isinstance(it, dict)]

    # --- delegate to the record; missing matcher attrs read as None (interim unknown) ---

    def __getattr__(self, name):
        # Reached only for names not found on the class/instance (so not _meta, not the
        # properties above). Delegate to the record. Absence handling splits by name kind:
        #   - PRIVATE (`_`-prefixed): a matcher probe attr (e.g. `hasattr(patient,
        #     '_pre_existing_condition_categories')`) must reflect TRUE absence, so re-raise —
        #     otherwise the matcher takes the wrong branch and iterates a None.
        #   - PUBLIC matcher attr absent from PatientRecord (a CB-PatientInfo-only computed
        #     field): resolve to None = unknown (interim contract), never raise.
        record = object.__getattribute__(self, '_record')
        try:
            return getattr(record, name)
        except AttributeError:
            if name.startswith('_'):
                raise
            return None

    def __setattr__(self, name, value):
        # The matcher memoizes private caches on the patient object (e.g.
        # `_pre_existing_condition_codes_cache`); allow those on the ADAPTER instance so
        # memoization works, without ever mutating the wrapped record. Block writes to data
        # fields — a matching read path must not derive/mutate patient state.
        if name.startswith('_'):
            object.__setattr__(self, name, value)
            return
        raise AttributeError(
            'MatchingPatient is read-only; refresh the PatientRecord via the derivation '
            'pipeline, not the matching read path.'
        )

    def __repr__(self):
        return f'<MatchingPatient person_id={getattr(self._record, "person_id", None)}>'


def resolved_patient_for_matching(person):
    """Return a read-only, matcher-consumable ``MatchingPatient`` for ``person`` from the
    LAST PERSISTED ``PatientRecord``, or ``None`` if the person has no derived record yet.

    Read-only by contract: this never calls ``refresh_patient_record`` (a write). Callers that
    need a fresh projection must refresh explicitly on the ingestion/update path first; a
    missing record is surfaced as ``None`` rather than silently derived here.
    """
    from omop_core.models import PatientRecord
    person_id = getattr(person, 'pk', person)
    record = PatientRecord.objects.filter(person_id=person_id).first()
    if record is None:
        return None
    return MatchingPatient(record)
