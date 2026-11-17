"""Give one person a full provisional view: propose, materialise, rederive (#2696).

``manage.py fill_provisional_record <person_id>`` takes a patient through the
whole provisional chain in one resumable command, for the patient whose page
should show as much as the proposals can say while the codes are still being
mapped:

1. **Propose.** The person's unresolved and inventory codes are found, their
   organization-unscoped queue rows are located (or created, the way
   ``enqueue_unmapped_source_codes`` does), and the normal Suggest pipeline runs
   on those that are ``proposed`` with no destination, with the ranker the
   caller chose (Jev by default). Destinations, confidences, provenance and notes
   are written exactly as a Suggest run writes them; the rows stay ``proposed``.
2. **Materialise.** The person's rows are resolved with the sweep's own scope
   (:func:`omop_core.services.provisional_sweep.person_scope`), so a row becomes
   provisional at a proposal at or above ``PROVISIONAL_CONFIDENCE_FLOOR`` and a
   row is created for an inventory code that has none. The floor is a site
   setting and is never overridden here: a later sweep would take back anything
   below it. What fell below is reported, by band.
3. **Rederive.** The person's ``PatientRecord`` is rebuilt inline, which writes
   ``patient_record_provisional`` and ``patient_record_confidence``.

Nothing here reimplements Suggest, the resolver or the derivation; it chooses
which rows they run on. With ``PROVISIONAL_DATA_ENABLED`` off nothing is written.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Case, CharField, Count, F, Q, Value, When
from django.db.models.functions import Upper
from django.utils import timezone

from omop_core.mapping import suggestions
from omop_core.mapping.code_resolution import (
    _DOMAIN_FOR_TABLE, CLINICAL_TABLES, NO_MATCHING_CONCEPT_ID, SOURCE_CODE_MAX,
    _confidence_percent, _usable_destination, choose_proposal, normalize_omop_table,
)
from omop_core.models import (
    PatientRecordConfidence, PatientRecordProvisional, PatientSourceCode, Person,
    SourceCodeConceptMapping, confidence_field_names,
)
from omop_core.services import provisional_sweep
from omop_core.services.derivation_jobs import FakeDispatcher, use_dispatcher
from omop_core.services.source_vocabularies import canonical_source_vocabulary

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 25         # codes per Suggest call; a batch's writes land when it ends
_IN_CHUNK = 500            # codes per IN list
# Below this many results a batch of fallbacks proves nothing about the ranker.
_ABORT_MIN_BATCH = 3
_BANDS = ((60, '60-69%'), (50, '50-59%'))

# The note a code's last attempt left when the ranker could not answer.
_RANKING_FAILED = 'Ranking failed'


class FillError(Exception):
    """A step failed and left the person half-done; the command exits non-zero."""


@dataclass
class PersonCode:
    """One source code a person holds, under one clinical table."""
    table: str
    value: str                                   # a spelling as stored
    held: set = field(default_factory=set)       # the code systems the inventory holds it under
    hint: set = field(default_factory=set)       # the system a clinical row's source concept names
    rows: int = 0                                # unresolved clinical rows

    @property
    def key(self):
        return self.table, self.value.upper()


# ---- 1. the person's codes -------------------------------------------------------------

def person_codes(person, tables=None) -> list[PersonCode]:
    """The codes of *person* a proposal could help: unresolved or provisional rows, and inventory codes with no row.

    An inventory code the person already has a clinical row for is not asked
    about: its rows are resolved (or not) by the mapping the row's code already
    has. Source systems come from the inventory, which is what the resolver
    matches proposals against, and from a clinical row's source concept as a hint
    for a code the inventory does not hold.
    """
    quarantine = provisional_sweep._quarantine_concept_ids()
    found: dict[tuple, PersonCode] = {}
    present: dict[str, set[str]] = {}
    for table, (model, concept_col, source_col) in CLINICAL_TABLES.items():
        if tables and table not in tables:
            continue
        source_concept_col = source_col.replace('_source_value', '_source_concept_id')
        base = (model.objects.filter(person=person)
                .exclude(**{f'{source_col}__isnull': True}).exclude(**{source_col: ''}))
        present[table] = {v.upper() for v in base.values_list(source_col, flat=True).distinct()}
        vocabulary = Case(
            When(Q(**{f'{source_concept_col}__isnull': True})
                 | Q(**{source_concept_col: NO_MATCHING_CONCEPT_ID}), then=Value('')),
            default=F(f'{source_concept_col}__vocabulary_id'), output_field=CharField())
        unresolved = (base.filter(provisional_sweep._unresolved_q(concept_col, quarantine))
                      .annotate(vocab=vocabulary).values(source_col, 'vocab')
                      .annotate(n=Count('pk')))
        for row in unresolved:
            code = found.setdefault((table, row[source_col].upper()),
                                    PersonCode(table, row[source_col]))
            code.rows += row['n']
            if row['vocab']:
                code.hint.add(row['vocab'])
        # A code already provisional stays in the picture: the report counts it,
        # and a rerun that finds its proposal moved takes its rows back.
        for value in base.filter(confidence__isnull=False).values_list(source_col, flat=True).distinct():
            found.setdefault((table, value.upper()), PersonCode(table, value))
    for omop_table, value, vocabulary in (PatientSourceCode.objects.filter(person=person)
                                          .values_list('omop_table', 'source_value',
                                                       'source_vocabulary_id').distinct()):
        table = normalize_omop_table(omop_table)
        if table not in CLINICAL_TABLES or not value or (tables and table not in tables):
            continue
        key = (table, value.upper())
        if key not in found and key[1] in present.get(table, ()):
            continue
        found.setdefault(key, PersonCode(table, value)).held.add(vocabulary or '')
    return sorted(found.values(), key=lambda c: (c.table, c.value.upper()))


# ---- 2. their queue rows ---------------------------------------------------------------

def _chunks(values, size=_IN_CHUNK):
    values = sorted(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def queue_rows(codes) -> dict[tuple, list]:
    """``{(table, UPPER code): [unscoped SourceCodeConceptMapping]}``, a handful of queries.

    Unscoped because the per-patient inventory cannot say which hospital a code
    came from, which is the resolver's own reason for ignoring scoped proposals.
    """
    by_key: dict[tuple, list] = defaultdict(list)
    by_table: dict[str, set[str]] = defaultdict(set)
    for code in codes:
        by_table[code.table].add(code.value.upper())
    for table, values in by_table.items():
        for chunk in _chunks(values):
            rows = (SourceCodeConceptMapping.objects
                    .filter(organization__isnull=True, omop_table=table)
                    .annotate(upper_code=Upper('source_code')).filter(upper_code__in=chunk)
                    .select_related('target_concept'))
            for row in rows:
                by_key[(table, row.upper_code)].append(row)
    return by_key


def _vocabularies(code: PersonCode):
    return {canonical_source_vocabulary(v) for v in (code.held or code.hint)}


def ensure_queue_rows(codes, rows_by_key, *, create=True):
    """Create the queue rows a person's codes lack; returns ``(created, missing)``.

    One row for each code system the inventory holds the code under (or, for a
    code the inventory does not hold, the system a clinical row's source concept
    names), because the resolver needs every system the person holds the code
    under to be covered by a proposal. The row is born the way
    ``enqueue_unmapped_source_codes`` makes it: proposed, no destination,
    provenance ``''``, which is the state Suggest looks for. With ``create``
    false nothing is written and *missing* counts what would be created.
    """
    now = timezone.now()
    created = missing = 0
    for code in codes:
        existing = rows_by_key.get(code.key, [])
        if any(row.status == 'approved' for row in existing):
            continue
        wanted = {canonical_source_vocabulary(v): v for v in (code.held or code.hint)} or {'': ''}
        have = {canonical_source_vocabulary(row.source_vocabulary_id) for row in existing}
        for canonical, raw in sorted(wanted.items()):
            if canonical in have or (not code.held and existing):
                continue
            missing += 1
            if not create:
                continue
            try:
                with transaction.atomic():
                    row = SourceCodeConceptMapping.objects.create(
                        source_vocabulary_id=raw, source_code=code.value[:SOURCE_CODE_MAX],
                        source_code_description='', domain_id=_DOMAIN_FOR_TABLE.get(code.table, ''),
                        omop_table=code.table, status='proposed', origin='import',
                        origin_system='', occurrence_count=max(code.rows, 1),
                        first_seen=now, last_seen=now)
            except IntegrityError:
                # The same row exists under another spelling or table, or another
                # process just made it. Either way there is a row to use.
                continue
            rows_by_key.setdefault(code.key, []).append(row)
            created += 1
    return created, missing


def target_rows(codes, rows_by_key):
    """The queue rows whose proposals decide *codes*: for each code, those under a system it is held in.

    A code held under no system the queue has a row for falls back to all its
    rows. Codes with an approved mapping are left out: the resolver settles
    those, and Suggest never touches an approved row.
    """
    out = {}
    for code in codes:
        rows = rows_by_key.get(code.key, [])
        if not rows or any(row.status == 'approved' for row in rows):
            continue
        vocabularies = _vocabularies(code)
        chosen = [row for row in rows
                  if canonical_source_vocabulary(row.source_vocabulary_id) in vocabularies]
        for row in chosen or rows:
            out[row.pk] = row
    return out


# ---- 3. propose ------------------------------------------------------------------------

def attempt_stamp(ranker: str) -> str:
    return f'{suggestions.SUGGESTION_MODEL_VERSION}-{ranker}'


def candidate_ids(rows, *, ranker, replace=False, retry=False):
    """The row ids Suggest may write to, in the run's own order.

    The tab rule (:func:`~omop_core.mapping.suggestions.suggestable_queryset`:
    proposed or rejected, no destination; with *replace* also a destination only
    a previous Suggest run put there), minus rows this model version already
    attempted unless *retry*, which is what makes a second run resume.
    """
    queryset = suggestions.suggestable_queryset(
        None, min_occurrences=0, resuggest=replace, ranking_model=ranker,
        mapping_ids=[row.pk for row in rows])
    if not retry:
        queryset = queryset.exclude(last_suggest_attempt=attempt_stamp(ranker))
    return list(queryset.values_list('pk', flat=True))


def preflight(ranker: str):
    """Ask the ranker one question before spending a run on it.

    An unavailable ranker (a rejected key, no key) does not fail a Suggest run:
    it hands back the first retrieval hit as a stand-in, which is written as a
    destination with no score. One call here catches that before the first batch.
    """
    candidate = {
        'concept_id': 3000963, 'concept_name': 'Hemoglobin [Mass/volume] in Blood',
        'concept_code': '718-7', 'vocabulary_id': 'LOINC', 'concept_class_id': 'Lab Test',
        'domain_id': 'Measurement', 'standard_concept': 'S', 'lexical_score': 0.9,
        'retrieval': suggestions.STRATEGY_LEXICAL,
    }
    chosen, note, *_ = suggestions.rank_candidates_dispatch(
        'HGB', [candidate], source_description='Hemoglobin, whole blood',
        require_model_selection=False, ranking_model=ranker)
    if not suggestions._ranker_answered(chosen, note):
        raise FillError(f'The {ranker} ranker did not answer a test question, so nothing was '
                        f'proposed: {(note or "no detail")[:300]}')


def propose(ids, *, ranker, replace=False, strategies=None, batch_size=DEFAULT_BATCH, log=print):
    """Run Suggest over *ids* in batches, so each batch's writes land before the next starts.

    ``suggest_mappings`` retrieves every code, ranks them, then writes, so one
    call over hundreds of codes would write nothing until the end and lose it all
    if the job died. A batch is also the unit a rerun resumes after. A code the
    ranker failed on is counted and left for a rerun; a batch in which every code
    fell back to the stand-in stops the run.
    """
    counts = Counter()
    total = len(ids)
    for start in range(0, total, batch_size):
        batch = ids[start:start + batch_size]
        results = suggestions.suggest_mappings(
            None, min_occurrences=0, resuggest=replace, strategies=strategies,
            ranking_model=ranker, mapping_ids=batch)
        unanswered = 0
        for entry in results:
            counts['considered'] += 1
            note = entry.get('note') or ''
            suggested = entry.get('suggested')
            if suggestions._is_retrieval_fallback(suggested, note):
                counts['fallback'] += 1
                unanswered += 1
            elif note.startswith(_RANKING_FAILED):
                counts['errors'] += 1
                unanswered += 1
            elif entry.get('updated') and suggested:
                counts['proposed'] += 1
            elif entry.get('updated'):
                counts['declined'] += 1
            else:
                counts['skipped'] += 1
        log(f'  proposed {counts["considered"]}/{total} codes: {counts["proposed"]} destinations, '
            f'{counts["declined"]} declined, {counts["errors"]} errors, {counts["fallback"]} fallback')
        if len(results) >= _ABORT_MIN_BATCH and unanswered == len(results):
            raise FillError(
                f'The {ranker} ranker did not answer any of a batch of {len(results)} codes, so '
                f'the run stopped after {counts["considered"]} of {total}. Rows it could not rank '
                f'carry no score; fix the ranker and run again (add --replace to redo fallbacks).')
    return counts


# ---- 4. materialise and rederive -------------------------------------------------------

def materialise(person):
    """Resolve *person* against the current proposals, in the sweep's scope. Returns the resolver's counts.

    The derivation the resolver would queue is swallowed: :func:`rederive` runs
    it inline next, so the command's output is true when it ends.
    """
    index = provisional_sweep.qualifying_index(refresh=True)
    source_values, tables = provisional_sweep.person_scope(
        person.person_id, provisional_sweep.qualifying_codes(index))
    if not source_values:
        return {'resolved': 0, 'created': 0, 'provisional': 0, 'reverted': 0, 'skipped': 0,
                'already_resolved': 0, 'code_details': []}
    with use_dispatcher(FakeDispatcher()):
        return provisional_sweep.locked_resolve(person, source_values=source_values, omop_tables=tables)


def rederive(person):
    """Rebuild the person's record inline. Returns ``(provisional_fields, marks)``."""
    from omop_core.services.patient_record_service import refresh_patient_record
    from omop_core.services.provisional_record import NEVER_OVERLAID

    refresh_patient_record(person)
    marks = PatientRecordConfidence.objects.filter(person=person).first()
    fields = {}
    if marks is not None:
        for name in confidence_field_names():
            value = getattr(marks, name, None)
            if value is not None and name not in NEVER_OVERLAID:
                fields[name] = value
    return fields, PatientRecordProvisional.objects.filter(person=person).first()


