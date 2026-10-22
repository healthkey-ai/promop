"""Owned NOTE references for text in CDM-width fact columns.

This is an application encoding, not a standard OMOP event relationship.
Only callers supplying the owning fact and context may resolve a reference.
"""
import re
from collections import Counter

from omop_core.models import Note
from omop_core.services.pk import next_pk


_REFERENCE = re.compile(r'\[note:([0-9]+)\]$')
_MAX_NOTE_ID = 2**63 - 1


def note_id(value):
    match = _REFERENCE.search(value or '')
    if not match or len(match[1]) > 19:
        return None
    pk = int(match[1])
    return pk if 0 < pk <= _MAX_NOTE_ID else None


def note_source(row, namespace):
    # Table identity is required: Measurement and Observation IDs overlap.
    return f'{namespace}:{row._meta.db_table}:{row.pk}'


def store_text(row, value, *, namespace, context):
    """Store overflow and escape literal reference-shaped text in owned notes.

    The caller allocates the fact ID first and wraps fact + NOTE writes in a
    transaction. Notes are immutable history: edits may create a new note;
    retiring a fact does not delete its earlier narrative.
    """
    width = row._meta.get_field('value_as_string').max_length
    if not value or (len(value) <= width and not _REFERENCE.search(value)):
        return value
    note = Note.objects.create(
        note_id=next_pk(Note, 'note_id'), person_id=row.person_id,
        note_date=getattr(row, f'{row._meta.db_table}_date'),
        note_type_concept_id=0, note_source_value=note_source(row, namespace),
        note_title=context, note_text=value,
    )
    return f'[note:{note.pk}]'


class OwnedTextReader:
    """One patient-scoped NOTE query for a collection of snapshot facts."""

    def __init__(self, person_id, rows):
        self.person_id = person_id
        ids = [note_id(row.value_as_string) for row in rows
               if row.person_id == person_id]
        self.references = Counter(pk for pk in ids if pk is not None)
        self.notes = {
            note.pk: note for note in Note.objects.filter(
                person_id=person_id, pk__in=self.references,
            )
        } if self.references else {}

    def read(self, row, *, namespace, context, legacy_source=None):
        value = row.value_as_string
        if row.person_id != self.person_id:
            return value
        note = self.notes.get(note_id(value))
        if note is None:
            return value
        if (value == f'[note:{note.pk}]'
                and note.note_source_value == note_source(row, namespace)
                and note.note_title == context):
            return note.note_text  # One level only: never follow narrative text.
        # Old genomics notes identify only a parent, not an individual fact.
        # Resolve only a unique, exact encoding made by the old overflow writer.
        # Ambiguous, missing or inconsistent references remain literal source
        # text for reconciliation; never guess their component ownership.
        if (legacy_source and note.note_source_value == legacy_source
                and self.references[note.pk] == 1
                and note.note_date == getattr(row, f'{row._meta.db_table}_date')
                and len(note.note_text) > 60):
            reference = f'[note:{note.pk}]'
            if value == note.note_text[:60 - len(reference)] + reference:
                return note.note_text
        return value
