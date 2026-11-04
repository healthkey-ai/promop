"""Lab derivation routes through the curation tables; _LOINC_LAB_FIELDS seeds them.

The checked-in table stays as the inventory of lab fields and the seed for
FieldConceptMapping (0240, 0326) and the alias SourceCodeConceptMapping rows
(0285). At derivation time the tables decide: ``resolve_lab_code_fields``
reads approved, concept-resolved mappings and their approved aliases, and the
table answers only for a field that has no such mapping.
"""
import importlib
import json
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import connection

from omop_core.models import FieldConceptMapping, SourceCodeConceptMapping
from omop_core.services.patient_record_service import (
    LAB_FIELDS, _DEMO_ONLY_LAB_CODES, _LOINC_LAB_FIELDS, _build_snapshot,
    _get_mm_specific_data, _latest_blood_count_measurements,
    refresh_patient_record, resolve_lab_code_fields,
)
from omop_core.services.provenance_registry import get_registry
from tests.conftest import FIELD_CURATION_FIXTURE
from tests.factories import ConceptFactory, MeasurementFactory, PersonFactory
from tests.migration_utils import run_data_migration

pytestmark = pytest.mark.django_db

migration = importlib.import_module(
    'omop_core.migrations.0326_approve_lab_field_mappings',
)

def _approve_alias(code, field, **extra):
    target = FieldConceptMapping.objects.get(field_name=field).concept_id
    return SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='LOINC', source_code=code, target_concept_id=target,
        status='approved', omop_table='measurement', origin='curator', **extra,
    )


# ── The content guarantee ───────────────────────────────────────────────────

def test_fixture_approves_every_extractor_field_on_a_code_the_table_routes():
    """Each lab field in the table has an approved LOINC measurement mapping in
    the fixture 0240 seeds, on a code the table sends to that same field; 0326
    carries the identical pair for the fields it froze. A field added after
    0326 ran is seeded by the fixture on a fresh install and derives from the
    table on an existing one until a curator (or a later migration) approves
    it, so the migration's set is a subset, never pinned to the inventory."""
    rows = {row['field_name']: row
            for row in json.loads(FIELD_CURATION_FIXTURE.read_text())['mappings']}
    assert set(migration.LAB_FIELD_CONCEPTS) <= LAB_FIELDS
    for field in sorted(LAB_FIELDS):
        row = rows[field]
        assert row['status'] == 'approved', field
        assert row['vocabulary_id'] == 'LOINC', field
        assert row['omop_table'].lower() == 'measurement', field
        assert _LOINC_LAB_FIELDS[row['concept_code']][0] == field, field
    for field, pair in migration.LAB_FIELD_CONCEPTS.items():
        assert pair == (rows[field]['concept_code'], rows[field]['concept_id']), field


def test_seeded_tables_route_every_table_code_to_the_same_field(lab_field_mappings):
    """Once the tables are seeded they route every real code the table lists,
    and the two demo-only codes (no mapping row can carry them) still come
    from the table, so the routing is the table's content exactly."""
    assert lab_field_mappings == _LOINC_LAB_FIELDS
    assert _DEMO_ONLY_LAB_CODES <= set(lab_field_mappings)
    approved = FieldConceptMapping.objects.filter(
        field_name__in=LAB_FIELDS, status='approved', concept__isnull=False,
    ).count()
    assert approved == len(LAB_FIELDS)


def test_table_answers_for_a_field_with_no_resolved_mapping():
    """A fresh install before Athena loads: 0240 seeded the row with no concept."""
    FieldConceptMapping.objects.create(
        field_name='hemoglobin_g_dl', status='approved', vocabulary_id='LOINC',
        concept_code='718-7', omop_table='measurement',
    )
    assert resolve_lab_code_fields()['718-7'] == ('hemoglobin_g_dl', float)


def test_no_curation_query_without_a_loinc_lab(django_assert_num_queries):
    """Unrelated measurements must not raise the full-refresh query budget (#541)."""
    with django_assert_num_queries(0):
        routing = resolve_lab_code_fields(measurements=[])
    assert routing == _LOINC_LAB_FIELDS


