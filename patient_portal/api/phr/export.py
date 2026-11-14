"""``/api/v1/phr/record/export/`` — the signed-in member's own record, to keep.

GET record/export/?format=fhir   the member's record as a signed FHIR R4 Bundle
GET record/export/?format=pdf    the same record as a PDF, one page per section

Both are resolved through the member's own PatientUser link, like every other
PHR view, so the member never needs a person id. The FHIR export is the same
bundle ``/api/v1/patient-records/{person_id}/export-fhir/`` serves, with the
same ``X-Content-SHA256`` and ``X-Content-Signature`` integrity headers; the
PDF is built from the same section builders the app's screens read, so a
member who prints it sees what the app shows.

``?lang=xx`` picks the language as ``consent/`` does. The PDF's text is English
only for now; the choice is recorded in the document's metadata.

The PDF is set in DejaVu Sans (``fonts/``), embedded, so a name, drug or
facility in Cyrillic, Greek or an extended Latin script (Polish, Vietnamese)
renders as written. CJK scripts are the remaining gap: see ``FONT``.

Every export writes an audit row with an export marker (``detail.export``),
so the Privacy Policy's record of exports can be answered from the audit trail
(see ``AuditLogMiddleware``).
"""
from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from patient_portal.api.fhir.integrity import check_fhir_version, signed_fhir_response

from .consent import language_of
from .labs import labs
from .outcomes import outcomes
from .records import medications, procedures
from .therapy import therapy
from .views import PhrView, about_fields, diagnoses, patient_name

FORMATS = ('fhir', 'pdf')
PDF_CONTENT_TYPE = 'application/pdf'

# A4 less the margins render_record_pdf sets; every table is laid out to it.
PAGE_MARGIN_MM = 15

# The PDF's one typeface. reportlab's built-in Helvetica carries WinAnsi
# glyphs only, so anything outside Western Latin rendered as boxes; DejaVu
# Sans (Bitstream Vera licence, ``fonts/LICENSE-DejaVu.txt``) is embedded,
# subset to the glyphs used, and covers Latin with its extensions, Greek,
# Cyrillic, Armenian and Georgian. It has Arabic and Hebrew glyphs too, but
# reportlab does no bidi reordering or shaping, so those do not read as
# written either. It has no CJK glyphs: a Chinese, Japanese or Korean name
# still shows as boxes, the export still succeeds. That needs a second, much
# larger font (Noto Sans CJK) switched in per run of text, which reportlab
# does not do by itself.
FONT = 'DejaVuSans'
FONT_BOLD = 'DejaVuSans-Bold'
FONT_DIR = Path(__file__).resolve().parent / 'fonts'

# One PDF save at a time per process. The registered TTFont objects are
# process-global, and when a document is saved reportlab subsets the face by
# walking the font file with a cursor kept on that shared object
# (``TTFontFace.makeSubset``), so two exports saving at once on one gunicorn
# worker (both Dockerfiles run ``--threads 2``) interleave their reads and
# embed corrupt glyphs, or fail with a KeyError. Helvetica, being built in,
# had no such state. Only registration and ``doc.build`` hold the lock: the
# section reads and the story are built outside it, so one member's
# database time never queues another's export.
_PDF_LOCK = threading.Lock()


def register_fonts() -> None:
    """Register the typeface with reportlab, once per process.

    reportlab's registry is process-global, so the second call is a no-op;
    it is called from ``render_record_pdf`` rather than at import so that
    loading this module never reads font files. The caller holds
    ``_PDF_LOCK``: the check and the registrations are one step, so a second
    thread cannot see the regular face registered and build before the bold
    face and the family mapping exist.
    """
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if FONT_BOLD in pdfmetrics.getRegisteredFontNames():
        return
    pdfmetrics.registerFont(TTFont(FONT, str(FONT_DIR / f'{FONT}.ttf')))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(FONT_DIR / f'{FONT_BOLD}.ttf')))
    # ``<b>`` inside a Paragraph resolves through this family table; there is
    # no italic face, so ``<i>`` falls back to the upright one.
    addMapping(FONT, 0, 0, FONT)
    addMapping(FONT, 1, 0, FONT_BOLD)
    addMapping(FONT, 0, 1, FONT)
    addMapping(FONT, 1, 1, FONT_BOLD)


