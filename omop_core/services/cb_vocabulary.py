"""CancerBot's controlled vocabularies as a Code Mapping source (#1649).

CancerBot (CB) matches trials on its own codes -- ``multiple_myeloma``,
``1q21Amplification``, ``speak__en`` -- and needs the OMOP concept each one
means. The mapping is reviewed here, in the Code Mapping queue, and exported
back; CB loads it with its own gates. Three commands share this module:
``import_cb_vocabularies`` seeds the queue from CB's proposal file,
``approve_cb_mappings`` signs off the categories that need no second look, and
``export_cb_mappings`` writes the complete decision set back out.

**Identity.** One queue row per CB term: ``source_vocabulary_id='CB'`` and
``source_code='<table>:<code>'``, e.g. ``cytogenicmarker:1q21Amplification``.
The table is part of the key because CB reuses codes across tables with
different meanings (``positive`` is an ER, a PR and a HER2 status).

**Destinations travel by natural key.** The proposal file names each concept as
``(vocabulary_id, concept_code)``, never by ``concept_id`` alone: an HK-*
concept is minted per instance, so its id on one deployment means nothing on
another.

CB terms are catalog entries, not codes that arrive in patient data; see
``source_vocabularies.CATALOG_SOURCE_VOCABULARIES`` for what approving one
must not do.
"""
import csv
from dataclasses import dataclass, field
from pathlib import Path

from omop_core.models import Concept

SOURCE_VOCABULARY_ID = 'CB'
ORIGIN_SYSTEM = 'cancerbot'
MAPPING_SOURCE = 'CancerBot'

#: Origins recorded on MappingDestinationCandidate rows, by what CB proposed.
ORIGIN_PROPOSAL = 'cb-proposal'
ORIGIN_CONCEPT_SET = 'cb-concept-set'
ORIGIN_VALUE = 'cb-value'

#: The reserved OMOP range for locally minted concepts. A concept in it that is
#: labelled as a public vocabulary (SNOMED, LOINC) is a seeding defect, not the
#: real thing (promop #461, #1223).
LOCAL_CONCEPT_ID_FLOOR = 2_000_000_000

REQUIRED_COLUMNS = ('table', 'code', 'title', 'match')
SOURCE_CODE_MAX = 100


@dataclass(frozen=True)
class ConceptKey:
    vocabulary_id: str
    concept_code: str

    def __str__(self):
        return f'{self.vocabulary_id}|{self.concept_code}'


@dataclass
class ProposalRow:
    """One CB term and what the proposal file says it means."""
    table: str
    code: str
    title: str
    match: str
    confidence: str = ''
    verified: str = ''
    note: str = ''
    domain_id: str = ''
    trial_count: int = 0
    proposal: ConceptKey | None = None
    concept_set: list = field(default_factory=list)
    value: ConceptKey | None = None

    @property
    def source_code(self):
        return f'{self.table}:{self.code}'

    @property
    def description(self):
        # The table rides along in the label, not only in the key: the review
        # queue groups rows by normalised label, and "Positive" from three
        # receptor tables must stay three questions, not one click.
        return f'{self.title} ({self.table})'[:255]

    def candidate_keys(self):
        """Every destination CB named for this term, each with its origin."""
        seen = {}
        if self.proposal:
            seen.setdefault(self.proposal, set()).add(ORIGIN_PROPOSAL)
        for key in self.concept_set:
            seen.setdefault(key, set()).add(ORIGIN_CONCEPT_SET)
        if self.value:
            seen.setdefault(self.value, set()).add(ORIGIN_VALUE)
        return seen


def _concept_key(vocabulary_id, concept_code):
    vocabulary_id, concept_code = (vocabulary_id or '').strip(), (concept_code or '').strip()
    if not vocabulary_id and not concept_code:
        return None
    if not vocabulary_id or not concept_code:
        raise ValueError(f'half a concept key: vocabulary={vocabulary_id!r} code={concept_code!r}')
    return ConceptKey(vocabulary_id, concept_code)


