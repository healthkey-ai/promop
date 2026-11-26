"""Reviewed answer-to-concept mappings for field choices (#1224).

An allowed value (``FieldChoice``) can be bound to the standard concept it
means. The binding is a decision with an owner and a history, not a column:

* a curator **proposes** one (``propose``), with a target concept or the
  reviewed statement that the answer has **no standard equivalent**;
* an approver **approves** or **rejects** it (``approve``, ``reject``);
  approving replaces the decision in force, which is kept as ``superseded``;
* every transition is a ``FieldValueConceptMappingRevision``.

The rules the database cannot say are here: the target must be a current
standard concept of a vocabulary that is not withdrawn, in a domain that fits
the role (a ``main_concept`` answer lands in the table its field is mapped to),
and is looked up on the server, never trusted from a client's concept id alone.
A choice without a mapping stays valid and saveable; this module never blocks a
value. Nothing here writes patient data.
"""
from __future__ import annotations

from typing import Optional

from django.db import transaction
from django.utils import timezone

ROLES = ('value_as_concept', 'main_concept', 'context_recipe')
OUTCOMES = ('mapped', 'no_equivalent')

#: The OMOP domain a concept must have to be the *fact* a table row records.
TABLE_DOMAIN = {
    'condition_occurrence': 'Condition',
    'procedure_occurrence': 'Procedure',
    'drug_exposure': 'Drug',
    'measurement': 'Measurement',
    'observation': 'Observation',
}
#: Domains that are never an answer: they describe the record, not the finding.
NON_ANSWER_DOMAINS = frozenset({
    'Metadata', 'Type Concept', 'Relationship', 'Vocabulary', 'Domain', 'Concept Class',
    'Unit', 'Currency', 'Visit', 'Cost', 'Payer', 'Plan',
})


class ValueMappingError(ValueError):
    """A mapping the rules refuse; the text is for a curator."""


class ConflictError(ValueMappingError):
    """The request is valid but the mapping is not in a state that allows it."""


def _concept_model():
    from omop_core.models import Concept
    return Concept


def lookup_concept(*, concept_id=None, vocabulary_id: str = '', concept_code: str = ''):
    """The concept named by id, or by exact vocabulary and code; ``None`` when absent."""
    Concept = _concept_model()
    if concept_id not in (None, ''):
        try:
            return Concept.objects.select_related('domain').filter(pk=int(concept_id)).first()
        except (TypeError, ValueError):
            raise ValueMappingError(f'{concept_id!r} is not a concept id.') from None
    if vocabulary_id and concept_code:
        return (Concept.objects.select_related('domain')
                .filter(vocabulary_id=vocabulary_id, concept_code=concept_code)
                .order_by('concept_id').first())
    return None


def release_of(vocabulary_id: str) -> str:
    from omop_core.models import Vocabulary
    return (Vocabulary.objects.filter(pk=vocabulary_id)
            .values_list('vocabulary_version', flat=True).first() or '')


class CheckContext:
    """What ``target_problem`` reads besides the concept, loaded once and lazily.

    Checking every approved mapping of a list one by one would otherwise ask
    for the withdrawn vocabularies and the field's table once per mapping.
    """

    def __init__(self):
        self._deprecated = None
        self._tables = None

    @property
    def deprecated(self) -> set:
        if self._deprecated is None:
            from omop_core.models import Vocabulary
            self._deprecated = set(Vocabulary.objects.filter(is_deprecated=True)
                                   .values_list('vocabulary_id', flat=True))
        return self._deprecated

    @property
    def tables(self) -> dict:
        if self._tables is None:
            from omop_core.models import FieldConceptMapping
            self._tables = dict(FieldConceptMapping.objects.values_list('field_name', 'omop_table'))
        return self._tables