class RecordExportView(PhrView):
    """The member's record as a file: ``?format=fhir`` or ``?format=pdf``."""

    renderer_classes = [JSONRenderer]
    # Django answers HEAD with get(): the whole export built and thrown away,
    # unaudited (the middleware skips HEAD). Nothing needs it.
    http_method_names = ['get', 'options']

    def perform_content_negotiation(self, request, force=False):
        # ``?format=`` names the export here, not a DRF renderer: the default
        # negotiator reads that same parameter as a URL format suffix and
        # answers 404 for any value it has no renderer for, so it is bypassed.
        # Errors still render as JSON; a successful export is an HttpResponse.
        return JSONRenderer(), JSONRenderer.media_type

    def get(self, request):
        fmt = (request.query_params.get('format') or '').strip().lower()
        if fmt not in FORMATS:
            return Response(
                {'detail': 'format must be one of: ' + ', '.join(FORMATS) + '.', 'formats': list(FORMATS)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if fmt == 'fhir':
            version_error = check_fhir_version(request)
            if version_error:
                return Response({'detail': version_error}, status=status.HTTP_406_NOT_ACCEPTABLE)

        person, record, error = self.resolve(request)
        if error:
            return error

        if fmt == 'fhir':
            response = signed_fhir_response(person, filename='my-record.fhir.json')
        else:
            response = pdf_response(person, record, language_of(request))
        # The middleware writes one audit row per request; this marks it as an
        # export (of which format, of whose record) so exports can be listed.
        # The marker goes on the Django request, which is what the middleware
        # holds: DRF's Request only reads through to it.
        request._request.audit_detail = {'export': fmt, 'person_id': person.person_id}
        return response


def pdf_response(person, record, lang: str) -> HttpResponse:
    body = render_record_pdf(person, record, lang)
    response = HttpResponse(body, content_type=PDF_CONTENT_TYPE)
    response['Content-Disposition'] = 'attachment; filename="my-record.pdf"'
    response['Cache-Control'] = 'no-store'
    return response


# ---------------------------------------------------------------- PDF

# English only for now. ``?lang=`` is accepted and recorded so that adding a
# language is a new entry here, not a new contract.
LABELS: dict[str, dict[str, str]] = {
    'en': {
        'title': 'My record',
        'exported': 'Exported on {date}',
        'nothing': 'Nothing recorded.',
        'about': 'About me',
        'diagnoses': 'Diagnoses',
        'therapy': 'Lines of therapy',
        'outcomes': 'Check-ins',
        'medications': 'Medications',
        'procedures': 'Procedures',
        'labs': 'Lab results',
        'name': 'Name',
        'date_of_birth': 'Date of birth',
        'gender': 'Gender',
        'phone': 'Phone',
        'email': 'Email',
        'address': 'Address',
        'race': 'Race',
        'ethnicity': 'Ethnicity',
        'blood_type': 'Blood type',
        'cancer': 'Cancer diagnoses',
        'other_conditions': 'Other conditions',
        'historical_procedures': 'Historical procedures',
        'condition': 'Condition',
        'diagnosed': 'Diagnosed',
        'status': 'Status',
        'subtype': 'Subtype',
        'spread': 'Spread',
        'line': 'Line {number}',
        'regimen': 'Regimen',
        'dates': 'Dates',
        'ongoing': 'ongoing',
        'outcome': 'Outcome',
        'intent': 'Intent',
        'stopped_because': 'Stopped because',
        'line_medications': 'Medicines',
        'line_procedures': 'Procedures',
        'survey': 'Survey',
        'last_completed': 'Last completed',
        'completed': 'Completed',
        'next_due': 'Next due',
        'never': 'never',
        'times': '{count} time(s)',
        'survey_status': {'due_now': 'Due now', 'overdue': 'Overdue', 'upcoming': 'Upcoming'},
        'medication': 'Medication',
        'dose': 'Dose',
        'current': 'Current',
        'past': 'Past',
        'started': 'Started',
        'ended': 'Ended',
        'confirmed': 'You said',
        'procedure': 'Procedure',
        'date': 'Date',
        'performed_by': 'Performed by',
        'where': 'Where',
        'note': 'Your note',
        'test': 'Test',
        'latest': 'Latest result',
        'range': 'Reference range',
        'results': 'Results',
        'recent': 'Recent',
        'source': 'Source',
        'source_patient': 'entered by you',
        'source_record': 'from your medical record',
    },
}


def labels_for(lang: str) -> dict[str, Any]:
    return LABELS.get(lang, LABELS['en'])


def _s(value: Any) -> str:
    """Text safe inside a reportlab Paragraph (its markup is XML)."""
    return escape('' if value is None else str(value))


def _when(start: str | None, end: str | None, ongoing: str) -> str:
    if not start:
        return ''
    return f'{start} – {end or ongoing}'


def _source_text(source: dict | None, labels: dict) -> str | None:
    if not source:
        return None
    kind = labels['source_patient'] if source.get('kind') == 'patient' else labels['source_record']
    facility = source.get('facility')
    return f'{kind} ({facility})' if facility else kind


def _styles():
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    sheet = getSampleStyleSheet()
    # The sample sheet is set in Helvetica; every style is re-based on the
    # embedded face (bold for the title and headings) so no run of text
    # falls back to a font without the glyphs.
    body = ParagraphStyle('body', parent=sheet['BodyText'], fontName=FONT)
    return {
        'title': ParagraphStyle('title', parent=sheet['Title'], fontName=FONT_BOLD),
        'h1': ParagraphStyle('h1', parent=sheet['Heading1'], fontName=FONT_BOLD),
        'h2': ParagraphStyle('h2', parent=sheet['Heading2'], fontName=FONT_BOLD),
        'h3': ParagraphStyle('h3', parent=sheet['Heading3'], fontName=FONT_BOLD),
        'body': body,
        'small': ParagraphStyle('small', parent=body, fontSize=8, leading=10, textColor='#555555'),
        'cell': ParagraphStyle('cell', parent=body, fontSize=9, leading=11),
    }


def _frame_width() -> float:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm

    return A4[0] - 2 * PAGE_MARGIN_MM * mm


def _table(rows: list[list[str]], weights: list[float], styles) -> Any:
    """A table across the full frame, its columns in proportion to ``weights``."""
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle

    frame = _frame_width()
    widths = [frame * w / sum(weights) for w in weights]
    data = [[Paragraph(_s(cell), styles['cell']) for cell in row] for row in rows]
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        # The cells are Paragraphs in the cell style, but the table sets its
        # own cell font (Helvetica by default) before drawing each one.
        ('FONTNAME', (0, 0), (-1, -1), FONT),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eeeeee')),
        ('LINEBELOW', (0, 0), (-1, 0), 0.5, colors.HexColor('#999999')),
        ('LINEBELOW', (0, 1), (-1, -1), 0.25, colors.HexColor('#dddddd')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 3),
        ('RIGHTPADDING', (0, 0), (-1, -1), 3),
    ]))
    return table


