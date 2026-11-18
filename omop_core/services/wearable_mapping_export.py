"""The whole Wearables tab as one file: CSV, or a Word document by category.

The Code Mapping page's own Download menu writes the rows a section has
loaded. This is every row of the tab, whatever section or page it is on, for
handing to someone outside the app: the CSV for a spreadsheet, the Word
document for a standards working group to read.

Reference data only. Neither file carries a patient value, a reviewer or a
note: ``notes`` holds Suggest's rationale and curators' own text.
"""
import csv
import datetime
import io
import re
import zipfile
from dataclasses import dataclass, replace
from itertools import groupby

from django.utils.timezone import localdate

from omop_core.models import BiomedicalConcept, SourceCodeConceptMapping
from omop_core.services.disease_field_sets import csv_cell
from omop_core.services.mapping_browse import canonical_source, tab_queryset
from omop_core.services.mapping_source_units import SourceUnits
from omop_core.services.source_vocabularies import CDISC_BC_VOCABULARY, WEARABLE_METRIC_CATEGORIES

#: The tab every wearable vocabulary is shown on.
WEARABLES_TAB = canonical_source('OpenWearables')

#: What the file maps each code to: the OMOP concept it is stored as, or the
#: CDISC Biomedical Concept that is mapped to the same concept.
DEST_OMOP = 'omop'
DEST_CDISC_BC = 'cdisc-bc'
DESTINATIONS = (DEST_OMOP, DEST_CDISC_BC)

FORMAT_CSV = 'csv'
FORMAT_DOCX = 'docx'
CONTENT_TYPES = {
    FORMAT_CSV: 'text/csv; charset=utf-8',
    FORMAT_DOCX: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}

TITLE = 'Wearable metric mappings'
BC_TITLE = 'Wearable metric mappings to CDISC Biomedical Concepts'
UNCATEGORIZED = 'Uncategorized'
NOT_MAPPED = 'Not mapped'

# What a reader calls each source, in the order one metric's codes are listed:
# the two phone platforms, the device vendor, the aggregator, then HL7's own
# codes. A vocabulary that joins the tab later sorts after these under its id.
SOURCE_LABELS = {
    'Apple': 'HealthKit',
    'HealthConnect': 'Health Connect',
    'Garmin': 'Garmin',
    'OpenWearables': 'OpenWearables',
    'HL7-PHR': 'HL7 PHR',
    CDISC_BC_VOCABULARY: 'CDISC BC',
}
_SOURCE_ORDER = {vocabulary: index for index, vocabulary in enumerate(SOURCE_LABELS)}
_CATEGORY_ORDER = {category: index for index, category in enumerate(WEARABLE_METRIC_CATEGORIES)}

CSV_COLUMNS = (
    'Category', 'Measure class', 'Metric key', 'Source', 'Source code', 'Description',
    'Source unit', 'Destination vocabulary', 'Destination code', 'Destination concept',
    'OMOP concept ID', 'Domain', 'Destination unit', 'Status', 'Provenance',
)


#: The CSV for the CDISC BC destination: the code, the OMOP concept it maps to
#: (how the BC is reached), then the BC and its NCIt identity.
BC_CSV_COLUMNS = (
    'Category', 'Measure class', 'Metric key', 'Source', 'Source code', 'Description',
    'Source unit', 'OMOP vocabulary', 'OMOP code', 'OMOP concept', 'CDISC BC ID',
    'CDISC BC name', 'NCIt code', 'BC categories', 'BC result scales', 'BC definition',
    'BC mapping status', 'Code mapping status',
)


class FormatError(ValueError):
    """The caller asked for a file this export does not write."""


# The Word document is built in memory inside the request: about 1.4 ms and
# 35 KB a row, so 20,000 rows is 28 s and 900 MB against gunicorn's 30 s. The
# tab holds a few hundred. The CSV has no such limit (20,000 rows in 0.1 s).
DOCX_MAX_ROWS = 5000