def target_problem(concept, role: str, field_name: str = '', *, question: bool = False,
                   ctx: 'CheckContext | None' = None) -> Optional[str]:
    """Why ``concept`` cannot be a mapping target, or ``None`` when it can.

    Used when a mapping is proposed or approved, and again by ``target_state``
    for one already approved, since a vocabulary reload can retire a concept
    after the fact.
    """
    ctx = ctx or CheckContext()
    what = 'question concept' if question else 'target concept'
    if concept is None:
        return f'The {what} is not loaded in this instance.'
    label = f'{concept.vocabulary_id}:{concept.concept_code}'
    if concept.invalid_reason:
        return f'{label} is retired ({concept.invalid_reason}); choose its current replacement.'
    if concept.standard_concept != 'S':
        return f'{label} is not a standard concept; map to the standard concept it maps to.'
    if concept.vocabulary_id in ctx.deprecated:
        return f'The {concept.vocabulary_id} vocabulary is withdrawn.'
    domain = concept.domain_id
    if domain in NON_ANSWER_DOMAINS:
        return f'{label} is in the {domain} domain, which is not a clinical answer.'
    if role == 'main_concept' and not question and field_name:
        wanted = TABLE_DOMAIN.get(ctx.tables.get(field_name) or '')
        table = ctx.tables.get(field_name)
        if wanted and domain != wanted:
            return (f'{label} is in the {domain} domain, but {field_name} is stored in '
                    f'{table}, which holds {wanted} concepts.')
    return None


def search_targets(choice, query: str, *, role: str = 'value_as_concept', question: bool = False,
                   vocabulary_id: str = '', ctx: 'CheckContext | None' = None):
    """The concepts ``choice`` may be proposed to, best match first (#1225).

    Returns ``(queryset, rules)``. The filter is ``target_problem`` expressed as
    a query, so the editor lists only what a proposal would accept; a concept
    that fails a rule is absent rather than shown and refused. A match is by
    name, exact vocabulary code or OMOP id, as in the curator's concept search.
    ``question=True`` searches for a recipe's question concept, which has no
    domain rule of its own.
    """
    from omop_core.api.concepts import concept_match_ordering, concept_text_search
    from django.db.models import Q

    if role not in ROLES:
        raise ValueMappingError(f'{role!r} is not a mapping role.')
    if question and role != 'context_recipe':
        raise ValueMappingError('Only a contextual recipe has a question concept.')
    ctx = ctx or CheckContext()
    text, concept_id = concept_text_search(query)
    queryset = _concept_model().objects.filter(text).filter(
        standard_concept='S').filter(Q(invalid_reason__isnull=True) | Q(invalid_reason=''))
    queryset = queryset.exclude(domain_id__in=NON_ANSWER_DOMAINS)
    if ctx.deprecated:
        queryset = queryset.exclude(vocabulary_id__in=ctx.deprecated)
    required_domain = None
    if role == 'main_concept' and not question:
        required_domain = TABLE_DOMAIN.get(ctx.tables.get(choice.field_name) or '')
        if required_domain:
            queryset = queryset.filter(domain_id=required_domain)
    if vocabulary_id:
        queryset = queryset.filter(vocabulary_id=vocabulary_id)
    rules = {
        'role': role, 'question': question, 'standard_only': True,
        'required_domain': required_domain,
        'excluded_domains': sorted(NON_ANSWER_DOMAINS),
        'excluded_vocabularies': sorted(ctx.deprecated),
    }
    return queryset.order_by(*concept_match_ordering(query, concept_id)), rules


def target_state(mapping, ctx: 'CheckContext | None' = None) -> str:
    """``ok``, or what is wrong now with the target of an approved mapping."""
    if mapping.outcome != 'mapped':
        return 'ok'
    if mapping.target_concept_id is None:
        return 'unresolved' if mapping.source_resolution != 'resolved' else 'missing'
    problem = target_problem(mapping.target_concept, mapping.role,
                             mapping.choice.field_name, ctx=ctx)
    if problem is None:
        return 'ok'
    if mapping.target_concept.invalid_reason:
        return 'retired'
    if mapping.target_concept.standard_concept != 'S':
        return 'non_standard'
    return 'invalid'


def _label(user) -> str:
    from omop_core.services.field_mapping_provenance import CURATOR_PROVENANCE, user_provenance
    return user_provenance(user) if getattr(user, 'pk', None) else CURATOR_PROVENANCE