def _pairs(rows: list[tuple[str, Any]], styles) -> list:
    """Label/value lines, skipping empty values."""
    from reportlab.platypus import Paragraph

    return [
        Paragraph(f'<b>{_s(label)}:</b> {_s(value)}', styles['body'])
        for label, value in rows if value not in (None, '', [])
    ]


def _about(person, record, labels, styles, diagnosed) -> list:
    rows = [(labels['name'], patient_name(person) or None)]
    for field in about_fields(record, person):
        value = field['value']
        origin = _source_text(field.get('source'), labels)
        rows.append((labels.get(field['key'], field['key'].replace('_', ' ').capitalize()),
                     f'{value} ({origin})' if origin else value))
    return _pairs(rows, styles)


def _cancer_entry(entry: dict, labels, styles) -> list:
    from reportlab.platypus import Paragraph

    out = [Paragraph(_s(entry.get('name')), styles['h3'])]
    rows = [
        (labels['diagnosed'], entry.get('date')),
        (labels['status'], entry.get('status')),
        (labels['subtype'], entry.get('subtype')),
    ]
    for key in ('stage', 'risk'):
        labelled = entry.get(key)
        if isinstance(labelled, dict):
            rows.append((labelled.get('label'), labelled.get('value')))
    rows.append((labels['spread'], entry.get('spread')))
    for marker in entry.get('biomarkers') or []:
        rows.append((marker.get('label'), marker.get('value')))
    rows.append((labels['source'], _source_text(entry.get('source'), labels)))
    out.extend(_pairs(rows, styles))
    transitions = [t for t in entry.get('transitions') or [] if t.get('name')]
    if len(transitions) > 1:
        out.append(Paragraph(
            ' → '.join(f"{_s(t.get('name'))} ({_s(t.get('date'))})" for t in transitions), styles['small']))
    return out


