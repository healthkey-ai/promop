"""Which standard concepts matter for a disease, for extraction services (#1942).

Meaning (source term to concept) is served by ``code-mappings/lookup``. This
is the second layer: given a disease, which concepts a document extractor
should emphasise. It is built from the fields the patient detail view shows for
that disease, joined to their **approved** ``FieldConceptMapping`` rows, so it
grows as field curation does (#1611, #1076) and never invents a concept.

Three tiers:

* ``disease``: the fields of the disease's extension (its
  ``FieldDiseaseMembership`` rows, #2331) and the genomics priority markers
  the catalog lists for it (#1180).
* ``common``: base-record clinical fields (general clinical, blood counts,
  chemistry, treatment), in no disease's extension.
* ``curated``: concepts a curator accepted from an extractor's
  absent-from-relevance signal (``RelevanceSignal``).

The version changes whenever anything a response could contain changes, so a
service that pins it on a run can tell later that the run used a stale set.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from omop_core.services import disease_profiles
from omop_core.services.genomics_catalog import catalog as genomics_catalog, disease_code, markers

# Bump when the field lists below change meaning, not when curation changes:
# curation is already in the content hash.
CATALOG_VERSION = 1

_COMMON_TABS = frozenset({'general', 'disease', 'blood', 'labs', 'treatment'})
_NOT_CLINICAL = frozenset({'internal', 'profile', 'location', 'alias'})


def diseases() -> tuple[str, ...]:
    """The diseases a set exists for: every Disease Category but ``Other``.

    A category a curator adds is supported at once, with an empty extension:
    its set is the common tier, its genomics markers and its curated concepts.
    """
    return disease_profiles.extension_codes()


def supported(code: str | None) -> bool:
    return code in diseases()


def resolver():
    """A function from a code or disease name to a supported disease, or ``None``.

    Reads the categories once, so a batch of signals is not a query per row.
    A code matches in any case, as a curator may have typed it.
    """
    resolve = disease_profiles.resolver()

    def supported_code(value):
        code = resolve(value)
        return None if code == disease_profiles.OTHER else code

    return supported_code


def _common_fields(field_names, claimed=None) -> set[str]:
    """The approved base fields on a common clinical tab, as the curator placed them.

    ``claimed`` is every disease-specific field; read here when not passed.

    Placement is the descriptor's resolved tab (``FieldConceptMapping.tab``
    first, then the code, #2328): a field a curator moved to Labs counts, one
    moved to ``omop`` (unplaced) does not.
    """
    from omop_core.models import FieldConceptMapping
    from omop_core.services.field_descriptor import _classify_field, resolve_tab

    placed = dict(FieldConceptMapping.objects.filter(field_name__in=field_names)
                  .values_list('field_name', 'tab'))
    if claimed is None:
        claimed = set(disease_profiles.memberships())
    return {f for f in field_names
            if f not in claimed
            and resolve_tab(f, mapping=_Placement(placed.get(f, ''))) in _COMMON_TABS
            and _classify_field(f) not in _NOT_CLINICAL}


class _Placement:
    """Just enough of a mapping row for ``resolve_tab``: its tab."""
    __slots__ = ('tab',)

    def __init__(self, tab: str):
        self.tab = tab


def _disease_fields(code: str, extension=None) -> set[str]:
    """The extension's fields plus the catalog's priority markers for the disease.

    The markers stay in the catalog: a priority is not a scope, and a marker
    one disease prioritises is still a field of every patient's record.
    """
    if extension is None:
        extension = disease_profiles.extension_fields(code)
    genomic = {m['field_name'] for m in markers() if code in m['diseases']}
    return set(extension) | genomic


def _approved_mappings() -> dict[str, dict]:
    from omop_core.models import FieldConceptMapping

    rows = (FieldConceptMapping.objects
            .filter(status='approved', concept__isnull=False)
            .values('field_name', 'omop_table', 'concept_id', 'concept__concept_name',
                    'concept__vocabulary_id', 'concept__concept_code', 'concept__domain_id'))
    return {r['field_name']: r for r in rows}


def _synonyms(concept_ids) -> dict[int, list[str]]:
    from omop_core.models import ConceptSynonym

    names = defaultdict(set)
    for concept_id, name in (ConceptSynonym.objects.filter(concept_id__in=concept_ids)
                             .values_list('concept_id', 'concept_synonym_name')):
        names[concept_id].add(name)
    return {k: sorted(v) for k, v in names.items()}


def _accepted_signals() -> dict[str, set[int]]:
    from omop_core.models import RelevanceSignal

    accepted = defaultdict(set)
    for disease, concept_id in (RelevanceSignal.objects.filter(status='accepted')
                                .values_list('disease', 'concept_id').distinct()):
        accepted[disease].add(concept_id)
    return accepted


def _build() -> dict:
    """Every disease's set, plus the version that names the whole state."""
    from omop_core.models import Concept

    mappings = _approved_mappings()
    scoped = disease_profiles.memberships()
    extensions = defaultdict(set)
    for field, codes in scoped.items():
        for code in codes:
            extensions[code].add(field)
    common = _common_fields(mappings, claimed=set(scoped))
    accepted = _accepted_signals()
    curated_ids = set().union(*accepted.values()) if accepted else set()
    curated = {c['concept_id']: c for c in Concept.objects.filter(concept_id__in=curated_ids).values(
        'concept_id', 'concept_name', 'vocabulary_id', 'concept_code', 'domain_id')}
    sets = {}
    for code in diseases():
        entries, seen = [], set()
        specific = _disease_fields(code, extensions[code])
        for tier, fields in (('disease', specific), ('common', common - specific)):
            for field in sorted(fields):
                row = mappings.get(field)
                if row is None:
                    continue
                seen.add(row['concept_id'])
                entries.append({
                    'concept_id': row['concept_id'], 'name': row['concept__concept_name'],
                    'vocabulary_id': row['concept__vocabulary_id'],
                    'concept_code': row['concept__concept_code'],
                    'domain_id': row['concept__domain_id'],
                    'omop_table': row['omop_table'], 'field_name': field, 'tier': tier,
                })
        for concept_id in sorted(accepted.get(code, ())):
            concept = curated.get(concept_id)
            if concept is None or concept_id in seen:
                continue
            entries.append({
                'concept_id': concept_id, 'name': concept['concept_name'],
                'vocabulary_id': concept['vocabulary_id'], 'concept_code': concept['concept_code'],
                'domain_id': concept['domain_id'], 'omop_table': None, 'field_name': None,
                'tier': 'curated',
            })
        sets[code] = {
            'concepts': entries,
            # Fields that belong to the set but have no approved concept yet:
            # how far curation still has to go, not an error.
            'unmapped_fields': sorted(f for f in specific if f not in mappings),
        }
    digest = hashlib.sha256(json.dumps(sets, sort_keys=True, default=str).encode()).hexdigest()[:12]
    version = f"{CATALOG_VERSION}.{genomics_catalog()['version']}.{digest}"
    return {'version': version, 'sets': sets}


