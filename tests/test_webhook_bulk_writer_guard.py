"""Every bulk writer of patient data has to say so, or say why it does not.

Django's `post_save` does not fire for `bulk_create`, `bulk_update` or
`QuerySet.update()`. The webhook publisher is connected to that signal, so a
writer that uses one of them changes patient data silently — no subscriber
hears it, and nothing in the code says that was intended.

That is not hypothetical: five such paths were found by hand while building
this feature (FHIR sync's `bulk_create`, the TP53 cache reconciler's
`QuerySet.update`, and three in `copy_patient`), each after the tests for both
sides passed. `docs/webhooks_architecture.md` asks bulk writers to call
`publish_patient_bulk_change`; nothing enforced it.

`QuerySet.delete()` is deliberately not in that list. `Collector.can_fast_delete`
returns False when a model has `post_delete` listeners, so a queryset delete
fetches the rows and fires the signal for each one — an aggregate demanded here
would duplicate them.

That makes a delete *signalled*, which is not the same as *announced*. For
anything but a `PatientRecord` the receiver resolves the organization through
`PatientRecord`, so a cascade that removed the record first leaves the rest of
the rows with nowhere to send their event: a patient-wide delete can go quiet
while a single-table delete is noisy. That is a defect in the publisher, not
something this guard can see — issue #1592.

**Granularity.** The check is per function, not per file. A file-level escape
would have exempted `patient_portal/api/views.py`, `api/fhir/sync.py` and
`services/patient_transfer.py` in their entirety — the files three of those
five bugs lived in — because an unrelated call site elsewhere in the same file
publishes.

**What it does not see.** The receiver has to be spelled `Model.objects...`:
a queryset held in a local (`qs = Measurement.objects.filter(...)`, then
`qs.update(...)`), a related manager (`person.measurement_set.update(...)`),
`self.get_queryset().update(...)`, an aliased import, `_raw_delete` and raw SQL
all pass unnoticed. It catches the idiom this codebase actually writes, and is
a ratchet against new ones, not a proof.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ('patient_portal/api', 'omop_core/services')
BULK_CALLS = {'bulk_create', 'bulk_update', 'update'}
ANNOUNCERS = {'publish_patient_bulk_change', 'suppress_webhook_events'}
PATIENT_EVENT_CLASSES = {
    'Person', 'PatientRecord', 'Measurement', 'PatientDocument',
    'ConditionOccurrence', 'DrugExposure', 'Observation',
    'ProcedureOccurrence', 'PatientTrialEnrollment', 'Episode',
}

# Reasons, not exemptions. Each entry still bulk-writes patient data; it says
# why no event follows. "NOT REVIEWED" is an honest value, and is tracked in
# the issue named beside it.
ALLOWED = {
    'patient_portal/api/org_views.py::OrgDetailView.patch':
        'marks derivation_version stale for a tenant when its unit policy '
        'changes. A derived-field marker, not patient data: announcing it '
        'would fan one event per patient out of an admin PATCH.',
    'omop_core/services/patient_record_service.py::recompute_patient_record_fields':
        'writes the record it has just derived — the read model, not the '
        'clinical rows. Subscribers hear the write that triggered the '
        'derivation; this would repeat it under a second name.',
    'omop_core/services/patient_record_service.py::recompute_formula_field':
        'the same read model, recomputed across every record of every tenant. '
        'No clinical write triggered it, so there is nothing for an event to '
        'correspond to; what it writes is derived state.',
    'omop_core/services/sample_patient_disease_status.py::ensure_sample_patient_disease_status':
        'seeds sample data for a demo tenant.',
    'omop_core/services/genomics.py::delete_variant':
        'NOT REVIEWED — marks measurements erroneous through querysets, so the '
        'post_save a .save() would have fired does not. It ends with '
        'refresh_patient_record, so a patient.changed still goes out; what is '
        'lost is the lab.updated typing a Measurement save would have carried. '
        'A second update in the same function (a queryset held in a local) is '
        'invisible to this guard. See issue #1592.',
}


def _scopes(tree):
    """(qualified name, node) for every scope in a module, module included.

    Qualified, because a file holds several `patch` methods: keyed on the bare
    name, one scope's entry overwrites another's and a real writer disappears
    behind a namesake that has nothing to report.

    Each scope is yielded once. Recursing into ordinary statements with the
    module's own empty prefix yielded `<module>` again for every statement in
    the file — paired with the statement rather than the module, so a write
    inside a module-level `if` was reported as silent even when the module
    published, and counted once per enclosing statement.
    """
    yield '<module>', tree
    yield from _nested_scopes(tree, '')


def _nested_scopes(node, prefix):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = f'{prefix}{child.name}'
            yield name, child
            yield from _nested_scopes(child, f'{name}.')
        elif isinstance(child, ast.ClassDef):
            yield from _nested_scopes(child, f'{prefix}{child.name}.')
        else:
            # A statement, not a scope: keep looking inside it for defs, and
            # do not yield anything of its own.
            yield from _nested_scopes(child, prefix)


def _in_scope(node):
    """Every node of this scope, not descending into nested functions.

    They are scopes of their own: a publisher in the enclosing function says
    nothing about what a closure inside it does, and vice versa.
    """
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        yield child
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        stack.extend(ast.iter_child_nodes(child))


def _bulk_writes(node):
    """Calls like `Measurement.objects.bulk_create(...)` in this scope."""
    found = []
    for child in _in_scope(node):
        if not (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)):
            continue
        if child.func.attr not in BULK_CALLS:
            continue
        receiver = ast.unparse(child.func.value)
        if any(f'{name}.objects' in receiver for name in PATIENT_EVENT_CLASSES):
            found.append((child.lineno, child.func.attr))
    return sorted(found)


def _announces(node):
    return any(
        (isinstance(child, ast.Name) and child.id in ANNOUNCERS)
        or (isinstance(child, ast.Attribute) and child.attr in ANNOUNCERS)
        for child in _in_scope(node)
    )


def _python_files():
    for package in SCANNED:
        for path in sorted((ROOT / package).rglob('*.py')):
            if path.name.startswith('test_') or path.name == 'tests.py':
                continue
            if 'tests' in path.parts:
                continue
            yield path


def _unannounced():
    """{'path::function': [(line, call)]} for every scope that writes silently."""
    silent = {}
    for path in _python_files():
        tree = ast.parse(path.read_text())
        for name, node in _scopes(tree):
            writes = _bulk_writes(node)
            if writes and not _announces(node):
                silent.setdefault(f'{path.relative_to(ROOT)}::{name}', []).extend(writes)
    return silent


def test_every_bulk_writer_of_patient_data_announces_it_or_says_why_not():
    unannounced = {key: sites for key, sites in _unannounced().items() if key not in ALLOWED}
    assert not unannounced, (
        'These bulk-write a patient-event model without publishing an event. '
        'post_save does not fire for bulk_create/bulk_update/QuerySet.update, '
        'so no subscriber hears the change. Call publish_patient_bulk_change '
        'in that function, or add it to ALLOWED above with the reason:\n'
        + '\n'.join(f'  {key}: {sites}' for key, sites in sorted(unannounced.items()))
    )


def test_the_allowlist_has_no_stale_entries():
    """An entry that no longer writes silently is a reason nobody needs.

    Covers the ways one goes stale: the function grew a publisher, the writes
    moved out of it, or the file was renamed or deleted.
    """
    silent = _unannounced()
    stale = sorted(key for key in ALLOWED if key not in silent)
    assert not stale, f'ALLOWED entries that no longer need one: {stale}'