# What XML 1.0 cannot hold, and python-docx therefore refuses to write: one
# such character in any stored code would fail the document for every reader.
_XML_ILLEGAL = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]')


def xml_safe(text):
    return _XML_ILLEGAL.sub('', text or '')


@dataclass(frozen=True)
class Row:
    """One mapping, as both files show it."""

    pk: int
    category: str
    measure_class: str
    metric_key: str
    vocabulary: str
    source_code: str
    description: str
    source_units: tuple
    concept_id: int | None
    destination_vocabulary: str
    destination_code: str
    destination_name: str
    domain: str
    destination_unit: str
    status: str
    provenance: str
    bc_id: str = ''
    bc_name: str = ''
    ncit_code: str = ''
    bc_categories: tuple = ()
    bc_scales: tuple = ()
    bc_definition: str = ''
    bc_status: str = ''

    @property
    def source(self):
        return SOURCE_LABELS.get(self.vocabulary, self.vocabulary)

    @property
    def mapped(self):
        """Whether the row names a destination, on the queue sections' rule."""
        return bool(self.concept_id)

    @property
    def sort_key(self):
        return (
            _blank_last(self.category, _CATEGORY_ORDER),
            (not self.metric_key, self.metric_key),
            (_SOURCE_ORDER.get(self.vocabulary, len(_SOURCE_ORDER)), self.vocabulary),
            self.source_code, self.pk,
        )


def _blank_last(value, order):
    """Listed values in list order, then unlisted ones by name, then blank."""
    return (not value, order.get(value, len(order)), value)


def export_rows(destination=DEST_OMOP):
    """Every mapping the Wearables tab can show, rejected ones aside, in file order.

    The tab's rows are the browse service's: every wearable vocabulary. A
    hospital's own override of a code is left out: the file is for use outside
    the app, where the hospital's row would read as a second, unexplained
    line for the same code. The list endpoint's ICD-10-CM Athena guard is not
    applied, since it can only remove ICD10CM rows. One query, whatever the
    row count.
    """
    rows = rows_from(tab_mappings().filter(organization__isnull=True).exclude(status='rejected'))
    return bc_rows(rows) if destination == DEST_CDISC_BC else rows


def bc_rows(rows):
    """The wearable codes of *rows* that reach a CDISC Biomedical Concept, a line per BC.

    A BC is a row of the tab itself (vocabulary ``CDISC-BC``, mapped to one
    OMOP concept), and a wearable code reaches the BCs that share its
    destination. So this is a view of the mappings above, with nothing stored
    about a code and its BC: the BC rows are not lines of their own, a code
    that shares a destination with no BC is left out, and the status shown is
    the BC mapping's. Approved BC mappings come before proposed. One query for
    the catalog, whatever the row count.
    """
    reached = {}
    for row in rows:
        if row.vocabulary == CDISC_BC_VOCABULARY and row.concept_id:
            reached.setdefault(row.concept_id, []).append(row)
    for held in reached.values():
        held.sort(key=lambda bc: (bc.status, bc.source_code))
    catalog = BiomedicalConcept.objects.in_bulk({bc.source_code for held in reached.values() for bc in held})
    lines = []
    for row in rows:
        if row.vocabulary == CDISC_BC_VOCABULARY:
            continue
        for mapped in reached.get(row.concept_id, ()):
            bc = catalog.get(mapped.source_code)
            lines.append(replace(
                row, bc_id=mapped.source_code,
                bc_name=bc.short_name if bc else mapped.description,
                ncit_code=bc.ncit_code if bc else '',
                bc_categories=tuple(bc.categories) if bc else (),
                bc_scales=tuple(bc.result_scales) if bc else (),
                bc_definition=bc.definition if bc else '', bc_status=mapped.status))
    return lines


def tab_mappings():
    """Every mapping on the Wearables tab, as a queryset to narrow."""
    return tab_queryset(SourceCodeConceptMapping.objects.all(), WEARABLES_TAB)