# ---- 5. what the proposals say about the person's codes --------------------------------

def band(percent):
    """The below-floor band a confidence percentage falls in."""
    for low, label in _BANDS:
        if percent >= low:
            return label
    return 'below 50%'


def proposal_report(codes) -> dict:
    """How each of the person's codes stands against its proposals, counted.

    ``provisional`` codes have a qualifying proposal the resolver uses;
    ``below_floor`` have a destination under the floor (counted by band, the
    best proposal for the code); ``unscored`` have one with no confidence;
    ``conflict`` have qualifying proposals that disagree or leave a held system
    uncovered; ``no_destination`` have none; ``approved`` have an approved mapping.
    """
    floor = settings.PROVISIONAL_CONFIDENCE_FLOOR
    rows_by_key = queue_rows(codes)
    status, bands = Counter(), Counter()
    for code in codes:
        rows = rows_by_key.get(code.key, [])
        if any(row.status == 'approved' for row in rows):
            status['approved'] += 1
            continue
        proposals = [r for r in rows if r.status == 'proposed' and r.target_concept_id]
        if not proposals:
            status['no_destination'] += 1
            continue
        qualifying = [
            (r.target_concept_id, _confidence_percent(r.suggestion_confidence),
             canonical_source_vocabulary(r.source_vocabulary_id))
            for r in proposals
            if r.suggestion_confidence is not None
            and _confidence_percent(r.suggestion_confidence) >= floor
            and _usable_destination(r.target_concept, code.table)]
        if qualifying:
            chosen = choose_proposal(qualifying, code.held or None)
            status['provisional' if chosen else 'conflict'] += 1
            continue
        scored = [_confidence_percent(r.suggestion_confidence) for r in proposals
                  if r.suggestion_confidence is not None]
        if not scored:
            status['unscored'] += 1
            continue
        status['below_floor'] += 1
        bands[band(max(scored))] += 1
    return {'status': dict(status), 'bands': dict(bands), 'floor': floor}