def _concept_set(raw):
    """``VOCAB|CODE;VOCAB|CODE`` -- ``|`` because HK codes contain ``:``."""
    keys = []
    for item in (raw or '').split(';'):
        item = item.strip()
        if not item:
            continue
        vocabulary_id, sep, concept_code = item.partition('|')
        if not sep:
            raise ValueError(f'concept_set item without "|": {item!r}')
        keys.append(_concept_key(vocabulary_id, concept_code))
    return keys


def read_proposals(path):
    """Parse CB's proposal CSV. Fails closed: one bad row stops the whole file.

    A partial read is the dangerous outcome here -- the export is meant to be a
    complete set, and CB clears concepts for terms it no longer sees.
    """
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        reader = csv.DictReader(stream)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f'{path.name}: missing columns {missing}')
        rows, seen = [], {}
        for line, raw in enumerate(reader, start=2):
            try:
                row = ProposalRow(
                    table=raw['table'].strip(),
                    code=raw['code'].strip(),
                    title=(raw.get('title') or '').strip(),
                    match=raw['match'].strip(),
                    confidence=(raw.get('confidence') or '').strip(),
                    verified=(raw.get('verified') or '').strip(),
                    note=(raw.get('note') or '').strip(),
                    domain_id=(raw.get('omop_domain') or '').strip(),
                    trial_count=int(raw.get('trial_count') or 0),
                    proposal=_concept_key(raw.get('omop_vocabulary_id'), raw.get('omop_concept_code')),
                    concept_set=_concept_set(raw.get('concept_set')),
                    value=_concept_key(raw.get('value_vocabulary_id'), raw.get('value_concept_code')),
                )
            except (ValueError, KeyError) as exc:
                raise ValueError(f'{path.name} line {line}: {exc}') from exc
            if not row.table or not row.code:
                raise ValueError(f'{path.name} line {line}: empty table or code')
            if len(row.source_code) > SOURCE_CODE_MAX:
                raise ValueError(f'{path.name} line {line}: key longer than {SOURCE_CODE_MAX}: {row.source_code}')
            # The unique constraint is case-sensitive but resolution matches
            # case-insensitively, so two keys differing only in case would be
            # one term to every reader and two rows to the database.
            folded = row.source_code.lower()
            if folded in seen:
                raise ValueError(f'{path.name} line {line}: duplicate key {row.source_code} (line {seen[folded]})')
            seen[folded] = line
            rows.append(row)
    return rows


def resolve_concepts(keys):
    """Map ConceptKey -> Concept for the keys that exist on this instance."""
    keys = set(keys)
    if not keys:
        return {}
    found = Concept.objects.filter(
        vocabulary_id__in={k.vocabulary_id for k in keys},
        concept_code__in={k.concept_code for k in keys},
    ).only('concept_id', 'concept_code', 'concept_name', 'vocabulary_id', 'domain_id',
           'standard_concept', 'invalid_reason')
    by_key = {ConceptKey(c.vocabulary_id, c.concept_code): c for c in found}
    return {k: by_key[k] for k in keys if k in by_key}


def destination_problem(concept):
    """Why this concept cannot be a CB term's destination, or '' if it can.

    Standardness is deliberately not a condition. HK-* concepts are accepted on
    a par with standard ones (product decision, cancerbot #5363), and CB's
    committed therapy crosswalk is built on HemOnc component concepts that are
    non-standard in CTOMOP. A CB row is a catalog entry, so a non-standard
    destination never reaches ingest here; ``standard_concept`` travels in the
    export and CB decides what it activates.

    What is refused is a destination that is broken rather than debatable:
    absent, retired or replaced, or a local-range id posing as a public
    vocabulary.
    """
    if concept is None:
        return 'not on this instance'
    if concept.invalid_reason:
        return f'invalid_reason={concept.invalid_reason}'
    if concept.concept_id >= LOCAL_CONCEPT_ID_FLOOR and not (concept.vocabulary_id or '').startswith('HK-'):
        return f'local-range id labelled {concept.vocabulary_id}'
    return ''
