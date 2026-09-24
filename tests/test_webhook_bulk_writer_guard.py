"""Every bulk writer of patient data has to say so, or say why it does not.

Django's `post_save`/`post_delete` do not fire for `bulk_create`,
`bulk_update`, `QuerySet.update()` or `QuerySet.delete()`. The webhook
publisher is connected to those signals, so a writer that uses any of them
changes patient data silently — no subscriber hears it, and nothing in the code
says that was intended.

That is not hypothetical: five such paths were found by hand while building
this feature (FHIR sync's `bulk_create`, the TP53 cache reconciler's
`QuerySet.update`, and three in `copy_patient`), each after the tests for both
sides passed. `docs/webhooks_architecture.md` asks bulk writers to call
`publish_patient_bulk_change`; nothing enforced it.

This scans the request-path packages — the API and the services a request
reaches — and requires each file that bulk-writes a patient-event model either
to publish, to suppress deliberately, or to appear below with a reason.
Management commands are out of scope: an operator running maintenance is not a
tenant's data changing under them.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ('patient_portal/api', 'omop_core/services')
BULK_CALLS = {'bulk_create', 'bulk_update', 'update', 'delete'}

# Reasons, not exemptions. A file here still bulk-writes patient data; the
# entry says why no event follows. "Not reviewed" is an honest value and is
# tracked in the issue named beside it.
ALLOWED = {
    'patient_portal/api/org_views.py':
        'marks derivation_version stale for a tenant. A derived-field marker, '
        'not patient data: announcing it would fan one event per patient out '
        'of an admin PATCH.',
    'omop_core/services/patient_cleanup.py':
        'deletes a patient\'s clinical rows immediately before the Person, '
        'whose post_delete announces the removal once. Per-table events here '
        'would repeat it for every table.',
    'omop_core/services/patient_record_service.py':
        'writes derivation bookkeeping onto the record it just derived; the '
        'clinical write that triggered the derivation is what subscribers hear.',
    'omop_core/services/sample_patient_disease_status.py':
        'seeds sample data for a demo tenant.',
    'patient_portal/api/lab_results/views.py':
        'NOT REVIEWED — deletes measurements orphaned by deleting a visit, '
        'and a subscriber plausibly wants that. See issue #1592.',
    'omop_core/services/episode_service.py':
        'NOT REVIEWED — deletes derived episode observations. See issue #1592.',
    'omop_core/services/genomics.py':
        'NOT REVIEWED — updates one measurement through a queryset, so the '
        'signal does not fire for it. See issue #1592.',
}
PATIENT_EVENT_CLASSES = {
    'Person', 'PatientRecord', 'Measurement', 'PatientDocument',
    'ConditionOccurrence', 'DrugExposure', 'Observation',
    'ProcedureOccurrence', 'PatientTrialEnrollment', 'Episode',
}


def _bulk_writes(path):
    """Calls like `Measurement.objects.bulk_create(...)` in one file."""
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in BULK_CALLS:
            continue
        receiver = ast.unparse(node.func.value)
        if '.objects' not in receiver:
            continue
        if any(f'{name}.objects' in receiver for name in PATIENT_EVENT_CLASSES):
            found.append(f'{path.relative_to(ROOT)}:{node.lineno} {node.func.attr}')
    return found


def test_every_bulk_writer_of_patient_data_announces_it_or_says_why_not():
    unannounced = {}
    for package in SCANNED:
        for path in sorted((ROOT / package).rglob('*.py')):
            if path.name.startswith('test_') or path.name == 'tests.py' or 'tests' in path.parts:
                continue
            writes = _bulk_writes(path)
            if not writes:
                continue
            source = path.read_text()
            if 'publish_patient_bulk_change' in source or 'suppress_webhook_events' in source:
                continue
            relative = str(path.relative_to(ROOT))
            if relative in ALLOWED:
                continue
            unannounced[relative] = writes

    assert not unannounced, (
        'These bulk-write a patient-event model without publishing an event. '
        'Signals do not fire for bulk_create/bulk_update/QuerySet.update/delete, '
        'so no subscriber hears the change. Call publish_patient_bulk_change, '
        'or add the file to ALLOWED above with the reason:\n'
        + '\n'.join(f'  {name}: {sites}' for name, sites in sorted(unannounced.items()))
    )


def test_the_allowlist_has_no_stale_entries():
    """An entry that no longer bulk-writes anything is a reason nobody needs."""
    stale = [
        name for name in ALLOWED
        if not _bulk_writes(ROOT / name)
        or 'publish_patient_bulk_change' in (ROOT / name).read_text()
    ]
    assert not stale, f'ALLOWED entries that no longer need one: {stale}'