def _record(mapping, action: str, user, label: str | None = None) -> None:
    """Append a revision for the mapping's current state and bump its counter."""
    from omop_core.models import FieldValueConceptMappingRevision

    last = (FieldValueConceptMappingRevision.objects.filter(mapping=mapping)
            .order_by('-revision').values_list('revision', flat=True).first() or 0)
    number = last + 1
    FieldValueConceptMappingRevision.objects.create(
        mapping=mapping, revision=number, action=action, status=mapping.status,
        outcome=mapping.outcome, role=mapping.role,
        concept_id=mapping.target_concept_id,
        vocabulary_id=mapping.vocabulary_id, concept_code=mapping.concept_code,
        question_concept_id=mapping.question_concept_id,
        rationale=mapping.rationale, vocabulary_release=mapping.vocabulary_release,
        changed_by=user if getattr(user, 'pk', None) else None,
        changed_by_label=label or _label(user),
    )
    type(mapping).objects.filter(pk=mapping.pk).update(revision=number)
    mapping.revision = number


def _lock(choice):
    """Serialise decisions about one choice: two approvals read-then-write the same rows."""
    from omop_core.models import FieldChoice
    return FieldChoice.objects.select_for_update().get(pk=choice.pk)


def _same_decision(mapping, outcome, target, question) -> bool:
    return (mapping.outcome == outcome
            and mapping.target_concept_id == (target.pk if target else None)
            and mapping.question_concept_id == (question.pk if question else None))


@transaction.atomic
def propose(choice, *, role: str = 'value_as_concept', outcome: str = 'mapped',
            concept_id=None, vocabulary_id: str = '', concept_code: str = '',
            question_concept_id=None, question_vocabulary_id: str = '',
            question_concept_code: str = '', rationale: str = '', user=None):
    """Propose what ``choice`` means in ``role``; returns the open proposal.

    A second proposal for the same choice and role replaces the first open one
    (kept in its revisions); the approved decision stays in force until a
    proposal is approved. Proposing what is already in force is refused.
    """
    from omop_core.models import FieldValueConceptMapping as Mapping

    if role not in ROLES:
        raise ValueMappingError(f'Unknown role {role!r}. Choose from: {", ".join(ROLES)}.')
    if outcome not in OUTCOMES:
        raise ValueMappingError(f'Unknown outcome {outcome!r}. Choose from: {", ".join(OUTCOMES)}.')
    choice = _lock(choice)

    target = question = None
    if outcome == 'no_equivalent':
        if concept_id not in (None, '') or concept_code:
            raise ValueMappingError('A reviewed "no standard equivalent" has no target concept.')
        if not str(rationale or '').strip():
            raise ValueMappingError('Say why the answer has no standard equivalent.')
        vocabulary_id = concept_code = ''
    else:
        target = lookup_concept(concept_id=concept_id, vocabulary_id=vocabulary_id,
                                concept_code=concept_code)
        if concept_id in (None, '') and not (vocabulary_id and concept_code):
            raise ValueMappingError('Name the target concept by id, or by vocabulary and code.')
        problem = target_problem(target, role, choice.field_name)
        if problem:
            raise ValueMappingError(problem)
        vocabulary_id, concept_code = target.vocabulary_id, target.concept_code
    if role == 'context_recipe':
        if outcome != 'mapped':
            raise ValueMappingError('An answer with its own question needs a target concept.')
        question = lookup_concept(concept_id=question_concept_id,
                                  vocabulary_id=question_vocabulary_id,
                                  concept_code=question_concept_code)
        if question_concept_id in (None, '') and not (question_vocabulary_id and question_concept_code):
            raise ValueMappingError('Name the question concept by id, or by vocabulary and code.')
        problem = target_problem(question, role, choice.field_name, question=True)
        if problem:
            raise ValueMappingError(problem)
    elif question_concept_id not in (None, '') or question_concept_code:
        raise ValueMappingError('Only an answer with its own question names a question concept.')

    in_force = Mapping.objects.filter(choice=choice, role=role, status='approved').first()
    if in_force is not None and _same_decision(in_force, outcome, target, question):
        raise ConflictError('That is already the approved mapping for this answer.')

    fields = dict(
        outcome=outcome, target_concept=target, vocabulary_id=vocabulary_id,
        concept_code=concept_code, question_concept=question,
        question_vocabulary_id=question.vocabulary_id if question else '',
        question_concept_code=question.concept_code if question else '',
        source_resolution='', rationale=str(rationale or '').strip(),
        vocabulary_release=release_of(target.vocabulary_id) if target else '',
        provenance=_label(user),
        proposed_by=user if getattr(user, 'pk', None) else None,
        reviewed_at=None, reviewer=None,
    )
    open_one = Mapping.objects.filter(choice=choice, role=role, status='proposed').first()
    if open_one is None:
        mapping = Mapping.objects.create(choice=choice, role=role, status='proposed', **fields)
        _record(mapping, 'proposed', user)
        return mapping
    for name, value in fields.items():
        setattr(open_one, name, value)
    open_one.save()
    _record(open_one, 'edited', user)
    return open_one


