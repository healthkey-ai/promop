"""A lookup's Seen bump is provisional until the code's per-patient rows exist (#2123)."""
import pytest

from omop_core.mapping import axis_variants, code_resolution
from omop_core.mapping.code_resolution import _record_proposal
from omop_core.models import PatientSourceCode, SourceCodeConceptMapping
from tests.factories import OrganizationFactory, PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db

LAB = 'urn:oid:1.2.840.114350.1.13.412.2.7.5.737384.134'


def sighting(code='5090', organization=None, provisional=True, vocabulary=LAB):
    token = code_resolution._PROVISIONAL.set(provisional)
    try:
        return _record_proposal(source_vocabulary_id=vocabulary, source_code=code, source_text='beta2 microglob',
                                concept=None, omop_table='measurement', source_system='etl',
                                organization=organization)
    finally:
        code_resolution._PROVISIONAL.reset(token)


def rows(code='5090', n=40, person=None, vocabulary=LAB, table='measurement'):
    return PatientSourceCode.objects.create(person=person or PersonFactory(), source_value=code,
                                            source_vocabulary_id=vocabulary, omop_table=table, occurrence_count=n)


def test_provisional_sightings_count_while_no_patient_rows_exist_then_read_the_rows():
    first = sighting()
    assert first.occurrence_count == 1
    assert sighting().occurrence_count == 2, 'a lookup is the only count before any patient upload'
    rows(n=40)
    again = sighting()
    assert again.occurrence_count == 40, 'with patient rows on the instance a sighting reads them'
    assert again.last_seen is not None and again.last_seen > first.last_seen


def test_a_per_record_sighting_always_counts():
    """FHIR sync resolves once per record and writes no per-patient rows."""
    rows(n=40)
    assert sighting(provisional=False).occurrence_count == 1
    assert sighting(provisional=False).occurrence_count == 2


def test_a_mapping_created_after_its_patients_were_uploaded_starts_at_their_count():
    rows(n=25)
    rows(n=5)
    assert sighting().occurrence_count == 30


def test_rows_on_another_table_or_vocabulary_spelling_do_not_count_as_this_mapping_s():
    rows(n=40, table='observation')
    assert sighting().occurrence_count == 1 and sighting().occurrence_count == 2
    # A vocabulary the rows carry as an OID and the mapping holds under its canonical id.
    oid = 'urn:oid:2.16.840.1.113883.6.96'
    rows(code='271737000', n=12, vocabulary=oid)
    snomed = sighting(code='271737000', vocabulary='SNOMED')
    assert snomed.source_vocabulary_id == 'SNOMED'
    assert code_resolution._has_patient_rows(snomed), 'the OID rows answer for the canonical vocabulary'


def test_a_hospital_scoped_proposal_asks_about_its_own_patients():
    ours, theirs = OrganizationFactory(), OrganizationFactory()
    rows(n=9, person=PatientRecordFactory(organization=theirs).person)
    scoped = sighting(organization=ours)
    assert scoped.organization_id == ours.pk
    assert sighting(organization=ours).occurrence_count == 2, "another hospital's rows do not count for ours"
    rows(n=3, person=PatientRecordFactory(organization=ours).person)
    assert sighting(organization=ours).occurrence_count == 3


def test_a_limited_rebuild_touches_only_the_keys_given():
    a = SourceCodeConceptMapping.objects.create(source_vocabulary_id=LAB, source_code='A', omop_table='measurement',
                                                occurrence_count=100)
    b = SourceCodeConceptMapping.objects.create(source_vocabulary_id=LAB, source_code='B', omop_table='measurement',
                                                occurrence_count=100)
    for code in ('A', 'B'):
        rows(code=code, n=5)
    assert axis_variants.rebuild_seen_counts([(LAB, 'A')]) == 1
    a.refresh_from_db(); b.refresh_from_db()
    assert (a.occurrence_count, b.occurrence_count) == (5, 100)
    assert axis_variants.rebuild_seen_counts([]) == 0


@pytest.mark.parametrize('mapping_vocab', ['SNOMED', 'urn:oid:2.16.840.1.113883.6.96'])
def test_seen_sums_canonical_and_oid_rows_in_both_directions(mapping_vocab):
    for vocabulary, count in [('SNOMED', 5), ('urn:oid:2.16.840.1.113883.6.96', 12)]:
        rows(code='271737000', n=count, vocabulary=vocabulary)
    result = sighting(code='271737000', vocabulary=mapping_vocab)
    assert result.occurrence_count == 17
    assert sighting(code='271737000', vocabulary=mapping_vocab).occurrence_count == 17
    result.occurrence_count = 100
    result.save(update_fields=['occurrence_count'])
    axis_variants.rebuild_seen_counts([('SNOMED', '271737000')])
    result.refresh_from_db()
    assert result.occurrence_count == 17
    axis_variants.rebuild_unit_evidence([result])
    result.refresh_from_db()
    assert result.occurrence_count == 17, 'a subsequent unit-evidence rebuild must not undo alias totals'


