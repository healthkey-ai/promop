"""Give one person a full provisional view: propose, materialise, rederive (#2696).

``manage.py fill_provisional_record <person_id>`` takes a patient through the
whole provisional chain in one resumable command, for the patient whose page
should show as much as the proposals can say while the codes are still being
mapped:

1. **Propose.** The person's codes with unresolved clinical rows are found (the
   inventory-only codes too with ``include_inventory``: they are slow, about four
   seconds each, and only matter for creating rows from the inventory), their
   organization-unscoped queue rows are located (or created, the way
   ``enqueue_unmapped_source_codes`` does), and the normal Suggest pipeline runs
   on those that are ``proposed`` with no destination, with the ranker the
   caller chose (Jev by default). Only rows under a code system the person holds
   the code under are proposed for. Destinations, confidences, provenance and
   notes are written exactly as a Suggest run writes them; the rows stay
   ``proposed``. A code the ranker could not rank is never written (not even as
   the first retrieval hit) and not stamped as attempted, so a rerun retries it.
2. **Materialise.** Every one of the person's rows is resolved against *all* the
   mappings there are, with the full per-person resolve (a single patient can
   afford the walk): an approved mapping makes its rows real (and creates rows for
   inventory codes that have none), a proposal at or above
   ``PROVISIONAL_CONFIDENCE_FLOOR`` makes them provisional, and a proposal below
   it, or unscored, leaves them alone. The floor is a site setting and is never
   overridden here: a later sweep would take back anything below it. What fell
   below is reported, by band, and the rows are counted as real, provisional or
   left.
3. **Rederive.** The person's ``PatientRecord`` is rebuilt inline: ``patient_record``
   from the approved rows only, and ``patient_record_provisional`` and
   ``patient_record_confidence`` from the provisional ones. Both are reported.

Nothing here reimplements Suggest, the resolver or the derivation; it chooses
which rows they run on. With ``PROVISIONAL_DATA_ENABLED`` off nothing is written.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.conf import settings
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models import Max
from django.db.models import Case, CharField, Count, F, Q, Value, When
from django.db.models.functions import Upper
from django.utils import timezone

from omop_core.mapping import suggestions
from omop_core.mapping.code_resolution import (
    _DOMAIN_FOR_TABLE, CLINICAL_TABLES, NO_MATCHING_CONCEPT_ID, SOURCE_CODE_MAX,
    _confidence_percent, _usable_destination, choose_proposal, normalize_omop_table,
)
from omop_core.models import (
    Concept, PatientRecordConfidence, PatientRecordProvisional, PatientSourceCode, Person,
    SourceCodeConceptMapping, UmlsSourceCode, confidence_field_names,
)
from omop_core.services import provisional_sweep
from omop_core.services.derivation_jobs import FakeDispatcher, use_dispatcher
from omop_core.services.source_vocabularies import canonical_source_vocabulary

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 25         # codes per Suggest call; a batch's writes land when it ends
_IN_CHUNK = 500            # codes per IN list
# The note a code carries when its ranking raised (anything else unanswered is the
# ranker's retrieval-order stand-in).
_RANKING_FAILED = 'Ranking failed'
# Derived bookkeeping on the record, not a value a rederive can be said to have changed.
_RECORD_NOISE = frozenset({'id', 'derivation_version'})


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
    metadata: dict = field(default_factory=dict)  # code system -> the inventory's source metadata

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
    for omop_table, value, vocabulary, metadata in PatientSourceCode.objects.filter(
            person=person).values_list('omop_table', 'source_value', 'source_vocabulary_id',
                                       'source_metadata'):
        table = normalize_omop_table(omop_table)
        if table not in CLINICAL_TABLES or not value or (tables and table not in tables):
            continue
        key = (table, value.upper())
        if key not in found and key[1] in present.get(table, ()):
            continue
        code = found.setdefault(key, PersonCode(table, value))
        code.held.add(vocabulary or '')
        if metadata:
            code.metadata.setdefault(vocabulary or '', metadata)
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


def ensure_queue_rows(codes, rows_by_key, *, create=True) -> dict:
    """Create the queue rows a person's codes lack; returns what was (or would be) done.

    ``{'created', 'missing', 'collisions', 'hk_skipped'}``. One row for each code
    system the inventory holds the code under (or, for a code the inventory does
    not hold, the system a clinical row's source concept names), because the
    resolver needs every system the person holds the code under to be covered by
    a proposal. The row is born the way ``enqueue_unmapped_source_codes`` makes it
    (proposed, no destination, provenance ``''``: the state Suggest looks for),
    with the Seen count of one (the global rebuild corrects it, and this person's
    row count is not the code's), the inventory's source metadata, and a name
    from Athena or UMLS where there is one, so the ranker sees what a curator
    sees. Left out, and counted: an HK-* source system (a minting vocabulary,
    never a source; ``clean()`` rejects it and ``create`` would not), and a row
    the unique constraint would refuse (organization, source system and code, case
    sensitive, whatever the table). With ``create`` false nothing is written and
    *missing* counts what would be created.
    """
    wanted_rows, hk_skipped = [], 0
    for code in codes:
        existing = rows_by_key.get(code.key, [])
        if any(row.status == 'approved' for row in existing):
            continue
        wanted = {canonical_source_vocabulary(v): v for v in (code.held or code.hint)} or {'': ''}
        have = {canonical_source_vocabulary(row.source_vocabulary_id) for row in existing}
        for canonical, raw in sorted(wanted.items()):
            if canonical in have or (not code.held and existing):
                continue
            if raw.startswith('HK-') or canonical.startswith('HK-'):
                hk_skipped += 1
                continue
            wanted_rows.append((code, raw))
    taken = set()
    for chunk in _chunks({code.value[:SOURCE_CODE_MAX] for code, _raw in wanted_rows}):
        taken.update(SourceCodeConceptMapping.objects.filter(
            organization__isnull=True, source_code__in=chunk,
        ).values_list('source_vocabulary_id', 'source_code'))
    to_create = [(code, raw) for code, raw in wanted_rows
                 if (raw, code.value[:SOURCE_CODE_MAX]) not in taken]
    result = {'created': 0, 'missing': len(to_create),
              'collisions': len(wanted_rows) - len(to_create), 'hk_skipped': hk_skipped}
    if not create or not to_create:
        return result
    now = timezone.now()
    id_before = SourceCodeConceptMapping.objects.aggregate(Max('id'))['id__max'] or 0
    for code, raw in to_create:
        metadata = code.metadata.get(raw) or next(iter(code.metadata.values()), None)
        try:
            with transaction.atomic():
                row = SourceCodeConceptMapping.objects.create(
                    source_vocabulary_id=raw, source_code=code.value[:SOURCE_CODE_MAX],
                    source_code_description='', domain_id=_DOMAIN_FOR_TABLE.get(code.table, ''),
                    omop_table=code.table, status='proposed', origin='import',
                    origin_system='', occurrence_count=1, first_seen=now, last_seen=now,
                    **({'source_metadata': metadata} if metadata else {}))
        except IntegrityError:
            # Another process made it between the check above and now.
            result['collisions'] += 1
            continue
        rows_by_key.setdefault(code.key, []).append(row)
        result['created'] += 1
    if result['created']:
        from omop_core.services.source_descriptions import backfill_source_descriptions
        backfill_source_descriptions(SourceCodeConceptMapping, Concept, UmlsSourceCode, min_id=id_before)
    result['missing'] = result['created']
    return result


def target_rows(codes, rows_by_key):
    """The queue rows whose proposals decide *codes*: for each code, those under a system it is held in.

    Never a row under a code system the person does not hold the code under: a
    short Epic code repeats across hospitals' systems, and a proposal for another
    hospital's "6" says nothing about this patient's (on HealthTree 58,588 rows
    match one patient's codes by string and 997 are in a system she holds). A
    system with no row yet is not a reason to fall back to another's: the row is
    created first. Codes with an approved mapping are left out: the resolver
    settles those, and Suggest never touches an approved row.
    """
    out = {}
    for code in codes:
        rows = rows_by_key.get(code.key, [])
        if not rows or any(row.status == 'approved' for row in rows):
            continue
        vocabularies = _vocabularies(code) or {canonical_source_vocabulary('')}
        for row in rows:
            if canonical_source_vocabulary(row.source_vocabulary_id) in vocabularies:
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
        raise FillError(f'The {ranker} ranker did not answer a test question (no key, a rejected '
                        f'key, or the service is unavailable), so nothing was proposed.')


def propose(ids, *, ranker, counts, replace=False, strategies=None, batch_size=DEFAULT_BATCH,
            log=print):
    """Run Suggest over *ids* in batches, so each batch's writes land before the next starts.

    ``suggest_mappings`` retrieves every code, ranks them, then writes, so one
    call over hundreds of codes would write nothing until the end and lose it all
    if the job died. A batch is also the unit a rerun resumes after.

    ``skip_fallback`` keeps this command from ever writing what is not an answer:
    a code the ranker could not rank (an error, a 429, a timeout, no key) is left
    unwritten and unstamped, so a rerun retries it. The first batch that has one
    stops the run, after that batch's real answers are written, because an
    unavailable ranker rarely recovers within the run. *counts* is filled in as it
    goes, so a caller that loses the run still knows how far it got.
    """
    total = len(ids)
    for start in range(0, total, batch_size):
        batch = ids[start:start + batch_size]
        if not connection.in_atomic_block:
            close_old_connections()
        results = suggestions.suggest_mappings(
            None, min_occurrences=0, resuggest=replace, strategies=strategies,
            ranking_model=ranker, mapping_ids=batch, skip_fallback=True)
        unanswered = 0
        for entry in results:
            counts['considered'] += 1
            suggested = entry.get('suggested')
            if entry.get('unanswered'):
                unanswered += 1
                counts['errors' if (entry.get('note') or '').startswith(_RANKING_FAILED)
                       else 'fallback'] += 1
            elif entry.get('updated') and suggested:
                counts['proposed'] += 1
            elif entry.get('updated'):
                counts['declined'] += 1
            else:
                counts['skipped'] += 1
        log(f'  proposed {counts["considered"]}/{total} codes: {counts["proposed"]} destinations, '
            f'{counts["declined"]} declined, {counts["errors"]} errors, '
            f'{counts["fallback"]} unavailable')
        if unanswered:
            raise FillError(
                f'The {ranker} ranker did not answer {unanswered} of {len(results)} codes in a batch '
                f'({counts["errors"]} errors, {counts["fallback"]} unavailable), so the run stopped '
                f'after {counts["considered"]} of {total} codes. That batch\'s answered codes and '
                f'every earlier batch are written; the unanswered ones were left untouched. Run the '
                f'command again to retry them when the ranker is available.')


# ---- 4. materialise and rederive -------------------------------------------------------

def materialise(person):
    """Resolve *person* against every mapping there is. Returns the resolver's counts.

    The full per-person resolve, not the sweep's narrowed scope: this is one
    patient, so the walk over every code is affordable (minutes on a 24,000-row
    chart) and it does everything a mapping can do for the rows. An approved
    mapping makes its rows real and creates the rows an inventory code lacks;
    a proposal at or above the floor makes rows provisional; one below the floor
    (or unscored) leaves them as they are; a provisional row whose proposal no
    longer qualifies goes back to unresolved. The derivation the resolver would
    queue is swallowed: :func:`rederive` runs it inline next, so the command's
    output is true when it ends.
    """
    with use_dispatcher(FakeDispatcher()):
        return provisional_sweep.locked_resolve(person)


def _record_values(person) -> dict:
    """The derived fields of the person's ``PatientRecord``, to tell what a rederive changed."""
    from omop_core.models import PatientRecord

    record = PatientRecord.objects.filter(person=person).first()
    if record is None:
        return {}
    return {f.name: getattr(record, f.attname) for f in record._meta.concrete_fields
            if f.name not in _RECORD_NOISE and not f.name.endswith('_at')}


def rederive(person):
    """Rebuild the person's record inline.

    Returns ``{'provisional_fields': {name: percent}, 'provisional': the provisional
    row or None, 'changed': [names]}``: the fields the provisional overlay holds
    (with the confidence), and the fields of the approved-only ``patient_record``
    that differ from before the rederive. Field names only, no values.
    """
    from omop_core.services.patient_record_service import refresh_patient_record
    from omop_core.services.provisional_record import NEVER_OVERLAID

    before = _record_values(person)
    refresh_patient_record(person)
    after = _record_values(person)
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    marks = PatientRecordConfidence.objects.filter(person=person).first()
    fields = {}
    if marks is not None:
        for name in confidence_field_names():
            value = getattr(marks, name, None)
            if value is not None and name not in NEVER_OVERLAID:
                fields[name] = value
    return {'provisional_fields': fields, 'changed': changed,
            'provisional': PatientRecordProvisional.objects.filter(person=person).first()}


# ---- 5. what the proposals say about the person's codes --------------------------------

def band(percent, floor):
    """The band, below *floor*, a confidence percentage falls in (two of ten points, then the rest)."""
    high = floor
    for _ in range(2):
        low = max(high - 10, 0)
        if percent >= low:
            return f'{low}-{high - 1}%'
        high = low
    return f'below {high}%'


def proposal_report(codes) -> dict:
    """How each of the person's codes stands against its proposals, counted.

    ``provisional``: a qualifying proposal the resolver uses. ``below_floor``: a
    destination under the floor (counted by band, from the best proposal for the
    code). ``destination_not_usable``: a proposal at or above the floor whose
    destination the resolver will not write (not standard, not valid, or not in
    the table's domain), which is a curation problem and not a confidence one.
    ``unscored``: a destination with no confidence. ``conflict``: qualifying
    proposals that disagree, or leave a held system uncovered. ``no_destination``:
    none. ``approved``: an approved mapping.
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
        scored = [(r, _confidence_percent(r.suggestion_confidence))
                  for r in proposals if r.suggestion_confidence is not None]
        qualifying = [
            (r.target_concept_id, percent, canonical_source_vocabulary(r.source_vocabulary_id))
            for r, percent in scored
            if percent >= floor and _usable_destination(r.target_concept, code.table)]
        if qualifying:
            status['provisional' if choose_proposal(qualifying, code.held or None) else 'conflict'] += 1
        elif any(percent >= floor for _r, percent in scored):
            status['destination_not_usable'] += 1
        elif scored:
            status['below_floor'] += 1
            bands[band(max(percent for _r, percent in scored), floor)] += 1
        else:
            status['unscored'] += 1
    return {'status': dict(status), 'bands': dict(bands), 'floor': floor}


# ---- the run ---------------------------------------------------------------------------

def run(person_id, *, ranker='jev', limit=None, tables=None, dry_run=False, skip_propose=False,
        replace=False, retry=False, strategies=None, batch_size=DEFAULT_BATCH,
        show_values=False, include_inventory=False, log=print) -> dict:
    """Propose, materialise and rederive for one person. Returns a summary dict.

    Proposing is for the codes the person has unresolved rows under; with
    *include_inventory* the inventory-only codes (no clinical row yet) too, which
    is slow and only matters for creating rows from the inventory. ``tables`` and
    ``limit`` narrow only the propose step: the person's rows are resolved against
    every mapping there is, approved and proposed, and their record rederived.
    Raises :class:`FillError` when the switch is off (nothing written) or a step
    fails and leaves the person half-done. Messages carry the step and the
    exception's class, never its text, which can quote key values.
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
    unresolved = [c for c in codes if c.rows]
    log(f'Person {person_id}: {len(codes)} code(s) to consider '
        f'({len(unresolved)} with unresolved rows, {len(codes) - len(unresolved)} inventory only).')

    propose_codes = codes if include_inventory else unresolved
    summary['codes_to_propose'] = len(propose_codes)
    rows_by_key = queue_rows(propose_codes)
    queue = ensure_queue_rows(propose_codes, rows_by_key, create=not (dry_run or skip_propose))
    summary['queue_rows'] = queue
    rows = target_rows(propose_codes, rows_by_key)
    ids = candidate_ids(rows.values(), ranker=ranker, replace=replace, retry=retry) if rows else []
    if dry_run:
        # A row the real run would create is proposed for like any other: count it.
        ids = ids + [None] * queue['missing']
    if limit:
        ids = ids[:limit]
    summary['proposable_rows'] = len(ids)
    log(f'{len(propose_codes)} code(s) to propose for '
        f'({"unresolved rows and inventory" if include_inventory else "unresolved rows only"}); '
        f'{len(rows)} queue row(s) decide them; {len(ids)} to propose for. Queue rows: '
        f'{queue["created"]} created, {queue["missing"] if dry_run or skip_propose else 0} would be '
        f'created, {queue["collisions"]} skipped (already exist), {queue["hk_skipped"]} skipped '
        f'(HK-* source system).')

    if dry_run:
        summary['report'] = proposal_report(codes)
        log(f'Dry run: nothing written. Standing now: {summary["report"]["status"]}')
        return summary

    counts = Counter()
    summary['propose'] = counts
    if not skip_propose and ids:
        preflight(ranker)
        log(f'Proposing with {ranker} in batches of {batch_size}...')
        try:
            propose(ids, ranker=ranker, counts=counts, replace=replace, strategies=strategies,
                    batch_size=batch_size, log=log)
        except FillError:
            raise
        except Exception as exc:  # noqa: BLE001 -- reported as the step that stopped, class only
            raise FillError(
                f'Proposing stopped after {counts["considered"]} codes on an error '
                f'({type(exc).__name__}). Every batch before it is written, so run the command '
                f'again to resume.') from exc
    summary['propose'] = dict(counts)

    log('Resolving every row against the approved and proposed mappings...')
    try:
        resolved = materialise(person)
    except Exception as exc:  # noqa: BLE001
        raise FillError(f'Resolving the rows failed ({type(exc).__name__}). The proposals '
                        f'are written; run the command again.') from exc
    real_created = sum(d.get('rows', 0) for d in resolved.get('code_details', ())
                       if d.get('outcome') == 'created')
    summary.update(
        rows_real=resolved['resolved'] + real_created, rows_real_created=real_created,
        rows_provisional=resolved['provisional'], rows_created=resolved['created'],
        rows_reverted=resolved['reverted'], rows_left=resolved['skipped'],
        rows_already_resolved=resolved['already_resolved'])
    log(f'  {summary["rows_real"]} row(s) made real by an approved mapping '
        f'({real_created} of them created from inventory); '
        f'{resolved["provisional"]} made provisional by a proposal at or above the floor '
        f'({resolved["created"] - real_created} of them created from inventory); '
        f'{resolved["reverted"]} provisional row(s) taken back; '
        f'{resolved["skipped"]} left (no approved mapping and no qualifying proposal); '
        f'{resolved["already_resolved"]} already resolved.')

    summary['report'] = proposal_report(codes)
    log(f'Proposals against the floor ({summary["report"]["floor"]}%): '
        f'{summary["report"]["status"]}; below the floor by band: {summary["report"]["bands"]}')

    log('Rederiving the record...')
    try:
        derived = rederive(person)
    except Exception as exc:  # noqa: BLE001
        raise FillError(f'Rederiving the record failed ({type(exc).__name__}); the rows are already '
                        f'resolved. Run the command again with --skip-propose.') from exc
    fields, provisional, changed = derived['provisional_fields'], derived['provisional'], derived['changed']
    summary['record_fields_changed'] = len(changed)
    summary['record_fields_changed_names'] = changed
    summary['provisional_fields'] = len(fields)
    log(f'  patient_record (approved rows only): {len(changed)} field(s) changed'
        + (f': {", ".join(changed[:25])}{" ..." if len(changed) > 25 else ""}' if changed else '') + '.')
    log(f'  patient_record_provisional: {len(fields)} provisional field(s) on the record.')
    for name, percent in sorted(fields.items(), key=lambda item: -item[1])[:12]:
        value = f' = {str(getattr(provisional, name, ""))[:40]}' if show_values and provisional else ''
        log(f'    {name}: {percent}%{value}')
    return summary
