"""`stage` keeps the tab's disease-specific answers.

Ann Arbor for follicular lymphoma, ISS/R-ISS for myeloma and Rai for CLL all
write the same `PatientRecord.stage` column, and each tab supplies its own
reviewed list. The single `FieldChoice` set is the lymphoma one, so attaching
it to the descriptor overrode the tab in the federated editor — a myeloma
record offered Ann Arbor stages. Every other curated field keeps its set.
"""
import pytest

from omop_core.models import FieldChoice
from omop_core.services.write_descriptor import build_writable_field_descriptor

pytestmark = pytest.mark.django_db


def test_stage_does_not_take_the_lymphoma_answer_set():
    for order, display in enumerate(('I', 'II', 'III', 'IV')):
        FieldChoice.objects.create(field_name='stage', display=display, sort_order=order)

    entry = build_writable_field_descriptor()['stage']

    assert entry['writable'] is True
    assert entry.get('options') is None


def test_other_curated_fields_keep_their_answer_sets():
    FieldChoice.objects.create(field_name='bone_lesions', display='Yes')

    entry = build_writable_field_descriptor()['bone_lesions']

    assert [option['value'] for option in entry['options']] == ['Yes']