def _diagnoses(person, record, labels, styles, diagnosed) -> list:
    from reportlab.platypus import Paragraph

    data = diagnosed
    out = []
    if data['cancer']:
        out.append(Paragraph(labels['cancer'], styles['h2']))
        for entry in data['cancer']:
            out.extend(_cancer_entry(entry, labels, styles))
    for key, heading in (('other', 'other_conditions'), ('historical_procedures', 'historical_procedures')):
        if data[key]:
            out.append(Paragraph(labels[heading], styles['h2']))
            rows = [[labels['diagnosed'], labels['condition'], labels['status'], labels['source']]]
            for entry in data[key]:
                rows.append([entry.get('date') or '', entry.get('name') or '', entry.get('status') or '',
                             _source_text(entry.get('source'), labels) or ''])
            out.append(_table(rows, [70, 220, 90, 150], styles))
    return out


def _therapy(person, record, labels, styles, diagnosed) -> list:
    from reportlab.platypus import Paragraph

    out = []
    for track in therapy(person, record, diagnosed)['tracks']:
        out.append(Paragraph(_s(track['diagnosis']['name']), styles['h2']))
        if not track['lines']:
            out.append(Paragraph(labels['nothing'], styles['body']))
        for line in track['lines']:
            out.append(Paragraph(labels['line'].format(number=_s(line.get('number'))), styles['h3']))
            outcome = line.get('outcome') or {}
            patient_reason = line.get('patient_reason') or {}
            out.extend(_pairs([
                (labels['regimen'], line.get('regimen')),
                (labels['dates'], _when(line.get('start'), line.get('end'), labels['ongoing'])),
                (labels['outcome'], outcome.get('label')),
                (labels['intent'], line.get('intent')),
                (labels['stopped_because'], line.get('stopped_because') or patient_reason.get('label')),
                (labels['line_medications'], ', '.join(
                    f"{m['name']} ({_when(m.get('start'), m.get('end'), labels['ongoing'])})"
                    if m.get('start') else m['name']
                    for m in line.get('medications') or [])),
                (labels['line_procedures'], ', '.join(
                    f"{p['name']} ({p['date']})" for p in line.get('procedures') or [])),
                (labels['source'], _source_text(line.get('source'), labels)),
            ], styles))
    return out


def _outcomes(person, record, labels, styles, diagnosed) -> list:
    surveys = outcomes(person, record)['surveys']
    if not surveys:
        return []
    rows = [[labels['survey'], labels['status'], labels['last_completed'], labels['completed'], labels['next_due']]]
    for survey in surveys:
        rows.append([
            survey.get('title') or survey.get('slug') or '',
            labels['survey_status'].get(survey.get('status'), survey.get('status') or ''),
            survey.get('last_completed') or labels['never'],
            labels['times'].format(count=survey.get('completed') or 0),
            survey.get('next_due') or '',
        ])
    return [_table(rows, [190, 70, 90, 90, 90], styles)]


def _medications(person, record, labels, styles, diagnosed) -> list:
    items = medications(person, record)['medications']
    if not items:
        return []
    rows = [[labels['medication'], labels['dose'], labels['status'], labels['started'], labels['ended'],
             labels['source']]]
    for item in items:
        confirmation = item.get('confirmation') or {}
        said = confirmation.get('status')
        status_text = labels['current'] if item.get('status') == 'current' else labels['past']
        if said and not confirmation.get('assumed'):
            status_text = f"{status_text} ({labels['confirmed']}: {said})"
        rows.append([
            item.get('name') or '', item.get('dose') or '', status_text,
            item.get('started') or '', item.get('ended') or '',
            _source_text(item.get('source'), labels) or '',
        ])
    return [_table(rows, [150, 80, 90, 65, 65, 80], styles)]


