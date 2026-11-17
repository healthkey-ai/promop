"""The Wearables tab as a downloadable file: who may fetch it, which rows it
holds and in what order, and what the CSV and the Word document look like."""
import csv
import datetime
import io
import zipfile

import pytest
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.oxml.ns import qn
from rest_framework.test import APIClient

from omop_core.models import GroupAccess, Organization, SourceCodeConceptMapping
from omop_core.services import wearable_mapping_export as export
from omop_core.services.source_vocabularies import WEARABLE_SOURCE_VOCABULARIES
from patient_portal.models import Identity
from tests.factories import ConceptFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

URL = '/api/v1/code-mappings/wearables-export/'
LONG_IDENTIFIER = 'HKQuantityTypeIdentifierWalkingDoubleSupportPercentage'
ZERO_WIDTH = '\u200b'


def with_role(role):
    user = Identity.objects.create_user(email=f'wearable-export-{role}@example.test')
    org, _ = Organization.objects.get_or_create(
        slug='wearable-export-org', defaults={'name': 'Wearable Export Org'})
    GroupAccess.objects.create(identity=user, org=org, role=role)
    return user


def client_for(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user)
    return client


@pytest.fixture
def reader():
    """A doctor: reads every mapping page, writes none."""
    return client_for(with_role('doctor'))


def row(code, **kwargs):
    return SourceCodeConceptMapping.objects.create(source_code=code, **{
        'source_vocabulary_id': 'OpenWearables', 'status': 'proposed',
        'domain_id': 'Measurement', 'omop_table': 'measurement', **kwargs})


def loinc(code, name):
    return ConceptFactory(concept_code=code, concept_name=name)


def ucum(code, name):
    return ConceptFactory(
        concept_code=code, concept_name=name,
        vocabulary=VocabularyFactory(vocabulary_id='UCUM', vocabulary_name='UCUM'))


def csv_rows(client, **params):
    response = client.get(URL, {'file_format': 'csv', **params})
    assert response.status_code == 200, response.content
    lines = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
    return lines[0], [dict(zip(lines[0], line)) for line in lines[1:]]


def document(client):
    response = client.get(URL, {'file_format': 'docx'})
    assert response.status_code == 200, response.content
    return Document(io.BytesIO(response.content))


def plain(text):
    return text.replace(ZERO_WIDTH, '')


def headings(doc):
    return [p.text for p in doc.paragraphs if p.style.name == 'Heading 1']


def table_rows(table):
    """A table's body as lists of cell text, without wrap points."""
    return [[plain(cell.text) for cell in line.cells] for line in table.rows[1:]]


# ── Access ──────────────────────────────────────────────────────────────

def test_an_unauthenticated_request_is_refused():
    assert client_for().get(URL, {'file_format': 'csv'}).status_code in (401, 403)


def test_a_patient_is_refused():
    response = client_for(with_role('patient')).get(URL, {'file_format': 'csv'})
    assert response.status_code == 403
    assert response['Content-Type'].startswith('application/json')


@pytest.mark.parametrize('role', ['doctor', 'analyst', 'org_admin'])
@pytest.mark.parametrize('file_format', ['csv', 'docx'])
def test_every_role_that_reads_the_mapping_pages_may_download(role, file_format):
    row('steps')
    response = client_for(with_role(role)).get(URL, {'file_format': file_format})
    assert response.status_code == 200
    assert response['Content-Type'] == export.CONTENT_TYPES[file_format]


def test_only_get_is_allowed(reader):
    assert reader.post(URL, {'file_format': 'csv'}).status_code == 405


# ── The request ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('file_format', ['xlsx', 'CSV', 'pdf', 'csv,docx'])
def test_an_unknown_file_format_is_a_400(reader, file_format):
    response = reader.get(URL, {'file_format': file_format})
    assert response.status_code == 400
    assert 'Use csv or docx' in response.json()['detail']


def test_the_file_format_defaults_to_csv(reader):
    response = reader.get(URL)
    assert response.status_code == 200
    assert response['Content-Type'] == 'text/csv; charset=utf-8'


def test_the_parameter_cannot_be_called_format(reader):
    """Why it is ``file_format``: DRF reads ``format`` itself, before the view,
    and has no renderer by either name."""
    assert reader.get(URL, {'format': 'docx'}).status_code == 404
    assert reader.get(URL, {'format': 'csv'}).status_code == 404