def test_truncated_mapping_reads_the_same_patient_rows_for_existence_and_total():
    code = 'x' * code_resolution.SOURCE_CODE_MAX + '-long-source-name'
    rows(code=code, n=12)
    assert sighting(code=code).occurrence_count == 12
    assert sighting(code=code).occurrence_count == 12


def _rebuilt_count(mapping):
    mapping.occurrence_count = 1000
    mapping.save(update_fields=['occurrence_count'])
    axis_variants.rebuild_seen_counts()
    mapping.refresh_from_db()
    return mapping.occurrence_count


def _all_entry_points(mapping):
    """What each reader of the shared row match answers for ``mapping``:
    existence for a lookup, the set-based existence, and the rebuilt total."""
    from omop_core.models import AxisVariantRefresh
    AxisVariantRefresh.objects.get_or_create(rebuild_evidence=True, outcome='done')
    key = (mapping.source_vocabulary_id, mapping.source_code)
    return (code_resolution._has_patient_rows(mapping),
            key in axis_variants.rebuilt_seen_keys(),
            key in axis_variants.rebuilt_seen_keys([key]),
            _rebuilt_count(mapping))


def test_a_short_code_matches_its_rows_exactly_never_by_prefix():
    """Prefix matching is for a code the mapping holds cut at the limit; a
    shorter code is the whole code (#2492)."""
    mapping = SourceCodeConceptMapping.objects.create(source_vocabulary_id=LAB, source_code='AB',
                                                      omop_table='measurement')
    rows(code='ABC', n=7)
    assert _all_entry_points(mapping) == (False, False, False, 1000)
    rows(code='AB', n=4)
    assert _all_entry_points(mapping) == (True, True, True, 4)


def test_a_code_one_short_of_the_limit_is_still_whole():
    """The threshold is held twice (Python for one mapping, SQL for the set);
    one character under it, neither may read the code as cut (#2492)."""
    code = 'y' * (code_resolution.SOURCE_CODE_MAX - 2) + '%'
    mapping = SourceCodeConceptMapping.objects.create(source_vocabulary_id=LAB, source_code=code,
                                                      omop_table='measurement')
    rows(code=code + '-longer', n=7)
    assert _all_entry_points(mapping) == (False, False, False, 1000)


@pytest.mark.parametrize('scoped', [False, True])
def test_a_truncated_code_matches_the_full_code_and_every_longer_one(scoped):
    """The cut code answers for its exact rows and every row it is a prefix
    of, across vocabulary spellings, from every reader alike (#2492)."""
    limit = code_resolution.SOURCE_CODE_MAX
    cut = 'y' * (limit - 1) + '%'  # a LIKE metacharacter in the code is matched literally
    org = OrganizationFactory() if scoped else None
    person = (lambda: PatientRecordFactory(organization=org).person) if scoped else (lambda: None)
    mapping = SourceCodeConceptMapping.objects.create(source_vocabulary_id='SNOMED', source_code=cut,
                                                      omop_table='measurement', organization=org)
    rows(code=cut[:-1] + 'Z' + '-tail', n=50, person=person(), vocabulary='SNOMED')  # differs at the cut
    assert _all_entry_points(mapping) == (False, False, False, 1000)
    rows(code=cut, n=3, person=person(), vocabulary='SNOMED')
    rows(code=cut + '-one', n=5, person=person(), vocabulary='urn:oid:2.16.840.1.113883.6.96')
    rows(code=cut + '-two', n=11, person=person(), vocabulary='SNOMED', table='observation')
    assert _all_entry_points(mapping) == (True, True, True, 8)


def test_the_exact_match_is_answered_by_the_source_value_index():
    """An OR of exact and prefix on the outer row's code left the planner no
    index condition, so it read every patient row once per mapping (#2492).
    With sequential scans priced out the planner walks a whole index instead,
    so what is asserted is a condition on ``source_value`` itself: an index
    lookup per mapping, or for the whole set one hash join on the code."""
    import re
    from django.db import connection
    from django.db.models import Exists
    from django.test.utils import CaptureQueriesContext

    short = SourceCodeConceptMapping.objects.create(source_vocabulary_id=LAB, source_code='AB',
                                                    omop_table='measurement')
    cut = SourceCodeConceptMapping.objects.create(source_vocabulary_id=LAB, source_code='q' * 100,
                                                  omop_table='measurement')
    with CaptureQueriesContext(connection) as captured:
        code_resolution._has_patient_rows(short)
        code_resolution._has_patient_rows(cut)
    set_based = SourceCodeConceptMapping.objects.filter(
        axis_variants._truncated_mappings(False), Exists(axis_variants._patient_rows_for()))
    queries = [(q['sql'], None) for q in captured.captured_queries if 'patient_source_code' in q['sql']]
    queries.append(set_based.query.sql_with_params())
    assert len(queries) == 3
    with connection.cursor() as cursor:
        cursor.execute('SET LOCAL enable_seqscan = off')
        for sql, params in queries:
            cursor.execute('EXPLAIN ' + sql, params)
            plan = '\n'.join(row[0] for row in cursor.fetchall())
            assert re.search(r'(Index|Hash) Cond: .*source_value', plan), plan
