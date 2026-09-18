"""Every model is system, reference or patient data, exactly once."""
from django.apps import apps

from omop_core.services import instance_data


def _all_labels() -> set[str]:
    """Label of every installed model, auto-created M2M tables included."""
    return {m._meta.label for m in apps.get_models(include_auto_created=True)}


def test_every_model_has_exactly_one_class():
    classes = {
        'system': instance_data.SYSTEM,
        'reference': instance_data.reference(),
        'patient': instance_data.PATIENT,
    }
    seen = {}
    for name, labels in classes.items():
        for label in labels:
            assert label not in seen, f'{label} is both {seen[label]} and {name}'
            seen[label] = name

    unclassified = sorted(_all_labels() - set(seen))
    assert not unclassified, (
        f'Classify these in omop_core/services/instance_data.py: {unclassified}'
    )


def test_every_classified_label_is_a_real_model():
    stale = sorted(
        (instance_data.SYSTEM | instance_data.reference() | instance_data.PATIENT) - _all_labels()
    )
    assert not stale, f'No such model: {stale}'


def test_copy_patient_covers_every_patient_table():
    from omop_core.services.patient_transfer import PATIENT_TABLES

    copied = {t.model._meta.label for t in PATIENT_TABLES} | {
        'omop_core.Person', 'omop_core.Location', 'omop_core.ProvenanceRecord',
    }
    # No clinical row references these yet, so there is nothing to follow.
    assert instance_data.PATIENT - copied == {'omop_core.CareSite', 'omop_core.Provider'}


def test_copy_reference_data_covers_every_copied_reference_table():
    from omop_core.services.reference_transfer import CANDIDATES, copied_tables

    curation = {
        'omop_core.CustomPatientField', 'omop_core.FieldChoice', 'omop_core.FieldChoiceCode',
        'omop_core.FieldConceptMapping', 'omop_core.FieldFormula', 'omop_core.FieldSynonym',
        'omop_core.SourceCodeConceptMapping',
    }
    handled = {t.model._meta.label for t in (*copied_tables(), CANDIDATES)} | curation | {
        'omop_core.TherapyOutcome_diseases',
    }
    assert instance_data.reference_copied() == handled
