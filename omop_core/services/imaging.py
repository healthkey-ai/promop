"""Imaging studies: FHIR ImagingStudy and radiology DiagnosticReport → OMOP (#1731).

The representation is recorded in ``docs/adr/0003-imaging-studies.md``:

- the study is a ``procedure_occurrence``, at the performing facility's visit;
- an ``image_occurrence`` (OHDSI Medical Imaging CDM) holds its modality, body
  site, DICOM study UID and the link to its images at the source;
- the radiologist's impression is a ``note`` kept byte-for-byte, the rest of the
  report another ``note``, both linked to the procedure (``note_event_id``);
- each finding as reported is a ``note_nlp`` row on the report note;
- contrast is an ``observation`` linked to the procedure;
- the report's file, when the source sends one, is a ``PatientDocument`` with
  ``procedure_occurrence_id`` set.

Both FHIR import paths (the upload endpoint and the provider sync) call
``import_imaging``. Re-importing a study replaces its imaging rows in place.
"""
from __future__ import annotations

import base64
import binascii
import html
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db import transaction
from django.utils import timezone

from omop_core.models import (
    CareSite,
    Concept,
    ImageOccurrence,
    Note,
    NoteNlp,
    Observation,
    PatientDocument,
    ProcedureOccurrence,
    VisitOccurrence,
)
from omop_core.services.mappings import CONCEPT_EHR_TYPE, CONCEPT_PROCEDURE_OCCURRENCE_FIELD
from omop_core.services.pk import next_pk
from omop_core.services.source_vocabularies import fhir_source_vocabulary
from omop_core.signals import suppress_patient_record_refresh

logger = logging.getLogger(__name__)

IMPRESSION = 'rad-impression'
REPORT = 'rad-report'
CONTRAST = 'imaging-contrast'
NLP_SYSTEM = 'FHIR DiagnosticReport.result'

# DiagnosticReport.category codes for radiology: HL7 v2-0074 and LOINC.
RADIOLOGY_CATEGORIES = {'RAD', 'RX', 'CT', 'NMR', 'NMS', 'RUS', 'VUS', 'LP29684-5'}

# DICOM modality codes, and how the record names them.
MODALITY_LABELS = {
    'CT': 'CT', 'MR': 'MRI', 'PT': 'PET', 'NM': 'Nuclear medicine', 'US': 'Ultrasound',
    'CR': 'X-ray', 'DX': 'X-ray', 'RF': 'Fluoroscopy', 'MG': 'Mammogram', 'XA': 'Angiography',
}
_TEXT_MODALITIES = [
    (re.compile(r'\bPET\b', re.I), 'PT'),
    (re.compile(r'\bMRI?\b|magnetic resonance', re.I), 'MR'),
    (re.compile(r'\bCT\b|computed tomograph', re.I), 'CT'),
    (re.compile(r'ultrasound|sonogra|\bUS\b', re.I), 'US'),
    (re.compile(r'mammogra', re.I), 'MG'),
    (re.compile(r'x-?ray|\bXR\b|radiograph', re.I), 'DX'),
    (re.compile(r'bone scan|scintigra|nuclear medicine', re.I), 'NM'),
]

_WITHOUT_CONTRAST = re.compile(
    r'\b(w/o|wo|without)\s+(iv\s+)?contrast\b|\bnon-?contrast\b|\bno\s+(iv\s+|iodinated\s+)?contrast\b|\bunenhanced\b',
    re.I)
_WITH_CONTRAST = re.compile(
    r'\b(w/?|with)\s+(iv\s+)?contrast\b|\bcontrast[- ]enhanced\b'
    r'|\bw\s*(/|and|&)?\s*wo?\b.*contrast|\bwith\s+and\s+without\s+(iv\s+)?contrast\b', re.I)