def rows_from(mappings):
    """*mappings* as ``Row`` values in file order. One query."""
    found = mappings.order_by().annotate(units=SourceUnits('source_unit_evidence')).values_list(
        'pk', 'category', 'measure_class', 'metric_key', 'source_vocabulary_id',
        'source_code', 'source_code_description', 'units', 'target_concept_id',
        'destination_vocabulary_id', 'target_concept__vocabulary_id',
        'target_concept__concept_code', 'target_concept__concept_name',
        'target_concept__domain_id', 'domain_id',
        'destination_unit_concept__concept_code', 'status', 'origin_system',
    )
    rows = []
    for (pk, category, measure_class, metric_key, vocabulary, code, description, units,
         concept_id, destination_vocabulary, concept_vocabulary, concept_code,
         concept_name, concept_domain, domain, unit, status, origin) in found:
        # The destination has no database constraint: an id whose concept is
        # not loaded here keeps its id and reads as a concept with no name.
        loaded = concept_id and concept_code is not None
        rows.append(Row(
            pk=pk, category=category, measure_class=measure_class, metric_key=metric_key,
            vocabulary=vocabulary, source_code=code, description=description,
            source_units=tuple(units or ()), concept_id=concept_id or None,
            destination_vocabulary=(destination_vocabulary or concept_vocabulary or '') if concept_id else '',
            destination_code=concept_code if loaded else '',
            destination_name=concept_name if loaded else '',
            # The destination's domain once there is one; until then the
            # domain the curator filed the code under.
            domain=(concept_domain if loaded else '') or domain,
            destination_unit=unit or '', status=status, provenance=origin,
        ))
    rows.sort(key=lambda row: row.sort_key)
    return rows


def export_file(file_format, day=None, destination=DEST_OMOP):
    """``(filename, content type, body)`` of the tab in *file_format*.

    *destination* is what each code is mapped to in the file: ``omop`` (the
    concept it is stored as) or ``cdisc-bc`` (the Biomedical Concept mapped
    to the same concept).
    """
    if file_format not in CONTENT_TYPES:
        raise FormatError(f"Unknown file format {file_format!r}. Use csv or docx.")
    if destination not in DESTINATIONS:
        raise FormatError(f"Unknown destination {destination!r}. Use omop or cdisc-bc.")
    day = day or localdate()
    rows = export_rows(destination)
    if file_format != FORMAT_CSV and len(rows) > DOCX_MAX_ROWS:
        raise FormatError(
            f'The Wearables tab holds {len(rows):,} mappings and the Word document is '
            f'limited to {DOCX_MAX_ROWS:,}. Download the CSV instead.')
    bc = destination == DEST_CDISC_BC
    body = csv_text(rows, bc) if file_format == FORMAT_CSV else docx_bytes(rows, day, bc)
    stem = 'wearable-cdisc-bc-mappings' if bc else 'wearable-mappings'
    return f'{stem}-{day.isoformat()}.{file_format}', CONTENT_TYPES[file_format], body


# ── CSV ──────────────────────────────────────────────────────────────────

def csv_text(rows, bc=False):
    """One line per mapping under a header row; UTF-8, ``\\n`` line ends.

    It opens with a byte order mark: descriptions carry "°C" and "μS", and
    Excel reads a UTF-8 file without one as Windows-1252 ("Â°C"). A source
    code or a description is whatever a producer sent, so text a spreadsheet
    would run as a formula is quoted (``csv_cell``).
    """
    buffer = io.StringIO()
    buffer.write('\ufeff')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(BC_CSV_COLUMNS if bc else CSV_COLUMNS)
    for row in rows:
        if bc:
            writer.writerow([csv_cell(value) for value in (
                row.category, row.measure_class, row.metric_key, row.source, row.source_code,
                row.description, '; '.join(row.source_units), row.destination_vocabulary,
                row.destination_code, row.destination_name, row.bc_id, row.bc_name,
                row.ncit_code, '; '.join(row.bc_categories), '; '.join(row.bc_scales),
                row.bc_definition, row.bc_status, row.status,
            )])
            continue
        writer.writerow([csv_cell(value) for value in (
            row.category, row.measure_class, row.metric_key, row.source, row.source_code,
            row.description, '; '.join(row.source_units), row.destination_vocabulary,
            row.destination_code, row.destination_name, row.concept_id,
            row.domain, row.destination_unit, row.status, row.provenance,
        )])
    return buffer.getvalue()