def current_version() -> str:
    return _build()['version']


def relevance(codes) -> dict:
    """The union of the given diseases' sets, each concept listing its diseases.

    A concept in several sets appears once, with the strongest tier it holds
    (disease, then curated, then common).
    """
    built = _build()
    rank = {'disease': 0, 'curated': 1, 'common': 2}
    merged: dict[tuple, dict] = {}
    for code in codes:
        for entry in built['sets'][code]['concepts']:
            key = (entry['concept_id'], entry['field_name'])
            existing = merged.get(key)
            if existing is None:
                merged[key] = {**entry, 'diseases': [code]}
                continue
            existing['diseases'].append(code)
            if rank[entry['tier']] < rank[existing['tier']]:
                existing['tier'] = entry['tier']
    concepts = sorted(merged.values(), key=lambda e: (rank[e['tier']], e['name'] or '', e['field_name'] or ''))
    synonyms = _synonyms({e['concept_id'] for e in concepts})
    for entry in concepts:
        entry['synonyms'] = synonyms.get(entry['concept_id'], [])
    return {
        'version': built['version'],
        'diseases': list(codes),
        'concepts': concepts,
        'unmapped_fields': {code: built['sets'][code]['unmapped_fields'] for code in codes},
    }


def patient_diseases(person) -> list[str]:
    """Every Disease Category the record names, primary first: its profile.

    ``PatientRecord.disease`` is the primary cancer; any other diagnosis on the
    record that falls in a category follows it, sorted. A patient with more
    than one cancer is served the union of the sets (see ``relevance``) and of
    the extensions (``disease_profiles``). ``Other`` is never returned: its
    profile is the base record.
    """
    from omop_core.models import PatientRecord
    from omop_core.services.disease_episodes import disease_keys_on_record

    record = PatientRecord.objects.filter(person=person).only('disease').first()
    known = set(diseases())
    primary = disease_code(record.disease) if record else None
    others = {disease_code(key) for key in disease_keys_on_record(person)}
    others = sorted(c for c in others if c in known and c != primary)
    return ([primary] if primary in known else []) + others