_AGENTS = [
    (re.compile(r'gadolinium|\bgad\b|gadobutrol|gadoterate', re.I), 'Gadolinium'),
    (re.compile(r'\bFDG\b|fluorodeoxyglucose', re.I), 'FDG tracer'),
    (re.compile(r'\bPSMA\b', re.I), 'PSMA tracer'),
    (re.compile(r'iodinated|iohexol|iopamidol|omnipaque', re.I), 'Iodinated contrast'),
]


# --------------------------------------------------------------- bundle helpers

def index_resources(entries: Iterable[dict]) -> dict[str, dict]:
    """Every resource in a bundle by the references that can point at it."""
    index: dict[str, dict] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        res = entry.get('resource') or {}
        if not isinstance(res, dict):
            continue
        rtype, rid = res.get('resourceType'), res.get('id')
        if rtype and rid:
            index[f'{rtype}/{rid}'] = res
        url = (entry.get('fullUrl') or '').strip()
        if url:
            index[url] = res
            if url.startswith('urn:uuid:'):
                index[url[len('urn:uuid:'):]] = res
    return index


def _resolve(ref: Any, index: dict, parent: dict | None = None) -> dict | None:
    if not isinstance(ref, dict):
        return None
    target = (ref.get('reference') or '').strip()
    if not target:
        return None
    if target.startswith('#') and parent:
        return next((c for c in parent.get('contained') or [] if c.get('id') == target[1:]), None)
    if target in index:
        return index[target]
    tail = target.split('/_history/')[0]
    tail = '/'.join(tail.rstrip('/').split('/')[-2:])
    return index.get(tail)


def _codings(codeable: Any) -> list[dict]:
    if not isinstance(codeable, dict):
        return []
    return [c for c in codeable.get('coding') or [] if isinstance(c, dict)]


def _text(codeable: Any) -> str:
    if not isinstance(codeable, dict):
        return ''
    text = (codeable.get('text') or '').strip()
    if text:
        return text
    return next(((c.get('display') or '').strip() for c in _codings(codeable) if (c.get('display') or '').strip()), '')