@pytest.mark.parametrize('file_format', ['csv', 'docx'])
def test_the_file_is_an_uncached_attachment_named_for_the_day(reader, file_format, monkeypatch):
    monkeypatch.setattr(export, 'localdate', lambda: datetime.date(2026, 3, 9))
    response = reader.get(URL, {'file_format': file_format})
    assert response['Content-Disposition'] == (
        f'attachment; filename="wearable-mappings-2026-03-09.{file_format}"')
    assert response['Cache-Control'] == 'no-store'


# ── Which rows, in what order ───────────────────────────────────────────

def test_the_csv_header_names_the_columns(reader):
    header, rows = csv_rows(reader)
    assert header == [
        'Category', 'Measure class', 'Metric key', 'Source', 'Source code', 'Description',
        'Source unit', 'Destination vocabulary', 'Destination code', 'Destination concept',
        'OMOP concept ID', 'Domain', 'Destination unit', 'Status', 'Provenance',
    ]
    assert rows == []


def test_it_holds_every_wearable_vocabulary_and_no_other(reader):
    for vocabulary in ('OpenWearables', 'Apple', 'Garmin', 'HL7-PHR'):
        row(f'code-{vocabulary}', source_vocabulary_id=vocabulary)
    for vocabulary in ('LOINC', 'EPIC', 'SNOMED', ''):
        row(f'other-{vocabulary}', source_vocabulary_id=vocabulary, category='Sleep')
    _, rows = csv_rows(reader)
    assert sorted(r['Source code'] for r in rows) == [
        'code-Apple', 'code-Garmin', 'code-HL7-PHR', 'code-OpenWearables']
    assert {r['Source'] for r in rows} == {'OpenWearables', 'HealthKit', 'Garmin', 'HL7 PHR'}


def test_rejected_rows_are_left_out_and_every_other_status_and_section_is_in(reader):
    steps = loinc('55423-8', 'Number of steps')
    row('proposed-unmapped')
    row('proposed-mapped', target_concept=steps)
    row('approved', target_concept=steps, status='approved')
    row('athena', target_concept=steps, status='approved', origin_system='athena')
    row('rejected', target_concept=steps, status='rejected')
    _, rows = csv_rows(reader)
    assert sorted(r['Source code'] for r in rows) == [
        'approved', 'athena', 'proposed-mapped', 'proposed-unmapped']


def test_a_hospitals_rows_are_in_it_as_on_the_tab(reader):
    hospital = Organization.objects.create(slug='export-hospital', name='Export Hospital')
    row('steps', organization=hospital)
    row('steps')
    _, rows = csv_rows(reader)
    assert [r['Source code'] for r in rows] == ['steps', 'steps']


def test_rows_run_by_category_then_metric_then_source_then_code(reader):
    # Created out of order on every key.
    row('zz', category='', metric_key='steps')
    row('gamma', category='Sleep', metric_key='sleep_score', source_vocabulary_id='Garmin')
    row('hk-b', category='Activity', metric_key='steps', source_vocabulary_id='Apple')
    row('ow', category='Activity', metric_key='steps')
    row('phr', category='Activity', metric_key='steps', source_vocabulary_id='HL7-PHR')
    row('hc', category='Activity', metric_key='steps', source_vocabulary_id='HealthConnect')
    row('gar', category='Activity', metric_key='steps', source_vocabulary_id='Garmin')
    row('hk-a', category='Activity', metric_key='steps', source_vocabulary_id='Apple')
    row('nokey', category='Activity', metric_key='')
    row('dist', category='Activity', metric_key='distance')
    row('vo2', category='Fitness', metric_key='vo2_max')
    row('garmin-only', category='Garmin-specific', metric_key='body_battery',
        source_vocabulary_id='Garmin')
    # Health Connect joins the tab in another change; its place is fixed here.
    health_connect = (
        [('Activity', 'steps', 'Health Connect', 'hc')]
        if 'HealthConnect' in WEARABLE_SOURCE_VOCABULARIES else [])
    _, rows = csv_rows(reader)
    assert [(r['Category'], r['Metric key'], r['Source'], r['Source code']) for r in rows] == [
        ('Activity', 'distance', 'OpenWearables', 'dist'),
        ('Activity', 'steps', 'HealthKit', 'hk-a'),
        ('Activity', 'steps', 'HealthKit', 'hk-b'),
        *health_connect,
        ('Activity', 'steps', 'Garmin', 'gar'),
        ('Activity', 'steps', 'OpenWearables', 'ow'),
        ('Activity', 'steps', 'HL7 PHR', 'phr'),
        ('Activity', '', 'OpenWearables', 'nokey'),
        ('Fitness', 'vo2_max', 'OpenWearables', 'vo2'),
        ('Sleep', 'sleep_score', 'Garmin', 'gamma'),
        ('Garmin-specific', 'body_battery', 'Garmin', 'garmin-only'),
        ('', 'steps', 'OpenWearables', 'zz'),
    ]