def test_demo_bundle_completion_needs_no_database(django_assert_num_queries):
    """generate_fhir_bundle runs before import, often with no database at all
    (AGENTS.md invokes it with DEBUG=True and no DATABASE_URL)."""
    from omop_core.services.sample_disease_profiles import complete_demo_fhir_bundle
    bundle = {'entry': [
        {'resource': {'resourceType': 'Patient', 'id': 'p1', 'birthDate': '1960-01-01'}},
        {'resource': {'resourceType': 'Observation', 'subject': {'reference': 'Patient/p1'},
                      'code': {'coding': [{'code': '33944-8'}]}, 'valueQuantity': {'value': 1000}}},
    ]}
    with django_assert_num_queries(0):
        complete_demo_fhir_bundle(bundle, 'MM')


# ── The tables are the authority ────────────────────────────────────────────

def test_curator_code_change_moves_the_field_off_the_table_code(lab_field_mappings):
    person = PersonFactory()
    mapping = FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl')
    arterial = ConceptFactory(concept_code='30313-1')
    mapping.concept = arterial
    mapping.concept_code = mapping.source_value = '30313-1'
    mapping.save()
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='718-7'),
        measurement_source_value='718-7', measurement_date=date(2024, 2, 1),
        value_as_number=13.1,
    )
    assert refresh_patient_record(person).hemoglobin_g_dl is None
    MeasurementFactory(
        person=person, measurement_concept=arterial, measurement_source_value='30313-1',
        measurement_date=date(2024, 1, 1), value_as_number=12.2,
    )
    record = refresh_patient_record(person)
    record.refresh_from_db()
    assert record.hemoglobin_g_dl == Decimal('12.2')


def test_curator_alias_feeds_the_blood_count_selection(lab_field_mappings):
    """An approved alias LOINC counts as the field's code in
    _latest_blood_count_measurements, so it is converted by unit (#2282)."""
    person = PersonFactory()
    _approve_alias('26474-7', 'alc_thousand_per_ul')
    row = MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='26474-7'),
        measurement_source_value='26474-7', value_as_number=1500,
        unit_source_value='cells/uL',
    )
    selected = _latest_blood_count_measurements(_build_snapshot(person))
    assert selected['alc_thousand_per_ul'].pk == row.pk
    record = refresh_patient_record(person)
    record.refresh_from_db()
    assert record.alc_thousand_per_ul == Decimal('1.5')
    assert record.absolute_lymphocyte_count == Decimal('1500')


def test_demo_flc_codes_still_form_the_pair_once_tables_are_seeded(lab_field_mappings):
    """Staging's synthea-mm cohort carries FLC only under the generator's codes."""
    person = PersonFactory()
    for code, value in (('33944-8', 1000), ('33945-5', 5)):
        MeasurementFactory(
            person=person, measurement_concept=ConceptFactory(concept_code=code),
            measurement_source_value=code, measurement_date=date(2024, 3, 1),
            value_as_number=value, unit_source_value='mg/L',
        )
    assert _get_mm_specific_data(person)['meets_slim'] is True
    record = refresh_patient_record(person)
    record.refresh_from_db()
    assert record.kappa_flc == Decimal('1000')
    assert record.lambda_flc == Decimal('5')


def test_slim_scan_reads_an_alias_code_for_plasma_cells(lab_field_mappings):
    person = PersonFactory()
    _approve_alias('99999-9', 'clonal_plasma_cells')
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='99999-9'),
        measurement_source_value='99999-9', value_as_number=65, unit_source_value='%',
    )
    assert _get_mm_specific_data(person)['meets_slim'] is True


def test_provenance_registry_lists_a_curator_alias(lab_field_mappings):
    _approve_alias('26474-7', 'alc_thousand_per_ul')
    entry = get_registry()['alc_thousand_per_ul']
    assert '26474-7' in entry.concept_codes
    assert '731-0' in entry.concept_codes


def test_one_refresh_resolves_the_routing_once(lab_field_mappings):
    """The lab, MM and blood-count readers share the snapshot's memo."""
    from django.test.utils import CaptureQueriesContext
    person = PersonFactory()
    for code, value in (('718-7', 13.0), ('751-8', 2.1), ('93021-4', 12)):
        MeasurementFactory(
            person=person, measurement_concept=ConceptFactory(concept_code=code),
            measurement_source_value=code, value_as_number=value,
        )
    with CaptureQueriesContext(connection) as ctx:
        refresh_patient_record(person)
    curation = [q['sql'] for q in ctx.captured_queries
                if 'FROM "field_concept_mapping"' in q['sql']
                and '"field_concept_mapping"."concept_id" IS NOT NULL' in q['sql']]
    assert len(curation) == 1, curation