@transaction.atomic
def approve(mapping, *, user=None, rationale: str = ''):
    """Make a proposal the decision in force; the one it replaces is superseded."""
    from omop_core.models import FieldValueConceptMapping as Mapping

    mapping = Mapping.objects.select_related('choice', 'target_concept', 'question_concept').get(pk=mapping.pk)
    choice = _lock(mapping.choice)
    mapping.refresh_from_db()
    if mapping.status != 'proposed':
        raise ConflictError(f'Only a proposal can be approved; this one is {mapping.status}.')
    if mapping.outcome == 'mapped':
        if mapping.target_concept_id is None:
            why = {
                'invalid': 'is retired',
                'non_standard': 'is not a standard concept',
            }.get(mapping.source_resolution, 'has no concept in this instance')
            raise ValueMappingError(
                f'{mapping.vocabulary_id}:{mapping.concept_code} {why}; '
                'propose a current standard concept before approving.')
        problem = target_problem(mapping.target_concept, mapping.role, choice.field_name)
        if problem:
            raise ValueMappingError(problem)
        if mapping.role == 'context_recipe':
            problem = target_problem(mapping.question_concept, mapping.role, choice.field_name,
                                     question=True)
            if problem:
                raise ValueMappingError(problem)
    for old in Mapping.objects.filter(choice=choice, role=mapping.role, status='approved').exclude(pk=mapping.pk):
        old.status = 'superseded'
        old.save(update_fields=['status', 'updated_at'])
        _record(old, 'superseded', user)
    mapping.status = 'approved'
    mapping.reviewer = user if getattr(user, 'pk', None) else None
    mapping.reviewed_at = timezone.now()
    mapping.vocabulary_release = (release_of(mapping.target_concept.vocabulary_id)
                                  if mapping.target_concept_id else '')
    if rationale and str(rationale).strip():
        mapping.rationale = str(rationale).strip()
    mapping.save()
    _record(mapping, 'approved', user)
    return mapping


@transaction.atomic
def reject(mapping, *, user=None, rationale: str = ''):
    """Reject a proposal, or withdraw an approval; the answer stays saveable."""
    from omop_core.models import FieldValueConceptMapping as Mapping

    mapping = Mapping.objects.select_related('choice').get(pk=mapping.pk)
    _lock(mapping.choice)
    mapping.refresh_from_db()
    if mapping.status not in ('proposed', 'approved'):
        raise ConflictError(f'This mapping is already {mapping.status}.')
    mapping.status = 'rejected'
    mapping.reviewer = user if getattr(user, 'pk', None) else None
    mapping.reviewed_at = timezone.now()
    if rationale and str(rationale).strip():
        mapping.rationale = str(rationale).strip()
    mapping.save()
    _record(mapping, 'rejected', user)
    return mapping


def in_force(choice, role: str = 'value_as_concept'):
    """The approved mapping of ``choice`` in ``role``, or ``None``."""
    from omop_core.models import FieldValueConceptMapping as Mapping
    return (Mapping.objects.select_related('target_concept')
            .filter(choice=choice, role=role, status='approved').first())


# An explicit "writes no concept", as against ``None``, "nothing says".
_NO_CONCEPT = object()