def test_the_source_order_puts_health_connect_after_healthkit_and_strangers_last():
    """The order itself, whichever vocabularies the tab holds today."""
    def at(vocabulary):
        return export.Row(
            pk=1, category='Activity', measure_class='', metric_key='steps',
            vocabulary=vocabulary, source_code='x', description='', source_units=(),
            concept_id=None, destination_vocabulary='', destination_code='',
            destination_name='', domain='', destination_unit='', status='proposed',
            provenance='')
    shuffled = ['Zebra', 'HL7-PHR', 'OpenWearables', 'Acme', 'Garmin', 'HealthConnect', 'Apple']
    ordered = sorted((at(v) for v in shuffled), key=lambda r: r.sort_key)
    assert [r.source for r in ordered] == [
        'HealthKit', 'Health Connect', 'Garmin', 'OpenWearables', 'HL7 PHR', 'Acme', 'Zebra']


# ── CSV cells ───────────────────────────────────────────────────────────

def test_a_mapped_row_carries_its_destination_units_status_and_provenance(reader):
    steps = loinc('55423-8', 'Number of steps in 24 hour Measured')
    row('HKQuantityTypeIdentifierStepCount', source_vocabulary_id='Apple',
        source_code_description='Step count', category='Activity',
        measure_class='Aggregate over interval', metric_key='steps',
        target_concept=steps, destination_vocabulary_id='LOINC',
        destination_unit_concept=ucum('{steps}', 'steps'), status='approved',
        origin_system='wearable-device-seed',
        source_unit_evidence=[
            {'code': 'count', 'display': 'count'}, {'code': '{steps}'},
            {'code': 'count'}, {'display': 'steps/day'}, 'junk'],
        notes='Suggest rationale that stays in the app')
    header, (line,) = csv_rows(reader)
    assert line == {
        'Category': 'Activity', 'Measure class': 'Aggregate over interval',
        'Metric key': 'steps', 'Source': 'HealthKit',
        'Source code': 'HKQuantityTypeIdentifierStepCount', 'Description': 'Step count',
        'Source unit': 'count; steps/day; {steps}', 'Destination vocabulary': 'LOINC',
        'Destination code': '55423-8',
        'Destination concept': 'Number of steps in 24 hour Measured',
        'OMOP concept ID': str(steps.concept_id), 'Domain': 'Measurement',
        'Destination unit': '{steps}', 'Status': 'approved',
        'Provenance': 'wearable-device-seed',
    }
    assert 'rationale' not in ','.join(header)


def test_an_unmapped_row_has_blank_destination_columns(reader):
    row('sleep_score', domain_id='Observation', omop_table='observation',
        source_unit_evidence=[{'code': '{score}'}])
    row('zero', target_concept_id=0)
    _, rows = csv_rows(reader)
    for line in rows:
        assert [line[name] for name in (
            'Destination vocabulary', 'Destination code', 'Destination concept',
            'OMOP concept ID', 'Destination unit')] == [''] * 5
    assert rows[0]['Source unit'] == '{score}'
    # Until a destination names one, the domain is the one the code is filed under.
    assert rows[0]['Domain'] == 'Observation'


def test_a_destination_that_is_not_loaded_keeps_its_id(reader):
    row('orphan', target_concept_id=987654321, destination_vocabulary_id='LOINC')
    _, (line,) = csv_rows(reader)
    assert line['OMOP concept ID'] == '987654321'
    assert line['Destination vocabulary'] == 'LOINC'
    assert (line['Destination code'], line['Destination concept']) == ('', '')


