"""``/api/v1/phr/consent/`` — the signed-in member's permissions, contact
preferences, declarations and document acceptances.

GET  consent/            what is asked, in the member's language, with the
                         current answer to each and any document to confirm
POST consent/            record answers: ``{"decisions": [{"purpose", "decision",
                         "version", "frequency"?}]}``; all or none
POST consent/documents/  accept a document: ``{"document", "version"}``
GET  consent/history/    every answer the member has given, newest first

``?lang=xx`` picks the language; otherwise the Accept-Language header, then
English. A decision names the version the member saw, and is refused with
409 when that is no longer the published version, so stale wording is never
agreed to.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from rest_framework.response import Response

from patient_portal import consent as svc
from patient_portal.api.permissions import MemberWritePermission
from patient_portal.models import ConsentEvent, ConsentPurpose, LegalDocument

from .views import PhrView


def body_of(request) -> dict:
    """The JSON object posted, or an empty one when the body is not an object."""
    data = getattr(request, 'data', None)
    return data if isinstance(data, dict) else {}


def language_of(request) -> str:
    lang = request.query_params.get('lang') or body_of(request).get('lang')
    if not isinstance(lang, str):
        lang = ''
    if not lang:
        header = request.headers.get('Accept-Language', '')
        lang = header.split(',')[0].split(';')[0].strip() if header else ''
    lang = (lang or svc.DEFAULT_LANGUAGE).lower()
    return lang.split('-')[0] or svc.DEFAULT_LANGUAGE


_string = svc.required_string
_text = svc.text_payload


def purpose_payload(state: svc.PurposeState) -> dict:
    p, latest = state.purpose, state.latest
    return {
        'key': p.key,
        'kind': p.kind,
        'answers': p.allowed_decisions(),
        'asks_frequency': p.asks_frequency,
        'frequency_options': p.frequency_options if p.asks_frequency else [],
        'stage': p.stage,
        'requires': p.requires or None,
        'available': state.available,
        'text': _text(state.text),
        'decision': state.decision,
        'granted': state.granted,
        'frequency': latest.frequency if latest else '',
        'decided_version': latest.version if latest else None,
        'decided_at': latest.recorded_at if latest else None,
        'outdated': state.outdated,
    }


def document_payload(state: svc.DocumentState) -> dict:
    d, latest = state.document, state.latest
    return {
        'key': d.key,
        'name': d.name,
        'text': _text(state.text),
        'accepted_version': state.accepted_version,
        'accepted_at': latest.accepted_at if latest else None,
        'needs_acceptance': state.needs_acceptance,
    }


class ConsentView(PhrView):
    # A withdrawal can erase the record: a read-only token may not make one.
    permission_classes = [MemberWritePermission]
    # Giving the permission again is how a record that is off comes back.
    writes_record = False

    def build(self, person, record, language=svc.DEFAULT_LANGUAGE):
        return {
            'language': language,
            'purposes': [purpose_payload(s) for s in svc.purpose_states(person, language)],
            'documents': [document_payload(s) for s in svc.document_states(person, language)],
        }

    def get(self, request):
        person, record, error = self.resolve(request)
        if error:
            return error
        return Response(self.build(person, record, language_of(request)))

    def post(self, request):
        person, record, error = self.resolve(request)
        if error:
            return error
        language = language_of(request)
        decisions = body_of(request).get('decisions')
        if not isinstance(decisions, list) or not decisions:
            return Response({'detail': 'decisions must be a non-empty list.'}, status=400)
        keys = [d.get('purpose') for d in decisions if isinstance(d, dict) and isinstance(d.get('purpose'), str)]
        if len(set(keys)) != len(keys):
            return Response({'detail': 'A batch names each purpose once.'}, status=400)
        email = svc.member_email(person)
        if not email:
            return Response({'detail': svc.NO_EMAIL}, status=400)
        purposes = {p.key: p for p in ConsentPurpose.objects.filter(is_active=True).prefetch_related('texts')}
        try:
            pairs = []
            for item in decisions:
                if not isinstance(item, dict):
                    raise ValidationError('Each decision is an object.')
                key = _string(item.get('purpose'), 'purpose')
                purpose = purposes.get(key)
                if purpose is None:
                    raise ValidationError(f'Unknown purpose {key!r}.')
                pairs.append((purpose, item))
            with transaction.atomic():
                svc.record_decisions(person, pairs, language=language, recorded_by=request.user, email=email)
        except svc.StaleVersion as e:
            return Response({'detail': e.messages[0]}, status=409)
        except ValidationError as e:
            return Response({'detail': e.messages[0]}, status=400)
        return Response(self.build(person, record, language))


class ConsentDocumentsView(PhrView):
    http_method_names = ['post', 'head', 'options']
    permission_classes = [MemberWritePermission]
    writes_record = False

    def post(self, request):
        person, record, error = self.resolve(request)
        if error:
            return error
        language = language_of(request)
        body = body_of(request)
        email = svc.member_email(person)
        if not email:
            return Response({'detail': svc.NO_EMAIL}, status=400)
        try:
            key = _string(body.get('document'), 'document')
            document = LegalDocument.objects.filter(key=key, is_active=True).first()
            if document is None:
                raise ValidationError(f'Unknown document {key!r}.')
            svc.accept_document(
                person, document, version=_string(body.get('version'), 'version'), language=language,
                recorded_by=request.user, source=ConsentEvent.MEMBER, email=email,
            )
        except svc.StaleVersion as e:
            return Response({'detail': e.messages[0]}, status=409)
        except ValidationError as e:
            return Response({'detail': e.messages[0]}, status=400)
        return Response({'documents': [document_payload(s) for s in svc.document_states(person, language)]})


class ConsentHistoryView(PhrView):
    # The endpoint's rows keep their shape; the title and current flag are
    # for the record export.
    FIELDS = ('kind', 'key', 'decision', 'version', 'language', 'frequency', 'source', 'recorded_at')

    def build(self, person, record):
        return {'history': [{k: row[k] for k in self.FIELDS} for row in svc.consent_history(person)]}