# ── Word ─────────────────────────────────────────────────────────────────

FONT = 'Calibri'
AUTHOR = 'promop'
_INK = '1F2937'
_MUTED = '6B7280'
_RULE = 'BFC5CD'
_HEADER_FILL = 'E8ECF1'
_BODY_PT = 9
_SMALL_PT = 8
#: Column headings and widths in inches. They sum to the 9.5in between the
#: margins of a landscape Letter page, and the table's layout is fixed, so a
#: long HealthKit identifier wraps inside Source code instead of widening it.
DOCX_COLUMNS = (
    ('Metric', 1.7), ('Source', 0.95), ('Source code', 2.55),
    ('Destination', 2.6), ('Unit', 0.8), ('Status', 0.9),
)
#: The CDISC BC document's columns, 9.5in in all like the above.
BC_DOCX_COLUMNS = (
    ('Metric', 1.6), ('Source', 0.9), ('Source code', 2.3),
    ('OMOP concept', 1.9), ('CDISC Biomedical Concept', 2.0), ('Status', 0.8),
)
BC_LEGEND = (
    'Each table lists the wearable source codes of one category that reach a CDISC '
    'Biomedical Concept (BC). Both a wearable code and a BC are mapped to an OMOP '
    'concept, and a code reaches the BC mapped to the same concept; that concept is '
    'shown between them. Several codes can reach one BC, and a code whose concept '
    'no BC is mapped to is not listed (steps and sleep, for example, have no CDISC '
    'BC). A BC is named with its NCIt code. Status is that of the BC mapping: '
    'Approved is in use, Proposed has not been approved yet and may still change. '
    'Long identifiers wrap at invisible break characters, so copy exact codes from '
    'the CSV file rather than from this document.'
)
LEGEND = (
    'Each table lists the source codes of one category. Metric is the vendor-neutral '
    'key shared by every source code that reports the same metric, with how the '
    'source produces the value beneath it. Source is the platform or vocabulary '
    'the code comes from, and Source code its identifier there. Destination is '
    'the OMOP concept the code is stored as, written vocabulary:code with the '
    f'concept name beneath; "{NOT_MAPPED}" means no concept has been chosen yet. '
    'Unit is the unit values are stored in; where none has been set, the unit '
    'the source reports is shown in grey. Status is Approved for a mapping in '
    'use; a Proposed mapping has not been approved yet and may still change. '
    'Long identifiers wrap at invisible break characters, so copy exact codes '
    'from the CSV file rather than from this document.'
)

# Where a long identifier may wrap: after a separator, and where a capital
# starts a new word (HKQuantityTypeIdentifier|Walking|Double|Support). The
# break is a zero-width space, which Word and LibreOffice both honour.
_ZWSP = '\u200b'
_WRAP_POINT = re.compile(r'(?<=[_./:-])(?=\w)|(?<=[a-z0-9])(?=[A-Z])')
_LONG_TOKEN = re.compile(r'\S{20,}')


def wrappable(text):
    """*text* with zero-width break points inside any token of 20 characters or more."""
    return _LONG_TOKEN.sub(lambda token: _WRAP_POINT.sub(_ZWSP, token.group()), text)


def sections(rows):
    """``[(heading, rows)]``: one per category that has rows, blank last."""
    return [
        (category or UNCATEGORIZED, list(members))
        for category, members in groupby(rows, key=lambda row: row.category)
    ]


