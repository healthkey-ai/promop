"""Scoped, reviewed answer resolution shared by curation, projection and imports.

No lexical concept lookup occurs in clinical writes. Missing or ambiguous
resolutions retain source values; only reviewed, current targets can be emitted.
"""
from datetime import date

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from omop_core.models import FieldChoice, FieldValueConceptMapping, FieldValueMappingRevision


def standard_target(concept, domains=None):
    if concept is None:
        return False
    today = timezone.localdate()
    start = date.fromisoformat(str(concept.valid_start_date))
    end = date.fromisoformat(str(concept.valid_end_date))
    return (0 < concept.pk < 2_000_000_000 and concept.standard_concept == 'S'
            and not concept.invalid_reason and concept.source != 'HealthKey'
            and not concept.vocabulary_id.startswith('HK-') and start <= today <= end
            and (domains is None or concept.domain_id in domains))


def validate_value_mapping(mapping):
    if mapping.status != 'approved':
        return
    if not mapping.notes.strip():
        raise ValidationError({'notes': 'Record the evidence for this decision.'})
    if mapping.outcome not in ('mapped', 'no_equivalent', 'not_applicable', 'structured'):
        raise ValidationError({'outcome': 'Unresolved or ambiguous mappings cannot be approved.'})
    if mapping.outcome != 'mapped':
        if mapping.target_concept_id or mapping.question_concept_id:
            raise ValidationError({'target_concept': 'An unmapped disposition cannot carry a target.'})
        return
    domains = {'Meas Value'} if mapping.role == 'answer' else {'Measurement', 'Observation', 'Condition', 'Procedure', 'Drug'}
    if mapping.role == 'structured' or not standard_target(mapping.target_concept, domains):
        raise ValidationError({'target_concept': 'Select a current external standard concept in the appropriate domain.'})
    if mapping.question_concept_id and not standard_target(mapping.question_concept, {'Measurement', 'Observation'}):
        raise ValidationError({'question_concept': 'Select a current standard Measurement or Observation question.'})
    if mapping.role == 'fact' and mapping.question_concept_id:
        raise ValidationError({'question_concept': 'A clinical assertion is its own fact concept.'})
    if not mapping.vocabulary_release:
        raise ValidationError({'vocabulary_release': 'Record the vocabulary release used for review.'})


def lock_scope(field_name, context_key):
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', [f'field-choice:{field_name}:{context_key}'])


def value_key(value):
    # Keep booleans and numbers distinct, but accept scalar text aliases.
    if isinstance(value, bool):
        return ('boolean', value)
    return ('scalar', str(value).strip().casefold())


def choice_aliases(choice):
    return [choice.code, choice.display, choice.canonical_value, *choice.aliases]


def validate_choice(choice):
    if not isinstance(choice.aliases, list) or any(not isinstance(a, str) or not a.strip() for a in choice.aliases):
        raise ValidationError({'aliases': 'Use a list of non-empty text aliases.'})
    if isinstance(choice.canonical_value, (dict, list)):
        raise ValidationError({'canonical_value': 'A choice must have a scalar canonical value.'})
    if choice.retired:
        return
    own = {value_key(v) for v in choice_aliases(choice) if v is not None and v != ''}
    for other in FieldChoice.objects.filter(field_name=choice.field_name, context_key=choice.context_key, retired=False).exclude(pk=choice.pk):
        if own & {value_key(v) for v in choice_aliases(other) if v is not None and v != ''}:
            raise ValidationError({'aliases': 'This value or alias already belongs to another choice in this context.'})


@transaction.atomic
def save_mapping(choice, data, reviewer=None, audit_context=None):
    choice = FieldChoice.objects.select_for_update().get(pk=choice.pk)
    mapping = FieldValueConceptMapping.objects.filter(choice=choice).first() or FieldValueConceptMapping(choice=choice)
    for key, value in data.items():
        setattr(mapping, key, value)
    mapping.reviewer = reviewer
    mapping.reviewed_at = timezone.now() if mapping.status in ('approved', 'rejected') else None
    mapping.full_clean()
    mapping.revision += 1
    mapping.save()
    decision = mapping_data(mapping)
    if audit_context:
        decision['origin'] = audit_context
    FieldValueMappingRevision.objects.create(mapping=mapping, revision=mapping.revision, decision=decision)
    return mapping