# ── Migration 0326 ──────────────────────────────────────────────────────────

def _migrate():
    run_data_migration('0326_approve_lab_field_mappings', 'approve_lab_field_mappings')


def test_migration_creates_missing_rows_and_approves_the_untouched_proposal():
    FieldConceptMapping.objects.create(
        field_name='creatinine_clearance_ml_min', status='proposed',
        vocabulary_id='LOINC', concept_code='2164-2', omop_table='Measurement',
        notes='Auto-proposed from field suggestion.',
    )
    ConceptFactory(concept_id=3000963, concept_code='718-7')
    _migrate()
    _migrate()
    crcl = FieldConceptMapping.objects.get(field_name='creatinine_clearance_ml_min')
    assert crcl.status == 'approved'
    assert crcl.provenance == migration.PROVENANCE
    assert crcl.notes.startswith('Auto-proposed from field suggestion. Approved')
    assert crcl.concept_id is None  # 2164-2 is not loaded here
    hgb = FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl')
    assert (hgb.status, hgb.concept_id, hgb.concept_code, hgb.source_value,
            hgb.omop_table, hgb.value_kind) == (
        'approved', 3000963, '718-7', '718-7', 'measurement', 'number')
    assert FieldConceptMapping.objects.filter(
        field_name__in=LAB_FIELDS, status='approved',
    ).count() == len(LAB_FIELDS)
    assert resolve_lab_code_fields()['718-7'] == ('hemoglobin_g_dl', float)


def test_migration_leaves_curator_decisions_alone():
    reviewer = get_user_model().objects.create_user(email='curator@example.com')
    rejected = FieldConceptMapping.objects.create(
        field_name='psa_ng_ml', status='rejected', vocabulary_id='LOINC',
        concept_code='2857-1', omop_table='measurement',
    )
    recoded = FieldConceptMapping.objects.create(
        field_name='esr', status='proposed', vocabulary_id='LOINC',
        concept_code='4537-7', omop_table='measurement',
    )
    reviewed = FieldConceptMapping.objects.create(
        field_name='inr', status='proposed', vocabulary_id='LOINC',
        concept_code='6301-6', omop_table='measurement', reviewer=reviewer,
    )
    other = FieldConceptMapping.objects.create(
        field_name='ldh_u_l', status='approved', vocabulary_id='LOINC',
        concept_code='14804-9', omop_table='measurement', source_value='14804-9',
    )
    _migrate()
    for row, status, code in ((rejected, 'rejected', '2857-1'),
                              (recoded, 'proposed', '4537-7'),
                              (reviewed, 'proposed', '6301-6'),
                              (other, 'approved', '14804-9')):
        row.refresh_from_db()
        assert (row.status, row.concept_code, row.provenance) == (status, code, '')


def test_migration_keys_a_new_row_on_its_code_only_when_free():
    FieldConceptMapping.objects.create(
        field_name='some_other_field', status='approved', vocabulary_id='LOINC',
        concept_code='718-7', omop_table='measurement', source_value='718-7',
    )
    _migrate()
    hgb = FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl')
    assert (hgb.status, hgb.concept_code, hgb.source_value) == ('approved', '718-7', '')


def test_migration_drops_a_colliding_source_value_when_approving():
    FieldConceptMapping.objects.create(
        field_name='some_other_field', status='approved', vocabulary_id='LOINC',
        concept_code='2164-2', omop_table='measurement', source_value='2164-2',
    )
    FieldConceptMapping.objects.create(
        field_name='creatinine_clearance_ml_min', status='proposed', vocabulary_id='LOINC',
        concept_code='2164-2', omop_table='measurement', source_value='2164-2',
    )
    _migrate()
    crcl = FieldConceptMapping.objects.get(field_name='creatinine_clearance_ml_min')
    assert (crcl.status, crcl.source_value) == ('approved', '')