@pytest.mark.parametrize('risky', ['=1+1', '+SUM(A1)', '-2+3', '@cmd', '\tx', '\rx'])
def test_text_a_spreadsheet_would_run_is_neutralised(reader, risky):
    row(risky, source_code_description=risky)
    _, (line,) = csv_rows(reader)
    assert line['Source code'] == "'" + risky
    assert line['Description'] == "'" + risky


def test_commas_quotes_and_newlines_survive_the_round_trip(reader):
    description = 'Heart rate, "resting"\nper minute'
    row('resting_hr', source_code_description=description)
    response = reader.get(URL, {'file_format': 'csv'})
    text = response.content.decode('utf-8')
    assert '"Heart rate, ""resting""\nper minute"' in text
    # A byte order mark, so Excel reads "°C" as UTF-8; none inside the file.
    assert text.startswith('\ufeff') and text.count('\ufeff') == 1 and '\r\n' not in text
    _, (line,) = csv_rows(reader)
    assert line['Description'] == description


def test_the_csv_never_carries_a_wrap_point(reader):
    row(LONG_IDENTIFIER, source_vocabulary_id='Apple')
    response = reader.get(URL, {'file_format': 'csv'})
    assert ZERO_WIDTH not in response.content.decode('utf-8')
    assert LONG_IDENTIFIER in response.content.decode('utf-8')


# ── The Word document ───────────────────────────────────────────────────

@pytest.fixture
def tab():
    """A small tab: three categories out of order, one metric across sources."""
    steps = loinc('55423-8', 'Number of steps in 24 hour Measured')
    double_support = loinc('LP-DS', 'Walking double support')
    row('steps', category='Activity', metric_key='steps', measure_class='Aggregate over interval',
        target_concept=steps, destination_vocabulary_id='LOINC', status='approved',
        destination_unit_concept=ucum('{steps}', 'steps'))
    row('HKQuantityTypeIdentifierStepCount', source_vocabulary_id='Apple',
        category='Activity', metric_key='steps', measure_class='Aggregate over interval',
        target_concept=steps, destination_vocabulary_id='LOINC', status='approved',
        source_unit_evidence=[{'code': 'count'}])
    row('TotalSteps', source_vocabulary_id='Garmin', category='Activity', metric_key='steps',
        measure_class='Direct measurement')
    row('active_minutes', category='Activity', metric_key='active_minutes')
    row(LONG_IDENTIFIER, source_vocabulary_id='Apple', category='Mobility & Gait',
        metric_key='walking_double_support_pct', measure_class='Algorithmic estimate',
        target_concept=double_support, destination_vocabulary_id='LOINC',
        source_unit_evidence=[{'code': '%'}])
    row('sleep_score', category='Sleep', metric_key='sleep_score', measure_class='Vendor score')
    row('mystery', source_vocabulary_id='HL7-PHR')
    row('gone', category='Sleep', status='rejected')
    row('8867-4', source_vocabulary_id='LOINC', category='Sleep')


def test_the_document_is_landscape_letter_with_a_title_counts_and_legend(reader, tab, monkeypatch):
    monkeypatch.setattr(export, 'localdate', lambda: datetime.date(2026, 3, 9))
    doc = document(reader)
    page = doc.sections[0]
    assert page.orientation == WD_ORIENT.LANDSCAPE
    assert (round(page.page_width.inches, 2), round(page.page_height.inches, 2)) == (11.0, 8.5)
    title, counts, legend = doc.paragraphs[:3]
    assert (title.style.name, title.text) == ('Title', 'Wearable metric mappings')
    assert counts.text == (
        'Generated 2026-03-09. 7 mappings across 4 metrics: 3 mapped to a concept, '
        '4 not yet mapped; 2 approved, 5 proposed.')
    assert 'has not been approved yet' in legend.text
    assert 'Not mapped' in legend.text
    assert 'copy exact codes from the CSV file' in legend.text


def test_headings_are_the_categories_present_in_order_with_counts(reader, tab):
    doc = document(reader)
    assert headings(doc) == [
        'Activity (4)', 'Sleep (1)', 'Mobility & Gait (1)', 'Uncategorized (1)']
    assert len(doc.tables) == 4