# ---- the run ---------------------------------------------------------------------------

def run(person_id, *, ranker='jev', limit=None, tables=None, dry_run=False, skip_propose=False,
        replace=False, retry=False, strategies=None, batch_size=DEFAULT_BATCH,
        show_values=False, log=print) -> dict:
    """Propose, materialise and rederive for one person. Returns a summary dict.

    Raises :class:`FillError` when the switch is off (nothing written) or a step
    fails and leaves the person half-done.
    """
    if not settings.PROVISIONAL_DATA_ENABLED:
        raise FillError('PROVISIONAL_DATA_ENABLED is off, so nothing was computed or written. '
                        'Turn it on for this deployment first.')
    if ranker not in suggestions.RANKING_MODELS:
        raise FillError(f'Unknown ranker {ranker!r}; choose from {sorted(suggestions.RANKING_MODELS)}.')
    person = Person.objects.filter(person_id=person_id).first()
    if person is None:
        raise FillError(f'No person {person_id}.')
    summary: dict = {'person_id': person_id, 'dry_run': dry_run}

    codes = person_codes(person, tables)
    summary['codes_considered'] = len(codes)
    log(f'Person {person_id}: {len(codes)} code(s) to consider '
        f'({sum(1 for c in codes if c.rows)} with unresolved rows, '
        f'{sum(1 for c in codes if not c.rows)} inventory only).')

    rows_by_key = queue_rows(codes)
    created, would_create = ensure_queue_rows(codes, rows_by_key, create=not (dry_run or skip_propose))
    summary['queue_rows_created'] = created
    if dry_run:
        summary['queue_rows_missing'] = would_create
    rows = target_rows(codes, rows_by_key)
    ids = candidate_ids(rows.values(), ranker=ranker, replace=replace, retry=retry) if rows else []
    if limit:
        ids = ids[:limit]
    summary['proposable_rows'] = len(ids)
    log(f'{len(rows)} queue row(s) decide these codes; {len(ids)} to propose for'
        + (f' ({would_create} more would be created)' if dry_run and would_create else '')
        + (f', {created} created' if created else '') + '.')

    if dry_run:
        summary['report'] = proposal_report(codes)
        log(f'Dry run: nothing written. Standing now: {summary["report"]["status"]}')
        return summary

    if not skip_propose and ids:
        preflight(ranker)
        log(f'Proposing with {ranker} in batches of {batch_size}...')
        summary['propose'] = dict(propose(
            ids, ranker=ranker, replace=replace, strategies=strategies,
            batch_size=batch_size, log=log))
    else:
        summary['propose'] = {}

    log('Making rows provisional...')
    try:
        resolved = materialise(person)
    except Exception as exc:  # noqa: BLE001 -- reported as the step that left the person half-done
        raise FillError(f'Making the rows provisional failed: {exc}') from exc
    summary['rows_provisional'] = resolved['provisional']
    summary['rows_created'] = resolved['created']
    summary['rows_reverted'] = resolved['reverted']
    log(f'  {resolved["provisional"]} row(s) provisional (of which {resolved["created"]} created '
        f'from inventory), {resolved["reverted"]} taken back.')

    summary['report'] = proposal_report(codes)
    log(f'Proposals against the floor ({summary["report"]["floor"]}%): '
        f'{summary["report"]["status"]}; below the floor by band: {summary["report"]["bands"]}')

    log('Rederiving the record...')
    try:
        fields, provisional = rederive(person)
    except Exception as exc:  # noqa: BLE001
        raise FillError(f'Rederiving the record failed (the rows are already provisional): {exc}') from exc
    summary['provisional_fields'] = len(fields)
    log(f'  {len(fields)} provisional field(s) on the record.')
    for name, percent in sorted(fields.items(), key=lambda item: -item[1])[:12]:
        value = f' = {str(getattr(provisional, name, ""))[:40]}' if show_values and provisional else ''
        log(f'    {name}: {percent}%{value}')
    return summary