def _answer_meanings(field_names):
    """What each choice of the given fields means as a concept (#1224).

    Returns ``(choices, meanings, aliases, NONE)``: the choice rows, a
    ``{choice pk: concept id | NONE}`` map that omits a choice nothing decides
    and no code covers, and the aliases per choice. Both readers of an answer's
    concept, the projection (answer to concept) and the derivation (concept to
    answer), take it from here, so they cannot disagree about a decision.
    """
    from omop_core.models import (
        FieldChoice, FieldChoiceAlias, FieldChoiceCode, FieldValueConceptMapping as Mapping,
        FieldValueConceptMappingRevision as Revision,
    )

    field_names = sorted(set(field_names))
    if not field_names:
        return [], {}, {}, _NO_CONCEPT
    choices = list(FieldChoice.objects.filter(field_name__in=field_names)
                   .values('pk', 'field_name', 'display', 'value', 'retired_at', 'context_key'))
    if not choices:
        return [], {}, {}, _NO_CONCEPT
    ids = [c['pk'] for c in choices]

    NONE = _NO_CONCEPT
    decided: dict[int, object] = {}   # approved: concept id or NONE
    blocked: set[int] = set()         # had a decision once, none in force now
    turned_down: dict[int, set] = {}  # concept ids a reviewer rejected, per choice
    rejected: dict[int, object] = {}
    ctx = CheckContext()
    rows = (Mapping.objects.filter(choice_id__in=ids, role='value_as_concept',
                                   status__in=('approved', 'rejected', 'superseded'))
            .select_related('choice', 'target_concept'))
    for row in rows:
        if row.status == 'approved':
            ok = row.outcome == 'mapped' and row.target_concept_id and target_state(row, ctx) == 'ok'
            decided[row.choice_id] = row.target_concept_id if ok else NONE
        elif row.status == 'superseded':
            blocked.add(row.choice_id)
        else:
            rejected[row.pk] = row
    if rejected:
        # A rejection of something once approved is a withdrawal: that choice
        # had a decision and has none now. Any other rejection only turns down
        # a proposal.
        withdrawn = set(Revision.objects.filter(mapping_id__in=rejected, action='approved')
                        .values_list('mapping_id', flat=True))
        for pk, row in rejected.items():
            if pk in withdrawn:
                blocked.add(row.choice_id)
            elif row.target_concept_id:
                turned_down.setdefault(row.choice_id, set()).add(row.target_concept_id)

    legacy_ids = [pk for pk in ids if pk not in decided and pk not in blocked]
    legacy: dict[int, int] = {}
    if legacy_ids:
        codes = list(FieldChoiceCode.objects.filter(choice_id__in=legacy_ids, is_primary=True)
                     .order_by('pk').values_list('choice_id', 'vocabulary_id', 'code'))
        by_vocab: dict[str, set] = {}
        for _, vocabulary_id, code in codes:
            by_vocab.setdefault(vocabulary_id, set()).add(code)
        found: dict[tuple, int] = {}
        for vocabulary_id, code_set in by_vocab.items():
            found.update({(vocabulary_id, code): pk for code, pk in
                          _concept_model().objects.filter(
                              vocabulary_id=vocabulary_id, concept_code__in=code_set,
                          ).values_list('concept_code', 'concept_id')})
        for choice_id, vocabulary_id, code in codes:
            concept_id = found.get((vocabulary_id, code))
            # A reviewer who turned down this very concept has answered for the
            # code; one who turned down a different proposal has not.
            if concept_id is not None and concept_id not in turned_down.get(choice_id, ()):
                legacy.setdefault(choice_id, concept_id)

    aliases: dict[int, list[str]] = {}
    for choice_id, alias in FieldChoiceAlias.objects.filter(choice_id__in=ids).values_list('choice_id', 'alias'):
        aliases.setdefault(choice_id, []).append(alias)

    meanings: dict[int, object] = {}
    for choice in choices:
        pk = choice['pk']
        if pk in decided:
            meanings[pk] = decided[pk]
        elif pk in blocked:
            meanings[pk] = NONE
        elif pk in legacy:
            meanings[pk] = legacy[pk]
    return choices, meanings, aliases, NONE