def test_each_category_has_one_table_of_its_rows(reader, tab):
    activity, sleep, gait, uncategorized = document(reader).tables
    for table in (activity, sleep, gait, uncategorized):
        assert [cell.text for cell in table.rows[0].cells] == [
            'Metric', 'Source', 'Source code', 'Destination', 'Unit', 'Status']
    assert table_rows(activity) == [
        ['active_minutes', 'OpenWearables', 'active_minutes', 'Not mapped', '', 'Proposed'],
        ['steps\nAggregate over interval / Direct measurement', 'HealthKit',
         'HKQuantityTypeIdentifierStepCount',
         'LOINC:55423-8\nNumber of steps in 24 hour Measured', 'count', 'Approved'],
        # The metric is named once; its other rows leave the cell empty.
        ['', 'Garmin', 'TotalSteps', 'Not mapped', '', 'Proposed'],
        ['', 'OpenWearables', 'steps',
         'LOINC:55423-8\nNumber of steps in 24 hour Measured', '{steps}', 'Approved'],
    ]
    assert table_rows(sleep) == [
        ['sleep_score\nVendor score', 'OpenWearables', 'sleep_score', 'Not mapped', '', 'Proposed']]
    assert table_rows(uncategorized) == [
        ['–', 'HL7 PHR', 'mystery', 'Not mapped', '', 'Proposed']]


def test_one_metrics_rows_read_as_one_cell_and_stay_on_one_page(reader, tab):
    """No rule between a metric's Metric cells, and no page break between its rows."""
    activity = document(reader).tables[0]
    open_edges, kept = [], []
    for line in activity.rows[1:]:
        first = line._tr.tc_lst[0]
        assert first.vMerge is None
        borders = first.tcPr.find(qn('w:tcBorders'))
        open_edges.append([] if borders is None else [
            edge.tag.split('}')[1] for edge in borders if edge.get(qn('w:val')) == 'nil'])
        kept.append({paragraph.paragraph_format.keep_with_next
                     for cell in line.cells for paragraph in cell.paragraphs})
    # active_minutes alone, then the three rows of steps.
    assert open_edges == [[], ['bottom'], ['top', 'bottom'], ['top']]
    assert kept == [{None}, {True}, {True}, {None}]


def test_every_cell_paragraph_is_set_tight(reader, tab):
    """An empty cell left at the document's spacing makes its row a line taller."""
    for table in document(reader).tables:
        for line in table.rows:
            for cell in line.cells:
                for paragraph in cell.paragraphs:
                    assert paragraph.paragraph_format.space_after == 0
                    assert paragraph.paragraph_format.line_spacing == 1.0


def test_a_long_identifier_is_whole_and_can_wrap(reader, tab):
    gait = document(reader).tables[2]
    cell = gait.rows[1].cells[2].text
    assert plain(cell) == LONG_IDENTIFIER
    assert cell.split(ZERO_WIDTH) == [
        'HKQuantity', 'Type', 'Identifier', 'Walking', 'Double', 'Support', 'Percentage']
    assert table_rows(gait) == [[
        'walking_double_support_pct\nAlgorithmic estimate', 'HealthKit', LONG_IDENTIFIER,
        'LOINC:LP-DS\nWalking double support', '%', 'Proposed']]


def test_short_text_gets_no_wrap_point():
    assert export.wrappable('steps') == 'steps'
    assert export.wrappable('LOINC:55423-8') == 'LOINC:55423-8'
    assert export.wrappable('HKQuantityTypeIdentifierStepCount').count(ZERO_WIDTH) == 4


def test_tables_are_fixed_width_repeat_their_header_and_keep_rows_whole(reader, tab):
    for table in document(reader).tables:
        properties = table._tbl.tblPr
        assert properties.find(qn('w:tblLayout')).get(qn('w:type')) == 'fixed'
        assert properties.find(qn('w:tblW')).get(qn('w:w')) == str(round(9.5 * 1440))
        assert [round(column.width.inches, 2) for column in table.columns] == [
            1.7, 0.95, 2.55, 2.6, 0.8, 0.9]
        header, *body = table.rows
        assert header._tr.trPr.find(qn('w:tblHeader')) is not None
        assert header.cells[0]._tc.tcPr.find(qn('w:shd')) is not None
        assert all(run.bold for cell in header.cells for run in cell.paragraphs[0].runs)
        for line in table.rows:
            assert line._tr.trPr.find(qn('w:cantSplit')) is not None
            assert [round(tc.width.inches, 2) for tc in line._tr.tc_lst] == [
                1.7, 0.95, 2.55, 2.6, 0.8, 0.9]