def bc_summary(rows, day):
    """The BC document's line under the title."""
    codes = len({row.pk for row in rows})
    concepts = len({row.bc_id for row in rows})
    approved = len({row.bc_id for row in rows if row.bc_status == 'approved'})
    return (
        f'Generated {day.isoformat()}. {_count(codes, "wearable source code")} reaching '
        f'{_count(concepts, "CDISC Biomedical Concept")}: {approved} approved, '
        f'{concepts - approved} proposed.'
    )


def summary(rows, day):
    """The line under the title: the date and what the tables add up to."""
    metrics = len({row.metric_key for row in rows if row.metric_key})
    mapped = sum(row.mapped for row in rows)
    approved = sum(row.status == 'approved' for row in rows)
    return (
        f'Generated {day.isoformat()}. {_count(len(rows), "mapping")} across '
        f'{_count(metrics, "metric")}: {mapped} mapped to a concept, '
        f'{len(rows) - mapped} not yet mapped; {approved} approved, '
        f'{len(rows) - approved} proposed.'
    )


def _count(number, noun):
    return f'{number} {noun}' if number == 1 else f'{number} {noun}s'


def docx_bytes(rows, day, bc=False):
    """The tab as a Word document: a table per category, landscape Letter.

    The same rows on the same day give the same bytes: the order is the
    file order, the document properties are fixed strings, and the archive's
    member times are the day's.
    """
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.shared import Inches

    document = Document()
    _document_properties(document, day)
    _styles(document)

    page = document.sections[0]
    page.orientation = WD_ORIENT.LANDSCAPE
    page.page_width, page.page_height = Inches(11), Inches(8.5)
    page.left_margin = page.right_margin = Inches(0.75)
    page.top_margin = page.bottom_margin = Inches(0.75)
    _page_number_footer(page)

    document.add_heading(BC_TITLE if bc else TITLE, level=0)
    _text(document.add_paragraph(), (bc_summary if bc else summary)(rows, day), size=10, color=_MUTED)
    _text(document.add_paragraph(), BC_LEGEND if bc else LEGEND, size=10)
    if not rows:
        _text(document.add_paragraph(), 'No wearable code reaches a CDISC Biomedical Concept.' if bc
              else 'There are no wearable mappings.', size=10, italic=True)
    for heading, members in sections(rows):
        document.add_heading(f'{xml_safe(heading)} ({len(members)})', level=1)
        _category_table(document, members, bc)

    buffer = io.BytesIO()
    document.save(buffer)
    return _with_fixed_times(buffer.getvalue(), day)


def _document_properties(document, day):
    properties = document.core_properties
    stamp = datetime.datetime(day.year, day.month, day.day)
    properties.title = TITLE
    properties.subject = 'Wearable source codes and the OMOP concepts they map to'
    properties.author = properties.last_modified_by = AUTHOR
    properties.comments = properties.keywords = properties.category = ''
    properties.created = properties.modified = stamp
    properties.revision = 1
    # python-docx's template carries a preview image of its own blank page,
    # which a file browser would show for this document.
    relationships = document.part.package.rels
    for key in [key for key, rel in relationships.items() if rel.reltype.endswith('/thumbnail')]:
        del relationships[key]


def _styles(document):
    from docx.shared import Pt, RGBColor

    for name, size, bold in (('Normal', 10, False), ('Title', 20, True), ('Heading 1', 13, True)):
        style = document.styles[name]
        _font(style.element.get_or_add_rPr(), FONT)
        style.font.size = Pt(size)
        style.font.bold = bold
        style.font.color.rgb = RGBColor.from_string(_INK)
    document.styles['Heading 1'].paragraph_format.space_before = Pt(14)
    document.styles['Heading 1'].paragraph_format.space_after = Pt(4)
    document.styles['Heading 1'].paragraph_format.keep_with_next = True