def _procedures(person, record, labels, styles, diagnosed) -> list:
    items = procedures(person, record)['procedures']
    if not items:
        return []
    rows = [[labels['date'], labels['procedure'], labels['performed_by'], labels['where'], labels['note']]]
    for item in items:
        when = item.get('date') or ''
        if item.get('end_date'):
            when = f"{when} – {item['end_date']}"
        rows.append([when, item.get('name') or '', item.get('performed_by') or '',
                     item.get('where') or _source_text(item.get('source'), labels) or '', item.get('note') or ''])
    return [_table(rows, [85, 185, 90, 90, 80], styles)]


def _lab_value(result: dict | None) -> str:
    if not result:
        return ''
    value = result.get('value')
    text = result.get('value_text')
    shown = text if value is None else str(value)
    if shown is None:
        return ''
    unit = result.get('unit')
    flag = result.get('flag')
    return ' '.join(part for part in (shown, unit, f'[{flag}]' if flag else None) if part)


def _lab_range(result: dict | None) -> str:
    rng = (result or {}).get('range')
    if not rng:
        return ''
    low, high = rng.get('low'), rng.get('high')
    if low is not None and high is not None:
        return f'{low} – {high}'
    return f'≥ {low}' if low is not None else f'≤ {high}'


def _labs(person, record, labels, styles, diagnosed) -> list:
    tests = labs(person, record, diagnosed)['tests']
    if not tests:
        return []
    rows = [[labels['test'], labels['date'], labels['latest'], labels['range'], labels['recent'], labels['results']]]
    for test in tests:
        latest = test.get('latest') or {}
        name = test['name'] + (f" ({test['specimen']})" if test.get('specimen') else '')
        recent = '; '.join(
            f"{r.get('date')}: {_lab_value(r)}" for r in (test.get('recent') or [])[1:] if r.get('date'))
        rows.append([name, latest.get('date') or '', _lab_value(latest), _lab_range(latest), recent,
                     str(test.get('count') or '')])
    return [_table(rows, [130, 60, 85, 75, 140, 40], styles)]


SECTIONS = (
    ('about', _about),
    ('diagnoses', _diagnoses),
    ('therapy', _therapy),
    ('outcomes', _outcomes),
    ('medications', _medications),
    ('procedures', _procedures),
    ('labs', _labs),
)


def render_record_pdf(person, record, lang: str = 'en') -> bytes:
    """The member's record as a PDF: a title page, then one page per section."""
    # Paragraphs measure their text as they are made, so the fonts must be
    # registered before the story is built.
    with _PDF_LOCK:
        register_fonts()
    return _render_record_pdf(person, record, lang)


def _render_record_pdf(person, record, lang: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    labels = labels_for(lang)
    styles = _styles()
    name = patient_name(person)
    today = timezone.localdate()
    # Therapy and labs each read the Diagnoses section too; built once here.
    diagnosed = diagnoses(person, record)

    story = [
        Paragraph(_s(labels['title']), styles['title']),
        Paragraph(_s(name), styles['h2']) if name else Spacer(1, 1),
        Paragraph(_s(labels['exported'].format(date=today.isoformat())), styles['small']),
    ]
    for key, build in SECTIONS:
        story.append(PageBreak())
        story.append(Paragraph(_s(labels[key]), styles['h1']))
        story.extend(build(person, record, labels, styles, diagnosed) or [Paragraph(labels['nothing'], styles['body'])])

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(FONT, 8)
        canvas.drawRightString(A4[0] - PAGE_MARGIN_MM * mm, 10 * mm,
                               f'{name} · {today.isoformat()} · {doc.page}'.strip(' ·'))
        canvas.restoreState()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=PAGE_MARGIN_MM * mm, rightMargin=PAGE_MARGIN_MM * mm,
        topMargin=PAGE_MARGIN_MM * mm, bottomMargin=18 * mm,
        title=f"{labels['title']} – {name}".strip(' –'), author=name or '', subject=f'lang={lang}',
        # The canvas names its default font (Helvetica) on every page's
        # resources even when nothing is drawn with it; start it on ours.
        initialFontName=FONT,
    )
    with _PDF_LOCK:  # the save subsets the shared font objects
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