def test_the_footer_numbers_the_pages(reader, tab):
    footer = document(reader).sections[0].footer._element
    fields = [field.get(qn('w:instr')) for field in footer.iter(qn('w:fldSimple'))]
    assert fields == ['PAGE', 'NUMPAGES']
    assert ''.join(text.text for text in footer.iter(qn('w:t'))) == 'Page 1 of 1'


def test_the_document_names_no_person_and_is_the_same_bytes_on_the_same_day(reader, tab, monkeypatch):
    monkeypatch.setattr(export, 'localdate', lambda: datetime.date(2026, 3, 9))
    first = reader.get(URL, {'file_format': 'docx'}).content
    second = reader.get(URL, {'file_format': 'docx'}).content
    assert first == second
    properties = Document(io.BytesIO(first)).core_properties
    assert (properties.title, properties.author, properties.last_modified_by) == (
        'Wearable metric mappings', 'promop', 'promop')
    assert properties.created == properties.modified == datetime.datetime(
        2026, 3, 9, tzinfo=datetime.timezone.utc)
    assert properties.comments == ''
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert {member.date_time for member in archive.infolist()} == {(2026, 3, 9, 0, 0, 0)}
        everything = b''.join(archive.read(name) for name in archive.namelist())
        # The template's preview image shows its own blank page, not this document.
        assert not [name for name in archive.namelist() if 'thumbnail' in name]
    assert b'wearable-export-doctor' not in everything


def test_a_destination_that_is_not_loaded_reads_as_its_id_in_the_document(reader):
    row('orphan', category='Sleep', metric_key='orphan', target_concept_id=987654321)
    (table,) = document(reader).tables
    assert table_rows(table)[0][3] == 'Concept 987654321\nNot loaded on this instance'


def test_an_unlisted_category_still_gets_its_own_table_before_uncategorized(reader):
    row('a', category='Sleep')
    row('b', category='Retired Category')
    row('c')
    assert headings(document(reader)) == ['Sleep (1)', 'Retired Category (1)', 'Uncategorized (1)']


# ── Edges ───────────────────────────────────────────────────────────────

def test_an_empty_tab_still_yields_valid_files(reader):
    row('8867-4', source_vocabulary_id='LOINC')
    header, rows = csv_rows(reader)
    assert len(header) == 15 and rows == []
    doc = document(reader)
    assert doc.tables == [] and headings(doc) == []
    assert '0 mappings across 0 metrics' in doc.paragraphs[1].text
    assert doc.paragraphs[-1].text == 'There are no wearable mappings.'


@pytest.mark.parametrize('file_format', ['csv', 'docx'])
def test_the_query_count_does_not_grow_with_the_rows(reader, file_format, django_assert_num_queries):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    steps = loinc('55423-8', 'Number of steps')
    unit = ucum('{steps}', 'steps')
    hospital = Organization.objects.create(slug='export-count', name='Export Count')

    def add(count, prefix):
        for index in range(count):
            row(f'{prefix}-{index}', category=('Activity', 'Sleep', '')[index % 3],
                metric_key=f'metric_{index % 4}', target_concept=steps if index % 2 else None,
                destination_unit_concept=unit if index % 2 else None,
                organization=hospital if index % 5 == 0 else None,
                source_vocabulary_id=('Apple', 'Garmin', 'OpenWearables', 'HL7-PHR')[index % 4])

    add(3, 'few')
    with CaptureQueriesContext(connection) as few:
        assert reader.get(URL, {'file_format': file_format}).status_code == 200
    add(40, 'many')
    with django_assert_num_queries(len(few)):
        assert reader.get(URL, {'file_format': file_format}).status_code == 200
    # The rows themselves are one query, joined to their concept and unit.
    reads = [q['sql'] for q in few if 'source_code_concept_mapping' in q['sql']]
    assert len(reads) == 1
