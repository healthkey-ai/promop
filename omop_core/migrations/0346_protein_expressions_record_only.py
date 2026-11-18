"""protein_expressions is a record-only multi-answer field (#2574).

The seeded curation row projected the comma-joined answer into one
``Observation.value_as_string`` (60 characters), which cannot hold a realistic
selection of markers. The field is written to ``PatientRecord`` only now, and
the extractor derives imported markers in the picker's reviewed spelling.

This migration:

* keeps the answer of a record the retired projection actually acknowledged —
  one with an ``Observation`` on the seeded concept — pending, so a refresh
  cannot replace a clinician's saved marker with an older imported one. Those
  observations are no longer read (the generic curated reader skips this field);
* relabels the remaining stored answers from the pre-v32 imported form
  (``CD5: positive`` -> ``CD5 +ve``, ``ZAP70`` -> ``ZAP-70``, the kappa/lambda
  ligatures) so the v32 derivation recognises them;
* records each relabel in the field-level revision history (old -> new,
  ``changed_by='system'``), like migration 0321;
* deletes the seeded projection row, and only while it is untouched.

The reverse re-creates the seeded row when it is absent. Relabelled values are
not rolled back (they are the same answer in the curated spelling).
"""
import re

from django.db import migrations


SEEDED = {
    'field_name': 'protein_expressions',
    'vocabulary_id': 'SNOMED',
    'concept_code': '371511004',
    'provenance': 'system_generated',
    'multiple': False,
    'status': 'approved',
    'omop_table': 'Observation',
    'source_value': '',
    'value_kind': '',
    'tab': '',
    'notes': 'Auto-proposed from field suggestion.',
}

_ALIASES = {
    'Kappa (κ) light chain': 'Kappa light chain',
    'Lambda (λ) light chain': 'Lambda light chain',
    'Kappa (k) light chain': 'Kappa light chain',
    'Lambda (l) light chain': 'Lambda light chain',
}
_POLARITY = {'positive': '+ve', 'pos': '+ve', 'present': '+ve',
             'negative': '-ve', 'neg': '-ve', 'absent': '-ve'}
_IMPORTED = re.compile(r'^(?P<label>[^:]+):\s*(?P<answer>.+)$')

#: Frozen copy of the extractor's marker table (migrations never import live
#: code). Bounded, so CD50 is not CD5 and a ratio is not a marker.
_MARKERS = (
    (re.compile(r'(?<![A-Z0-9])CD-?38(?![0-9])'), 'CD38'),
    (re.compile(r'(?<![A-Z0-9])CD-?49D(?![0-9])'), 'CD49d'),
    (re.compile(r'(?<![A-Z0-9])CD-?19(?![0-9])'), 'CD19'),
    (re.compile(r'(?<![A-Z0-9])CD-?20(?![0-9])'), 'CD20'),
    (re.compile(r'(?<![A-Z0-9])CD-?23(?![0-9])'), 'CD23'),
    (re.compile(r'(?<![A-Z0-9])CD-?5(?![0-9])'), 'CD5'),
    (re.compile(r'(?<![A-Z0-9])ZAP-?70(?![0-9])'), 'ZAP-70'),
    (re.compile(r'(?<![A-Z0-9])KAPPA(?![A-Z])'), 'Kappa light chain'),
    (re.compile(r'(?<![A-Z0-9])LAMBDA(?![A-Z])'), 'Lambda light chain'),
)
_EXCLUDES = ('/', 'RATIO', 'FREE', 'SERUM', 'PLASMA', 'CONCENTRATION')


def _marker_label(label):
    upper = label.upper()
    if any(token in upper for token in _EXCLUDES):
        return None
    labels = {marker for pattern, marker in _MARKERS if pattern.search(upper)}
    return labels.pop() if len(labels) == 1 else None


def _relabel(answer):
    text = answer.strip()
    if not text:
        return text
    for old, new in _ALIASES.items():
        if text.startswith(old):
            text = new + text[len(old):]
            break
    match = _IMPORTED.match(text)
    if match:
        label = _marker_label(match.group('label')) or match.group('label').strip()
        raw = match.group('answer').strip()
        token = raw.casefold()
        if token in _POLARITY:
            return f'{label} {_POLARITY[token]}'
        return f'{label}: {raw}'
    return text


def _looks_like_answer(part):
    text = part.strip()
    if not text:
        return False
    if _IMPORTED.match(text):
        return True
    if _marker_label(text.split(':')[0]):
        return True
    return text.split()[-1].casefold() in _POLARITY


def _answers(raw):
    """Split a stored value into answers.

    A legacy answer may itself contain a comma (``CD5: positive, 90%``); a
    following part only starts a new answer when it looks like one.
    """
    text = str(raw or '').strip()
    parts = text.split(',')
    if len(parts) > 1 and not all(_looks_like_answer(part) for part in parts[1:]):
        return [text]
    return parts


def _relabel_field(value):
    answers = []
    for part in _answers(value):
        text = _relabel(part)
        if text and text not in answers:
            answers.append(text)
    return ', '.join(answers) or None


def remove_seeded_projection(apps, schema_editor):
    alias = schema_editor.connection.alias
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    PatientRecord = apps.get_model('omop_core', 'PatientRecord')
    RecordRevision = apps.get_model('omop_core', 'RecordRevision')
    Observation = apps.get_model('omop_core', 'Observation')
    Concept = apps.get_model('omop_core', 'Concept')

    concept = Concept.objects.using(alias).filter(concept_code='371511004').first()
    acknowledged = set()
    if concept is not None:
        acknowledged = set(
            Observation.objects.using(alias)
            .filter(observation_concept_id=concept.pk)
            .values_list('person_id', flat=True)
        )

    records = PatientRecord.objects.using(alias).exclude(
        protein_expressions__isnull=True).exclude(protein_expressions='').only(
        'pk', 'person_id', 'protein_expressions', 'user_edited_fields')
    for record in records.iterator(chunk_size=500):
        updates = {}
        if record.person_id in acknowledged:
            # The retired projection acknowledged this answer, so it left
            # `user_edited_fields`; keep it pending or the refresh replaces a
            # clinician's selection with an older imported marker.
            pending = list(record.user_edited_fields or [])
            if 'protein_expressions' not in pending:
                pending.append('protein_expressions')
                updates['user_edited_fields'] = pending
        else:
            relabelled = _relabel_field(record.protein_expressions)
            if relabelled and relabelled != record.protein_expressions:
                RecordRevision.objects.using(alias).create(
                    patient_record_id=record.pk, field='protein_expressions',
                    changed_by='system',
                    old_value=str(record.protein_expressions), new_value=relabelled,
                )
                updates['protein_expressions'] = relabelled
        if updates:
            PatientRecord.objects.using(alias).filter(pk=record.pk).update(**updates)

    FieldConceptMapping.objects.using(alias).filter(**SEEDED).delete()


def restore_seeded_projection(apps, schema_editor):
    alias = schema_editor.connection.alias
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')
    if FieldConceptMapping.objects.using(alias).filter(
            field_name='protein_expressions').exists():
        return
    concept = Concept.objects.using(alias).filter(concept_code='371511004').first()
    FieldConceptMapping.objects.using(alias).create(
        field_name='protein_expressions', concept=concept,
        vocabulary_id='SNOMED', concept_code='371511004', status='approved',
        omop_table='Observation', multiple=False,
        provenance='system_generated',
        notes='Auto-proposed from field suggestion.',
    )


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0345_normalise_stage_labels')]

    operations = [
        migrations.RunPython(remove_seeded_projection, restore_seeded_projection),
    ]