def answer_concept_ids(field_names) -> dict[str, dict[str, int]]:
    """The concept each answer of the given fields writes as ``value_as_concept_id``.

    ``{field_name: {spelling: concept_id}}``, where a spelling is the label, the
    stored value or any alias of a choice, so a value saved under an earlier
    label still projects. This is what the projection engine reads (#1224); it
    asks for the approved ``value_as_concept`` decision of each choice, and the
    rules are:

    * an **approved, mapped** decision whose target is still a current standard
      concept writes that concept. A target a vocabulary reload has since
      retired, or made non-standard, writes none: the value still saves, with
      no concept, rather than carry a concept that no longer means it;
    * an **approved no-equivalent** decision writes none, even where the choice
      has a legacy code, because a reviewer said no standard concept exists;
    * a choice with **no decision yet**, or only an open proposal, keeps the
      concept of its primary ``FieldChoiceCode``, as before this existed: the
      proposal migration 0368 made from that code is not a new decision, and
      holding projection back until each is approved would silently stop
      writing concepts that are written today;
    * a choice whose decision **was once made and is gone** (an approval since
      withdrawn or superseded), or whose primary code's own concept a reviewer
      **rejected**, does not fall back to that code. Rejecting some *other*
      proposal for the choice leaves the code in place: nobody has answered for it.

    A spelling that two choices of one field resolve to different concepts (the
    same label in two contexts) writes none: a guess is worse than a plain
    value. Retired choices are included, since stored values still carry them,
    but a live choice's spelling wins over a retired one's.
    """
    # One entry per (field, spelling): the concepts its choices resolve to, with
    # NONE for a choice a decision says writes no concept. A choice nothing
    # decides and no code covers says nothing, as before. Only a spelling that
    # means exactly one concept is projected; where a live choice and a retired
    # one share a spelling, the live one speaks.
    choices, meanings, aliases, NONE = _answer_meanings(field_names)

    def spellings(rows):
        live: dict[str, dict[str, set]] = {}
        retired: dict[str, dict[str, set]] = {}
        for choice in rows:
            pk = choice['pk']
            if pk not in meanings:
                continue
            target = retired if choice['retired_at'] else live
            for text in {choice['display'], choice['value'] or choice['display'], *aliases.get(pk, ())}:
                target.setdefault(choice['field_name'], {}).setdefault(text, set()).add(meanings[pk])
        merged: dict[str, dict[str, set]] = {}
        for field in {*live, *retired}:
            merged[field] = {**retired.get(field, {}), **live.get(field, {})}
        return merged

    def resolved(merged):
        return {field: {text: next(iter(concepts)) for text, concepts in by_text.items()
                        if len(concepts) == 1 and NONE not in concepts}
                for field, by_text in merged.items()}

    curated_rows = [c for c in choices if not c['context_key'].startswith('lookup:')]
    lookup_rows = [c for c in choices if c['context_key'].startswith('lookup:')]
    curated = spellings(curated_rows)
    result = resolved(curated)
    # A decision about a lookup entry speaks only where no curated answer of the
    # field uses the spelling: once a field has answers of its own they replace
    # the lookup, and a lookup decision must not void or override one (a spelling
    # two answers read differently writes none, so even an ambiguous curated
    # spelling is left alone).
    for field, by_text in resolved(spellings(lookup_rows)).items():
        taken = curated.get(field, {})
        for text, concept in by_text.items():
            if text not in taken:
                result.setdefault(field, {})[text] = concept
    return result


def answer_values_by_concept(field_names) -> dict[str, dict[int, str]]:
    """The answer a stored ``value_as_concept_id`` stands for, per field.

    ``{field_name: {concept_id: stored_value}}``: the other direction of
    ``answer_concept_ids``, for the derivation of a record from facts that
    carry the answer as a concept and no text (an ETL's, #1224). It uses the
    same decisions as the projection, so an approved answer reads back as what
    it writes; a choice nobody has decided keeps the concept of its primary
    code, here as there. A
    concept that two answers of one field share (a mapping the curators
    allowed to be many-to-one, or the same concept in two contexts) reads as
    nothing rather than as a guess, and a live answer speaks over a retired one.
    The value is what a save stores for the choice, its ``stored_value``: the
    label renamed since is another spelling of it, never a different answer.
    """
    choices, meanings, _, NONE = _answer_meanings(field_names)

    def values(rows):
        live: dict[str, dict[int, set]] = {}
        retired: dict[str, dict[int, set]] = {}
        for choice in rows:
            meaning = meanings.get(choice['pk'])
            if meaning is None or meaning is NONE:
                continue
            target = retired if choice['retired_at'] else live
            # What a save stores for an answer, a lookup's entry included (its
            # code for ECOG): its value, or its label while it has none. The
            # label can change after the data was saved; the value does not.
            shown = choice['value'] or choice['display']
            target.setdefault(choice['field_name'], {}).setdefault(meaning, set()).add(shown)
        merged: dict[str, dict[int, set]] = {}
        for field in {*live, *retired}:
            merged[field] = {**retired.get(field, {}), **live.get(field, {})}
        return merged

    def resolved(merged):
        return {field: {concept: next(iter(vals)) for concept, vals in by_concept.items() if len(vals) == 1}
                for field, by_concept in merged.items()}

    curated = values([c for c in choices if not c['context_key'].startswith('lookup:')])
    result = resolved(curated)
    # A curated answer speaks over a lookup entry decided to the same concept,
    # as in the other direction (see ``answer_concept_ids``).
    for field, by_concept in resolved(values([c for c in choices if c['context_key'].startswith('lookup:')])).items():
        taken = curated.get(field, {})
        for concept, value in by_concept.items():
            if concept not in taken:
                result.setdefault(field, {})[concept] = value
    return result