def _font(rPr, name):
    """Name the font outright; a theme font on the style would win otherwise."""
    from docx.oxml.ns import qn

    fonts = rPr.get_or_add_rFonts()
    for attribute in ('ascii', 'hAnsi', 'cs', 'eastAsia'):
        fonts.set(qn(f'w:{attribute}'), name)
        # The attribute is asciiTheme / hAnsiTheme / eastAsiaTheme, but cstheme.
        for theme in (f'w:{attribute}Theme', f'w:{attribute}theme'):
            fonts.attrib.pop(qn(theme), None)


def _text(paragraph, text, *, size=_BODY_PT, bold=False, italic=False, color=None):
    from docx.shared import Pt, RGBColor

    run = paragraph.add_run(xml_safe(text))
    run.font.size = Pt(size)
    run.font.bold = bold or None
    run.font.italic = italic or None
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    return run


def _element(tag, **attributes):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    element = OxmlElement(f'w:{tag}')
    for name, value in attributes.items():
        element.set(qn(f'w:{name}'), str(value))
    return element


def _page_number_footer(page):
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    paragraph = page.footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _text(paragraph, 'Page ', size=_SMALL_PT, color=_MUTED)
    _field(paragraph, 'PAGE')
    _text(paragraph, ' of ', size=_SMALL_PT, color=_MUTED)
    _field(paragraph, 'NUMPAGES')


def _field(paragraph, instruction):
    """A field Word fills in when it lays the page out, shown as 1 until then."""
    run = _text(paragraph, '1', size=_SMALL_PT, color=_MUTED)
    field = _element('fldSimple', instr=instruction)
    run._r.addprevious(field)
    field.append(run._r)


def _category_table(document, members, bc=False):
    from docx.shared import Inches

    columns = BC_DOCX_COLUMNS if bc else DOCX_COLUMNS
    table = document.add_table(rows=1, cols=len(columns))
    _table_properties(table, columns)
    for column, (_, width) in zip(table.columns, columns):
        column.width = Inches(width)

    header = table.rows[0]
    _row_properties(header, header=True)
    for cell, (label, width) in zip(header.cells, columns):
        cell.width = Inches(width)
        cell._tc.get_or_add_tcPr().append(_element('shd', val='clear', color='auto', fill=_HEADER_FILL))
        _text(_first_paragraph(cell), label, bold=True)

    for _, metric_rows in groupby(members, key=lambda row: row.metric_key or f'\0{row.pk}'):
        metric_rows = list(metric_rows)
        for index, row in enumerate(metric_rows):
            line = table.add_row()
            _row_properties(line)
            cells = line.cells
            for cell in cells:
                # Every cell, the empty ones too: an untouched paragraph keeps
                # the document's spacing and makes its row a line taller.
                _first_paragraph(cell)
            last = index == len(metric_rows) - 1
            # One metric's rows read as one Metric cell: the text is written
            # once, on the first, and no rule is drawn between them.
            if index == 0:
                _metric_cell(cells[0], metric_rows)
            _open_edges(cells[0], top=index > 0, bottom=not last)
            _text(_first_paragraph(cells[1]), row.source)
            _text(_first_paragraph(cells[2]), wrappable(row.source_code))
            if bc:
                _destination_cell(cells[3], row)
                _bc_cell(cells[4], row)
                state = row.bc_status
            else:
                _destination_cell(cells[3], row)
                if row.destination_unit:
                    _text(_first_paragraph(cells[4]), row.destination_unit)
                elif row.source_units:
                    _text(_first_paragraph(cells[4]), '; '.join(row.source_units), color=_MUTED)
                state = row.status
            approved = state == 'approved'
            _text(_first_paragraph(cells[5]), state.capitalize() if state else '–',
                  italic=not approved, color=None if approved else _MUTED)
            # A page break inside a metric would leave the rows after it under
            # an empty Metric cell, so each row but the last stays with the next.
            if not last:
                for cell in cells:
                    for paragraph in cell.paragraphs:
                        paragraph.paragraph_format.keep_with_next = True