def mapping_data(mapping):
    if mapping is None:
        return None
    def concept_data(concept):
        return None if concept is None else {'concept_id': concept.pk, 'vocabulary_id': concept.vocabulary_id,
            'concept_code': concept.concept_code, 'concept_name': concept.concept_name, 'domain_id': concept.domain_id}
    return {'target_concept': concept_data(mapping.target_concept), 'question_concept': concept_data(mapping.question_concept),
            'role': mapping.role, 'status': mapping.status, 'outcome': mapping.outcome,
            'notes': mapping.notes, 'vocabulary_release': mapping.vocabulary_release,
            'revision': mapping.revision, 'reviewer_id': mapping.reviewer_id,
            'reviewed_at': mapping.reviewed_at.isoformat() if mapping.reviewed_at else None}


def choice_queryset():
    return FieldChoice.objects.select_related('value_mapping__target_concept', 'value_mapping__question_concept')


def historical_questions(field_name, context_key=''):
    """Resolve prior approved override questions by portable vocabulary/code.

    Retiring a choice or changing its question must not prevent a later clear
    from suppressing facts written using the old decision.
    """
    from django.db.models import Q
    from omop_core.models import Concept

    mappings = FieldValueConceptMapping.objects.filter(
        choice__field_name=field_name, choice__context_key=context_key,
    )
    keys = set(mappings.exclude(question_concept=None).values_list(
        'question_concept__vocabulary_id', 'question_concept__concept_code',
    ))
    for decision in FieldValueMappingRevision.objects.filter(
        mapping__in=mappings, decision__status='approved', decision__outcome='mapped',
    ).values_list('decision', flat=True):
        question = decision.get('question_concept')
        if question:
            keys.add((question['vocabulary_id'], question['concept_code']))
    query = Q(pk__in=[])
    for vocabulary, code in keys:
        query |= Q(vocabulary_id=vocabulary, concept_code=code)
    return Concept.objects.filter(query, domain_id__in=['Measurement', 'Observation'])


def store_source_answer(row, raw):
    """Keep long source aliases in a patient/fact-linked NOTE, never truncate."""
    from omop_core.models import Note
    from omop_core.services.pk import next_pk

    text = str(raw)
    if len(text) <= row._meta.get_field('value_source_value').max_length:
        return text, False
    source = f'field-answer:{row._meta.db_table}:{row.pk}'
    note = Note.objects.filter(person_id=row.person_id, note_source_value=source).first()
    changed = note is None or note.note_text != text
    if note is None:
        note = Note.objects.create(
            note_id=next_pk(Note, 'note_id'), person_id=row.person_id,
            note_date=getattr(row, f'{row._meta.db_table}_date'),
            note_type_concept_id=0, note_source_value=source, note_text=text,
        )
    elif changed:
        note.note_text = text
        note.save(update_fields=['note_text'])
    return f'[note:{note.pk}]', changed


class ValueResolver:
    """One bounded query for a batch; instances never outlive a request/refresh."""
    @classmethod
    def for_snapshot(cls, snapshot):
        cache = getattr(snapshot, 'genomics_cache', {})
        if 'field_value_resolver' not in cache:
            cache['field_value_resolver'] = cls()
        return cache['field_value_resolver']

    def __init__(self, fields=None):
        qs = choice_queryset().filter(retired=False)
        if fields is not None:
            qs = qs.filter(field_name__in=fields)
        self.choices = {}
        for choice in qs:
            self.choices.setdefault((choice.field_name, choice.context_key), []).append(choice)

    def resolve(self, field, value, context=''):
        matches = [c for c in self.choices.get((field, context), []) if value_key(value) in {value_key(a) for a in choice_aliases(c)}]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def mapping(choice):
        mapping = getattr(choice, 'value_mapping', None) if choice else None
        if mapping is None or mapping.status != 'approved' or mapping.outcome != 'mapped':
            return None
        try:
            validate_value_mapping(mapping)
        except ValidationError:
            return None
        return mapping

    def reverse(self, field, row, context=''):
        target_id = getattr(row, 'value_as_concept_id', None)
        if target_id:
            question_id = getattr(row, 'measurement_concept_id', None) or getattr(row, 'observation_concept_id', None)
            matches = []
            for choice in self.choices.get((field, context), []):
                mapping = self.mapping(choice)
                if (mapping and mapping.role == 'answer' and mapping.target_concept_id == target_id
                        and (not mapping.question_concept_id or mapping.question_concept_id == question_id)):
                    matches.append(choice)
            if len(matches) == 1:
                return matches[0].canonical_value
        raw = row.value_as_number if row.value_as_number is not None else row.value_as_string
        choice = self.resolve(field, raw, context) if raw is not None else None
        return choice.canonical_value if choice else raw
