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
from dataclasses import dataclass
from itertools import groupby

from django.utils.timezone import localdate

from omop_core.models import SourceCodeConceptMapping
from omop_core.services.disease_field_sets import csv_cell
from omop_core.services.mapping_browse import canonical_source, tab_queryset
from omop_core.services.mapping_source_units import SourceUnits
from omop_core.services.source_vocabularies import WEARABLE_METRIC_CATEGORIES

#: The tab every wearable vocabulary is shown on.
WEARABLES_TAB = canonical_source('OpenWearables')

FORMAT_CSV = 'csv'
FORMAT_DOCX = 'docx'
CONTENT_TYPES = {
    FORMAT_CSV: 'text/csv; charset=utf-8',
    FORMAT_DOCX: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}

TITLE = 'Wearable metric mappings'
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
}
_SOURCE_ORDER = {vocabulary: index for index, vocabulary in enumerate(SOURCE_LABELS)}
_CATEGORY_ORDER = {category: index for index, category in enumerate(WEARABLE_METRIC_CATEGORIES)}

CSV_COLUMNS = (
    'Category', 'Measure class', 'Metric key', 'Source', 'Source code', 'Description',
    'Source unit', 'Destination vocabulary', 'Destination code', 'Destination concept',
    'OMOP concept ID', 'Domain', 'Destination unit', 'Status', 'Provenance',
)


class FormatError(ValueError):
    """The caller asked for a file format this export does not write."""


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


def export_rows():
    """Every mapping the Wearables tab can show, rejected ones aside, in file order.

    The tab's rows are the browse service's: every wearable vocabulary,
    global and hospital-scoped alike (browse shows a reader every
    organization's rows). The list endpoint's ICD-10-CM Athena guard is not
    applied, since it can only remove ICD10CM rows. One query, whatever the
    row count.
    """
    return rows_from(tab_mappings().exclude(status='rejected'))


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


def export_file(file_format, day=None):
    """``(filename, content type, body)`` of the tab in *file_format*."""
    if file_format not in CONTENT_TYPES:
        raise FormatError(f"Unknown file format {file_format!r}. Use csv or docx.")
    day = day or localdate()
    rows = export_rows()
    body = csv_text(rows) if file_format == FORMAT_CSV else docx_bytes(rows, day)
    return f'wearable-mappings-{day.isoformat()}.{file_format}', CONTENT_TYPES[file_format], body


# ── CSV ──────────────────────────────────────────────────────────────────

def csv_text(rows):
    """One line per mapping under a header row; UTF-8, ``\\n`` line ends.

    It opens with a byte order mark: descriptions carry "°C" and "μS", and
    Excel reads a UTF-8 file without one as Windows-1252 ("Â°C"). A source
    code or a description is whatever a producer sent, so text a spreadsheet
    would run as a formula is quoted (``csv_cell``).
    """
    buffer = io.StringIO()
    buffer.write('\ufeff')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(CSV_COLUMNS)
    for row in rows:
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


def docx_bytes(rows, day):
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

    document.add_heading(TITLE, level=0)
    _text(document.add_paragraph(), summary(rows, day), size=10, color=_MUTED)
    _text(document.add_paragraph(), LEGEND, size=10)
    if not rows:
        _text(document.add_paragraph(), 'There are no wearable mappings.', size=10, italic=True)
    for heading, members in sections(rows):
        document.add_heading(f'{heading} ({len(members)})', level=1)
        _category_table(document, members)

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
        fonts.attrib.pop(qn(f'w:{attribute}Theme'), None)


def _text(paragraph, text, *, size=_BODY_PT, bold=False, italic=False, color=None):
    from docx.shared import Pt, RGBColor

    run = paragraph.add_run(text)
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


def _category_table(document, members):
    from docx.shared import Inches

    table = document.add_table(rows=1, cols=len(DOCX_COLUMNS))
    _table_properties(table)
    for column, (_, width) in zip(table.columns, DOCX_COLUMNS):
        column.width = Inches(width)

    header = table.rows[0]
    _row_properties(header, header=True)
    for cell, (label, width) in zip(header.cells, DOCX_COLUMNS):
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
            _destination_cell(cells[3], row)
            if row.destination_unit:
                _text(_first_paragraph(cells[4]), row.destination_unit)
            elif row.source_units:
                _text(_first_paragraph(cells[4]), '; '.join(row.source_units), color=_MUTED)
            approved = row.status == 'approved'
            _text(_first_paragraph(cells[5]), row.status.capitalize(), italic=not approved,
                  color=None if approved else _MUTED)
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


def _table_properties(table):
    """Fixed layout at the column widths, thin grey rules, a little padding.

    The children go in the order the schema lists them: Word refuses a
    document whose table properties are out of order.
    """
    from docx.oxml.ns import qn

    properties = table._tbl.tblPr
    for tag in ('tblW', 'tblBorders', 'tblLayout', 'tblCellMar'):
        for stale in properties.findall(qn(f'w:{tag}')):
            properties.remove(stale)
    width = _element('tblW', w=round(sum(inches for _, inches in DOCX_COLUMNS) * 1440), type='dxa')
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