def _open_edges(cell, *, top, bottom):
    """Draw no rule on the named edges of *cell*.

    This, not a vertical merge, joins a metric's cells: LibreOffice misdraws a
    merged cell whose rows are kept together, and a metric must not straddle
    a page.
    """
    edges = [edge for edge, opened in (('top', top), ('bottom', bottom)) if opened]
    if edges:
        borders = _element('tcBorders')
        for edge in edges:
            borders.append(_element(edge, val='nil'))
        cell._tc.get_or_add_tcPr().append(borders)


def _metric_cell(cell, metric_rows):
    key = metric_rows[0].metric_key
    _text(_first_paragraph(cell), wrappable(key) if key else '–', bold=bool(key),
          color=None if key else _MUTED)
    # Sources may produce one metric differently; name each way once.
    classes = list(dict.fromkeys(row.measure_class for row in metric_rows if row.measure_class))
    if classes:
        _text(_paragraph(cell), ' / '.join(classes), size=_SMALL_PT, color=_MUTED)


def _bc_cell(cell, row):
    first = _first_paragraph(cell)
    _text(first, wrappable(row.bc_name), bold=True)
    _text(_paragraph(cell), f'NCIt {row.ncit_code}' if row.ncit_code else row.bc_id,
          size=_SMALL_PT, color=_MUTED)


def _destination_cell(cell, row):
    first = _first_paragraph(cell)
    if not row.mapped:
        _text(first, NOT_MAPPED, italic=True, color=_MUTED)
    elif not row.destination_code:
        _text(first, f'Concept {row.concept_id}')
        _text(_paragraph(cell), 'Not loaded on this instance', size=_SMALL_PT, italic=True, color=_MUTED)
    else:
        _text(first, wrappable(f'{row.destination_vocabulary}:{row.destination_code}'))
        _text(_paragraph(cell), row.destination_name, size=_SMALL_PT, color=_MUTED)


def _first_paragraph(cell):
    return _tight(cell.paragraphs[0])


def _paragraph(cell):
    return _tight(cell.add_paragraph())


def _tight(paragraph):
    from docx.shared import Pt

    paragraph.paragraph_format.space_before = paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.line_spacing = 1.0
    return paragraph


def _table_properties(table, columns=DOCX_COLUMNS):
    """Fixed layout at the column widths, thin grey rules, a little padding.

    The children go in the order the schema lists them: Word refuses a
    document whose table properties are out of order.
    """
    from docx.oxml.ns import qn

    properties = table._tbl.tblPr
    for tag in ('tblW', 'tblBorders', 'tblLayout', 'tblCellMar'):
        for stale in properties.findall(qn(f'w:{tag}')):
            properties.remove(stale)
    width = _element('tblW', w=round(sum(inches for _, inches in columns) * 1440), type='dxa')
    borders = _element('tblBorders')
    for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        borders.append(_element(edge, val='single', sz=4, space=0, color=_RULE))
    margins = _element('tblCellMar')
    for edge, twips in (('top', 45), ('left', 85), ('bottom', 45), ('right', 85)):
        margins.append(_element(edge, w=twips, type='dxa'))
    look = properties.find(qn('w:tblLook'))
    for element in (width, borders, _element('tblLayout', type='fixed'), margins):
        if look is None:
            properties.append(element)
        else:
            look.addprevious(element)


def _row_properties(row, header=False):
    """Keep a row on one page; repeat the header row on every page."""
    properties = row._tr.get_or_add_trPr()
    properties.append(_element('cantSplit'))
    if header:
        properties.append(_element('tblHeader'))


def _with_fixed_times(archive, day):
    """*archive* with each member stamped *day*, not the moment it was zipped."""
    stamp = (max(day.year, 1980), day.month, day.day, 0, 0, 0)
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive)) as source, \
            zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as target:
        for member in source.infolist():
            info = zipfile.ZipInfo(member.filename, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            target.writestr(info, source.read(member.filename))
    return buffer.getvalue()