def _date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _strip_html(value: str) -> str:
    text = re.sub(r'<br\s*/?>|</p>|</div>', '\n', value, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def is_radiology_report(report: dict) -> bool:
    if report.get('imagingStudy'):
        return True
    for category in report.get('category') or []:
        if any((c.get('code') or '').strip() in RADIOLOGY_CATEGORIES for c in _codings(category)):
            return True
        if re.search(r'radiolog|imaging', _text(category), re.I):
            return True
    return False


# --------------------------------------------------------------- mapping

def modality_label(codes: Iterable[str]) -> str | None:
    codes = list(dict.fromkeys(c.upper() for c in codes if c))
    if 'PT' in codes and 'CT' in codes:
        return 'PET/CT'
    if 'PT' in codes and 'MR' in codes:
        return 'PET/MRI'
    for code in codes:
        if code in MODALITY_LABELS:
            return MODALITY_LABELS[code]
    return codes[0] if codes else None


def modalities_from_text(text: str) -> list[str]:
    return [code for pattern, code in _TEXT_MODALITIES if text and pattern.search(text)]


def contrast(texts: Iterable[str]) -> tuple[str, str | None]:
    """('with' | 'without' | 'unknown', what to show) from the study's own words."""
    joined = ' · '.join(t for t in texts if t)
    agent = next((label for pattern, label in _AGENTS if pattern.search(joined)), None)
    if _WITHOUT_CONTRAST.search(joined) and not _WITH_CONTRAST.search(joined) and not agent:
        return 'without', 'None'
    if agent or _WITH_CONTRAST.search(joined):
        return 'with', agent or 'With contrast'
    return 'unknown', None


def _attachment_text(attachment: dict) -> str:
    content_type = (attachment.get('contentType') or '').lower()
    data = attachment.get('data')
    if not data or not (content_type.startswith('text/') or not content_type):
        return ''
    try:
        raw = base64.b64decode(data, validate=False).decode('utf-8')
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return ''
    return _strip_html(raw) if 'html' in content_type else raw.strip()


def _finding(observation: dict) -> str:
    for key in ('valueString',):
        value = (observation.get(key) or '').strip()
        if value:
            return value
    value = _text(observation.get('valueCodeableConcept'))
    name = _text(observation.get('code'))
    if value:
        return f'{name}: {value}' if name and name.lower() not in ('finding', 'impression') else value
    return name


@dataclass
class Study:
    """One imaging study as the source reported it."""
    date: date
    name: str
    code: dict | None
    modalities: list[str]
    body_site: dict | None
    study_uid: str | None
    series_uid: str | None
    image_url: str | None
    has_images: bool
    contrast: tuple[str, str | None]
    impression: str | None
    report_text: str
    findings: list[str]
    facility: str | None
    document: dict | None
    key: str


def read_study(report: dict | None, studies: list[dict], index: dict) -> Study | None:
    """Map a radiology report and the ImagingStudy resources it names to a ``Study``."""
    study = studies[0] if studies else {}
    report = report or {}
    on = (
        _date(report.get('effectiveDateTime'))
        or _date((report.get('effectivePeriod') or {}).get('start'))
        or _date(study.get('started'))
        or _date(report.get('issued'))
    )
    if on is None:
        return None

    procedure_codes = study.get('procedureCode') or []
    code = report.get('code') or (procedure_codes[0] if procedure_codes else None)
    name = _text(report.get('code')) or (study.get('description') or '').strip() or _text(code)

    series = [s for st in studies for s in st.get('series') or [] if isinstance(s, dict)]
    modalities = [
        (c.get('code') or '').upper()
        for st in studies for c in (st.get('modality') or []) if isinstance(c, dict)
    ] + [((s.get('modality') or {}).get('code') or '').upper() for s in series]
    modalities = [m for m in dict.fromkeys(modalities) if m] or modalities_from_text(
        ' '.join([name, _text(code)]))
    name = name or (f'{modality_label(modalities)} study' if modalities else 'Imaging study')

    # R4 series.bodySite is a Coding; treat it as a one-coding CodeableConcept.
    site = next((s.get('bodySite') for s in series if isinstance(s.get('bodySite'), dict)), None)
    body_site = site if site is None or 'coding' in site or 'text' in site else {'coding': [site]}

    endpoints = [_resolve(ref, index, st) for st in studies for ref in st.get('endpoint') or []]
    endpoints += [_resolve(ref, index, study) for s in series for ref in s.get('endpoint') or []]
    image_url = next(((e.get('address') or '').strip() for e in endpoints if e and e.get('address')), None)

    study_uid = None
    for st in studies:
        for identifier in st.get('identifier') or []:
            value = (identifier.get('value') or '').strip()
            if value and ((identifier.get('system') or '') == 'urn:dicom:uid' or value.startswith('urn:oid:')):
                study_uid = value.removeprefix('urn:oid:')
                break
        if study_uid:
            break
    series_uid = (series[0].get('uid') or '').strip() or None if len(series) == 1 else None
    has_images = bool(study_uid or image_url or any(
        (st.get('numberOfInstances') or 0) > 0 or st.get('series') for st in studies))

    notes = [(n.get('text') or '').strip() for st in studies for n in st.get('note') or [] if isinstance(n, dict)]
    forms = [f for f in report.get('presentedForm') or [] if isinstance(f, dict)]
    report_text = '\n\n'.join(n for n in notes if n) or next(
        (t for t in (_attachment_text(f) for f in forms) if t), '') or _strip_html(
        ((report.get('text') or {}).get('div') or ''))

    impression = report.get('conclusion')
    impression = impression if isinstance(impression, str) and impression.strip() else None

    findings = [f for f in (_finding(o) for o in (
        _resolve(ref, index, report) for ref in report.get('result') or []) if o) if f]
    if not findings:
        findings = [t for t in (_text(c) for c in report.get('conclusionCode') or []) if t]

    facility = None
    for ref in report.get('performer') or []:
        target = _resolve(ref, index, report)
        facility = ((target or {}).get('name') or ref.get('display') or '').strip() or None
        if facility:
            break

    document = next(({'url': f['url'].strip(), 'title': (f.get('title') or '').strip() or name}
                     for f in forms if (f.get('url') or '').strip()), None)

    contrast_texts = [name, _text(code), study.get('description') or ''] + [
        (s.get('description') or '') for s in series]
    key = study_uid or (f"DiagnosticReport/{report['id']}" if report.get('id') else '') or \
        (f"ImagingStudy/{study['id']}" if study.get('id') else '') or f'{name}|{on.isoformat()}'
    return Study(
        date=on, name=name[:255], code=code, modalities=modalities, body_site=body_site,
        study_uid=study_uid, series_uid=series_uid, image_url=image_url, has_images=has_images,
        contrast=contrast(contrast_texts), impression=impression, report_text=report_text,
        findings=findings, facility=facility, document=document, key=key,
    )


# --------------------------------------------------------------- writing

def _concept(concept_id: int) -> Concept | None:
    return Concept.objects.filter(concept_id=concept_id).first()


def _coded_concept(codeable: Any, *vocabularies: str) -> Concept | None:
    for coding in _codings(codeable):
        code = (coding.get('code') or '').strip()
        vocab = fhir_source_vocabulary(coding.get('system', '')) or None
        if not code:
            continue
        query = Concept.objects.filter(concept_code=code)
        if vocab:
            query = query.filter(vocabulary_id=vocab)
        elif vocabularies:
            query = query.filter(vocabulary_id__in=vocabularies)
        else:
            continue
        concept = query.order_by('-standard_concept').first()
        if concept:
            return concept
    return None


def _visit(person, study: Study, ehr_type: Concept, no_match: Concept) -> VisitOccurrence | None:
    """The visit at the performing facility, so the study carries its provenance."""
    if not study.facility:
        return None
    site = CareSite.objects.filter(care_site_name=study.facility).first()
    if site is None:
        site = CareSite.objects.create(
            care_site_id=next_pk(CareSite, 'care_site_id'),
            care_site_name=study.facility[:255], care_site_source_value=study.facility[:50],
        )
    source_value = f'imaging:{study.key}'[:255]
    visit = VisitOccurrence.objects.filter(
        person=person, visit_start_date=study.date, care_site_id=site.care_site_id,
        visit_source_value=source_value,
    ).first()
    if visit is None:
        visit = VisitOccurrence.objects.create(
            visit_occurrence_id=next_pk(VisitOccurrence, 'visit_occurrence_id'), person=person,
            visit_concept=_concept(9202) or no_match, visit_start_date=study.date, visit_end_date=study.date,
            visit_type_concept=ehr_type, care_site_id=site.care_site_id, visit_source_value=source_value,
        )
    return visit


def _procedure(person, study: Study, ehr_type: Concept, no_match: Concept, visit) -> ProcedureOccurrence:
    concept = _coded_concept(study.code) or no_match
    source_value = ((_codings(study.code)[0].get('code') if _codings(study.code) else '') or study.name)[:50]
    existing = ProcedureOccurrence.objects.filter(
        person=person, procedure_source_value=source_value, procedure_date=study.date, is_erroneous=False,
    ).order_by('procedure_occurrence_id').first()
    if existing is not None:
        existing.procedure_concept = concept
        existing.visit_occurrence = visit or existing.visit_occurrence
        existing._skip_patient_record_refresh = True
        existing.save(update_fields=['procedure_concept', 'visit_occurrence'])
        return existing
    procedure = ProcedureOccurrence(
        procedure_occurrence_id=next_pk(ProcedureOccurrence, 'procedure_occurrence_id'), person=person,
        procedure_concept=concept, procedure_date=study.date, procedure_type_concept=ehr_type,
        visit_occurrence=visit, procedure_source_value=source_value,
        procedure_source_concept=concept if concept.concept_id else None,
    )
    procedure._skip_patient_record_refresh = True
    procedure.save()
    return procedure


def _clear(person, procedure_id: int) -> None:
    ImageOccurrence.objects.filter(person=person, procedure_occurrence_id=procedure_id).delete()
    Note.objects.filter(person=person, note_event_id=procedure_id,
                        note_source_value__in=[IMPRESSION, REPORT]).delete()  # cascades to note_nlp
    Observation.objects.filter(person=person, observation_event_id=procedure_id,
                               observation_source_value=CONTRAST).delete()


def write_study(person, study: Study, *, record_provenance: Callable | None = None) -> int | None:
    """Write (or rewrite) one study's OMOP rows. Returns its procedure_occurrence_id."""
    ehr_type, no_match = _concept(CONCEPT_EHR_TYPE), _concept(0)
    if ehr_type is None or no_match is None:
        logger.warning('imaging import skipped: concepts 0 and %s must be loaded', CONCEPT_EHR_TYPE)
        return None
    field = _concept(CONCEPT_PROCEDURE_OCCURRENCE_FIELD)
    created = []

    visit = _visit(person, study, ehr_type, no_match)
    procedure = _procedure(person, study, ehr_type, no_match, visit)
    pid = procedure.procedure_occurrence_id
    _clear(person, pid)
    created.append(procedure)

    modality = study.modalities[0] if study.modalities else None
    created.append(ImageOccurrence.objects.create(
        image_occurrence_id=next_pk(ImageOccurrence, 'image_occurrence_id'), person=person,
        procedure_occurrence=procedure, visit_occurrence=visit, image_occurrence_date=study.date,
        image_study_uid=study.study_uid, image_series_uid=study.series_uid,
        wadors_uri=study.image_url[:2048] if study.image_url else None,
        modality_concept=Concept.objects.filter(vocabulary_id='DICOM', concept_code=modality).first()
        if modality else None,
        modality_source_value=','.join(study.modalities)[:50] or None,
        anatomic_site_concept=_coded_concept(study.body_site, 'SNOMED'),
        anatomic_site_source_value=_text(study.body_site)[:255] or None,
    ))

    def note(kind: str, text: str) -> Note:
        return Note.objects.create(
            note_id=next_pk(Note, 'note_id'), person=person, note_date=study.date,
            note_type_concept=ehr_type, note_title=study.name, note_text=text,
            visit_occurrence=visit, note_source_value=kind,
            note_event_id=pid, note_event_field_concept=field,
        )

    if study.impression is not None:
        created.append(note(IMPRESSION, study.impression))
    report = note(REPORT, study.report_text)
    created.append(report)
    for position, finding in enumerate(study.findings):
        NoteNlp.objects.create(
            note_nlp_id=next_pk(NoteNlp, 'note_nlp_id'), note=report, snippet=finding[:2500],
            lexical_variant=finding[:250], offset=str(position), nlp_system=NLP_SYSTEM,
            nlp_date=timezone.now(), term_exists='Y',
        )

    status, shown = study.contrast
    contrast_row = Observation(
        observation_id=next_pk(Observation, 'observation_id'), person=person,
        observation_concept=no_match, observation_date=study.date, observation_type_concept=ehr_type,
        value_as_string=status, value_source_value=(shown or '')[:50] or None,
        observation_source_value=CONTRAST, visit_occurrence=visit,
        observation_event_id=pid, obs_event_field_concept=field,
    )
    contrast_row._skip_patient_record_refresh = True
    contrast_row.save()
    created.append(contrast_row)

    if study.document:
        document, _ = PatientDocument.objects.update_or_create(
            person=person, file_url=study.document['url'][:200],
            defaults={'doc_type': 'IMAGING', 'title': study.document['title'][:255],
                      'effective_date': study.date, 'procedure_occurrence_id': pid},
        )
        created.append(document)

    if record_provenance:
        for row in created:
            record_provenance(row)
    return pid


def import_imaging(person, reports: Iterable[dict], studies: Iterable[dict], index: dict, *,
                   record_provenance: Callable | None = None) -> list[int]:
    """Import a bundle's imaging. Returns the studies' procedure_occurrence_ids.

    Each radiology report becomes one study with the ImagingStudy resources it
    names; an ImagingStudy no report names becomes a study without a report.
    """
    studies = [s for s in studies if isinstance(s, dict)]
    pairs, named = [], set()
    for report in reports:
        if not isinstance(report, dict) or not is_radiology_report(report):
            continue
        linked = [s for s in (_resolve(ref, index, report) for ref in report.get('imagingStudy') or [])
                  if s and s.get('resourceType') == 'ImagingStudy']
        named.update(id(s) for s in linked)
        pairs.append((report, linked))
    pairs += [(None, [s]) for s in studies if id(s) not in named]

    ids = []
    with transaction.atomic(), suppress_patient_record_refresh():
        for report, linked in pairs:
            study = read_study(report, linked, index)
            if study is None:
                continue
            pid = write_study(person, study, record_provenance=record_provenance)
            if pid is not None:
                ids.append(pid)
    return ids


# --------------------------------------------------------------- reading

def imaging_studies(person) -> list[dict[str, Any]]:
    """The person's imaging studies, newest first, as the record shows them."""
    images = list(
        ImageOccurrence.objects.filter(person=person, procedure_occurrence__is_erroneous=False)
        .select_related('procedure_occurrence__procedure_concept', 'procedure_occurrence__visit_occurrence',
                        'anatomic_site_concept')
        .order_by('-image_occurrence_date', '-procedure_occurrence_id')
    )
    pids = [i.procedure_occurrence_id for i in images]
    notes: dict = {}
    for row in Note.objects.filter(person=person, note_event_id__in=pids,
                                   note_source_value__in=[IMPRESSION, REPORT]).order_by('note_id'):
        notes[(row.note_event_id, row.note_source_value)] = row
    findings: dict = {}
    for nlp in NoteNlp.objects.filter(note__in=[n for n in notes.values() if n.note_source_value == REPORT],
                                      nlp_system=NLP_SYSTEM).order_by('note_nlp_id'):
        findings.setdefault(nlp.note_id, []).append((int(nlp.offset or 0), nlp.snippet))
    contrasts = {
        o.observation_event_id: o for o in Observation.objects.filter(
            person=person, observation_event_id__in=pids, observation_source_value=CONTRAST)
    }
    documents = {
        d.procedure_occurrence_id: d for d in PatientDocument.objects.filter(
            person=person, procedure_occurrence_id__in=pids).order_by('-uploaded_at')
    }

    result, seen = [], set()
    for image in images:
        pid = image.procedure_occurrence_id
        if pid in seen:
            continue
        seen.add(pid)
        procedure = image.procedure_occurrence
        report, impression = notes.get((pid, REPORT)), notes.get((pid, IMPRESSION))
        concept = procedure.procedure_concept
        name = (
            (report.note_title if report else None)
            or (concept.concept_name if procedure.procedure_concept_id else None)
            or procedure.procedure_source_value or 'Imaging study'
        )
        modalities = [m for m in (image.modality_source_value or '').split(',') if m]
        site = image.anatomic_site_concept
        contrast_row = contrasts.get(pid)
        result.append({
            'procedure': procedure,
            'procedure_occurrence_id': pid,
            'type': modality_label(modalities),
            'modalities': modalities,
            'name': name,
            'date': image.image_occurrence_date,
            'body_part': image.anatomic_site_source_value or (site.concept_name if site else None),
            'contrast': {
                'status': contrast_row.value_as_string if contrast_row else 'unknown',
                'text': contrast_row.value_source_value if contrast_row else None,
            },
            'impression': impression.note_text if impression else None,
            'findings': [text for _, text in sorted(findings.get(report.note_id, []) if report else [])],
            'notes': (report.note_text or None) if report else None,
            'has_image': bool(image.image_study_uid or image.wadors_uri),
            'image_url': image.wadors_uri or None,
            'document': documents.get(pid),
        })
    return result