def coverage(field_name: str | None = None) -> dict[str, dict[str, int]]:
    """Answer coverage per field, apart from whether the field's question is mapped.

    Counts the choices still offered: ``mapped`` have an approved concept,
    ``no_equivalent`` an approved reviewed outcome, ``proposed`` have only an
    open proposal, and ``unmapped`` have neither. A choice is counted once,
    under its best state, whatever its roles.
    """
    from omop_core.models import FieldChoice, FieldValueConceptMapping as Mapping

    choices = FieldChoice.objects.filter(retired_at__isnull=True).exclude(context_key__startswith='lookup:')
    if field_name:
        choices = choices.filter(field_name=field_name)
    states: dict[int, str] = {}
    rank = {'mapped': 3, 'no_equivalent': 2, 'proposed': 1}
    rows = (Mapping.objects.filter(choice__in=choices, status__in=('approved', 'proposed'))
            .values_list('choice_id', 'status', 'outcome'))
    for choice_id, status, outcome in rows:
        state = 'proposed' if status == 'proposed' else ('mapped' if outcome == 'mapped' else 'no_equivalent')
        if rank[state] > rank.get(states.get(choice_id), 0):
            states[choice_id] = state
    result: dict[str, dict[str, int]] = {}
    for pk, name in choices.values_list('pk', 'field_name'):
        entry = result.setdefault(name, {'choices': 0, 'mapped': 0, 'no_equivalent': 0,
                                         'proposed': 0, 'unmapped': 0})
        entry['choices'] += 1
        entry[states.get(pk, 'unmapped')] += 1
    return result


def resolve_unresolved(*, dry_run: bool = False) -> dict[str, int]:
    """Point proposals that carry only a code at the concept now loaded for it.

    Migration ``0368`` runs before any vocabulary on a fresh instance, so every
    proposal it carries over names its concept by vocabulary and code with no
    target. After a load this finds them again: a current standard concept
    becomes the target (and the proposal is ready to review); one that is
    retired or not standard is recorded as such; one still not loaded stays as
    it is. Approved rows are never touched, and a replay changes nothing.
    ``load_athena_vocabularies`` calls it after every load, and
    ``manage.py resolve_field_value_mappings`` does it by hand.
    """
    from omop_core.models import FieldValueConceptMapping as Mapping

    ctx = CheckContext()
    counts = {'resolved': 0, 'invalid': 0, 'non_standard': 0, 'still_unresolved': 0}
    rows = (Mapping.objects.filter(status='proposed', outcome='mapped', target_concept__isnull=True)
            .exclude(concept_code='').select_related('choice'))
    for mapping in rows:
        concept = lookup_concept(vocabulary_id=mapping.vocabulary_id, concept_code=mapping.concept_code)
        if concept is None:
            counts['still_unresolved'] += 1
            continue
        if concept.invalid_reason:
            outcome = 'invalid'
        elif concept.standard_concept != 'S':
            outcome = 'non_standard'
        elif target_problem(concept, mapping.role, mapping.choice.field_name, ctx=ctx):
            outcome = 'invalid'
        else:
            outcome = 'resolved'
        counts[outcome] += 1
        if dry_run or (outcome == mapping.source_resolution and outcome != 'resolved'):
            continue
        with transaction.atomic():
            mapping.source_resolution = outcome
            if outcome == 'resolved':
                mapping.target_concept = concept
                mapping.vocabulary_release = release_of(concept.vocabulary_id)
            mapping.save()
            _record(mapping, 'resolved', None, 'resolver')
    return counts
