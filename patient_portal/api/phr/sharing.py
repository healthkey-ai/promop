"""Sharing a Personal Health Record.

The patient grants view-only access to the parts of their record they choose,
for a set time, by email invite, private link or QR code. Whoever holds the
link reads those parts through ``/api/v1/phr/shared/`` with the link's token in
an ``X-Share-Token`` header; they have no account.

The token is the credential, so it is never stored. It is the share's
``public_id`` followed by an HMAC of it under a server-side key: a copy of the
database alone opens nothing, a guessed token fails before any query, and the
patient can still be shown their link again (the design re-displays it).
Rotating ``PHR_SHARE_SECRET`` (or ``SECRET_KEY`` when that is unset) closes
every open link; ``SECRET_KEY_FALLBACKS`` keep links working through a
``SECRET_KEY`` rotation.

A selection names whole sections (``{"all": true}``, which includes records
that arrive later) or items in them (``{"items": [...]}``). Items are record
ids where the record lists them one by one, and named groups where it groups
them (About me, lab panels, medication lists). What's new and My outcomes are
never shared: the feed summarises every section.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import timedelta, timezone as dt_timezone
from typing import Any, Callable

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from rest_framework import status as http
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import APIException, NotFound
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, UserRateThrottle

from omop_core.models import PatientRecord
from patient_portal.models import RecordShare, ShareScan

from .feed import imaging
from .labs import labs
from .records import genetics, medications, procedures
from .therapy import therapy
from .views import (
    AboutView,
    DiagnosesView,
    GeneticsView,
    ImagingView,
    LabHistoryView,
    LabsView,
    MedicationDetailView,
    MedicationsView,
    PhrView,
    ProceduresView,
    TherapyView,
    about_fields,
    diagnoses,
    patient_name,
    section_status,
)

logger = logging.getLogger(__name__)

DURATIONS = {
    '1h': timedelta(hours=1), '24h': timedelta(hours=24), '1w': timedelta(days=7),
    '1m': timedelta(days=30), '1y': timedelta(days=365),
}
# In the record's order.
SHARE_SECTIONS = ('about', 'diagnoses', 'therapy', 'labs', 'medications', 'procedures', 'genetics', 'imaging')
SECTION_LABELS = {
    'about': 'About me', 'diagnoses': 'Diagnoses', 'therapy': 'Lines of therapy', 'labs': 'Labs',
    'medications': 'Medications', 'procedures': 'Procedures', 'genetics': 'Genetic testing',
    'imaging': 'Imaging testing',
}
ABOUT_GROUPS = {
    'details': ('date_of_birth', 'gender', 'race', 'ethnicity', 'blood_type'),
    'contact': ('phone', 'email', 'address'),
}
ABOUT_LABELS = {'details': 'Personal details', 'contact': 'Contact information and address'}
LAB_LABELS = {
    'markers': 'Cancer markers', 'panel:CBC': 'Blood counts (CBC)',
    'panel:BMP': 'Kidney and minerals (BMP)', 'panel:CMP': 'Liver and protein (CMP)',
    'other': 'Other tests',
}
MEDICATION_LABELS = {'current': 'Current medications', 'past': 'Past medications', 'added': 'Medications you added'}
NAME_MAX, ITEMS_MAX, ITEM_KEY_MAX = 100, 500, 64
# Opening the record again within this window is the same visit, not a new scan.
SCAN_WINDOW = timedelta(minutes=10)

# ---------------------------------------------------------------- Tokens

_PUBLIC_ID_LEN, _MAC_LEN = 12, 22  # 72 random bits to find the row; 132 bits of MAC


def _keys() -> list[bytes]:
    secrets_ = [settings.PHR_SHARE_SECRET] if settings.PHR_SHARE_SECRET else [
        settings.SECRET_KEY, *getattr(settings, 'SECRET_KEY_FALLBACKS', []),
    ]
    return [hashlib.sha256(b'phr-share-link\0' + s.encode()).digest() for s in secrets_ if s]


def _mac(public_id: str, key: bytes) -> str:
    digest = hmac.new(key, public_id.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip('=')[:_MAC_LEN]


def new_public_id() -> str:
    return secrets.token_urlsafe(9)  # 12 characters


def token_for(share: RecordShare) -> str:
    return share.public_id + _mac(share.public_id, _keys()[0])


def share_url(share: RecordShare) -> str:
    return f"{settings.PHR_SHARE_URL.rstrip('/')}/{token_for(share)}"


def share_for_token(token: str | None) -> RecordShare | None:
    """The share a token opens, whatever its state; None for anything forged."""
    if not isinstance(token, str) or len(token) != _PUBLIC_ID_LEN + _MAC_LEN:
        return None
    public_id, mac = token[:_PUBLIC_ID_LEN], token[_PUBLIC_ID_LEN:]
    if not any(hmac.compare_digest(mac, _mac(public_id, key)) for key in _keys()):
        return None
    return RecordShare.objects.filter(public_id=public_id).select_related('person').first()


# ---------------------------------------------------------------- What can be shared

def about_group(key: str) -> str:
    return next((group for group, keys in ABOUT_GROUPS.items() if key in keys), 'details')


def lab_group(test: dict) -> str:
    if test.get('disease_slugs'):
        return 'markers'
    if test.get('panels'):
        return f"panel:{test['panels'][0]}"
    return 'other'


def medication_group(med: dict) -> str:
    return 'added' if med['source']['kind'] == 'patient' else med['status']


def _item(key, label, date=None, members=None, detail=None) -> dict[str, Any]:
    item = {'key': str(key), 'label': label, 'date': date, 'detail': detail, 'members': members}
    return {k: v for k, v in item.items() if v is not None}


def _groups(rows, group_of, member_of, labels) -> list[dict[str, Any]]:
    members: dict[str, list[str]] = {}
    for row in rows:
        members.setdefault(group_of(row), []).append(str(member_of(row)))
    order = list(labels)
    return [
        _item(key, labels.get(key, key.split(':', 1)[-1]), members=keys)
        for key, keys in sorted(members.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else len(order), kv[0]))
    ]


def catalog(person, record) -> list[dict[str, Any]]:
    """Everything the patient can choose to share or print, section by section."""
    dx = diagnoses(person, record)
    sections = {
        'about': _groups(about_fields(record), lambda f: about_group(f['key']), lambda f: f['key'], ABOUT_LABELS),
        'diagnoses': [
            _item(d['id'], d['name'], d.get('date'), detail='cancer') for d in dx['cancer']
        ] + [_item(d['id'], d['name'], d.get('date')) for d in dx['other']],
        'therapy': [
            _item(line['id'], line.get('regimen') or f"Line {line['number']}", line.get('start'),
                  detail=track['diagnosis']['name'])
            for track in therapy(person, record)['tracks'] for line in track['lines']
        ],
        'labs': _groups(labs(person, record)['tests'], lab_group, lambda t: t['id'], LAB_LABELS),
        'medications': _groups(medications(person, record)['medications'], medication_group,
                               lambda m: m['id'], MEDICATION_LABELS),
        'procedures': [_item(p['id'], p['name'], p.get('date')) for p in procedures(person, record)['procedures']],
        'genetics': [_item(t['id'], t['type'], t.get('date')) for t in genetics(person, record)['tests']],
        'imaging': [_item(s['id'], s['name'], s.get('date')) for s in imaging(person, record)['studies']],
    }
    return [
        {'id': section, 'label': SECTION_LABELS[section], 'items': sections[section]}
        for section in SHARE_SECTIONS if sections[section]
    ]


def allowed(selection: dict, section: str) -> Callable[[str], bool] | None:
    """A test for item keys in a shared section; None when nothing in it is shared."""
    entry = (selection or {}).get(section) or {}
    if entry.get('all'):
        return lambda key: True
    keys = {str(k) for k in entry.get('items') or []}
    return keys.__contains__ if keys else None


def clean_selection(raw) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _Invalid({'selection': 'Choose what to share.'})
    out: dict[str, Any] = {}
    for section, entry in raw.items():
        if section not in SHARE_SECTIONS or not isinstance(entry, dict):
            raise _Invalid({'selection': f'{section} cannot be shared.'})
        if entry.get('all') is True:
            out[section] = {'all': True}
            continue
        items = entry.get('items') or []
        if not isinstance(items, list) or len(items) > ITEMS_MAX or not all(
            isinstance(i, (str, int)) and 0 < len(str(i)) <= ITEM_KEY_MAX for i in items
        ):
            raise _Invalid({'selection': f'The items chosen in {SECTION_LABELS[section]} are not valid.'})
        if items:
            out[section] = {'items': sorted({str(i) for i in items})}
    if not out:
        raise _Invalid({'selection': 'Choose at least one item to share.'})
    return out


class _Invalid(Exception):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors


# ---------------------------------------------------------------- The patient's shares

def _place(scan: ShareScan) -> dict[str, str]:
    return {k: v for k, v in (('city', scan.city), ('region', scan.region), ('country', scan.country)) if v}


def share_json(share: RecordShare) -> dict[str, Any]:
    data = {
        'id': share.public_id,
        'recipient': share.recipient,
        'recipient_label': share.get_recipient_display(),
        'name': share.name,
        'method': share.method,
        'url': share_url(share),
        'status': 'expired' if share.is_expired else 'active',
        'created_at': share.created_at.isoformat(),
        'expires_at': share.expires_at.isoformat(),
        'selection': share.selection,
        'scans': [{'at': s.scanned_at.isoformat(), **_place(s)} for s in share.scans.all()],
    }
    if share.email:
        data['email'] = share.email
    return data


def _send_invite(share: RecordShare) -> bool:
    patient = patient_name(share.person)
    first = (share.person.given_name or '').strip() or patient
    product = settings.PHR_SHARE_PRODUCT_NAME
    until = share.expires_at.astimezone(dt_timezone.utc).strftime('%b %-d, %Y at %-I:%M %p UTC')
    subject = f'{patient} shared their {product} health record with you'
    body = (
        f'Hi {share.name},\n\n'
        f'{patient} has shared part of their {product} health record with you. '
        f'You can view it, but not change it, until {until}.\n\n'
        f'Open it here:\n\n  {share_url(share)}\n\n'
        f'Anyone with this link can see what {first} shared, so please don’t forward it. '
        f'{first} can remove your access at any time.\n\n'
        f'If you weren’t expecting this, you can ignore this email.\n'
    )
    try:
        sent = send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [share.email])
    except Exception:
        logger.exception('Could not send a record share invite (share %s)', share.public_id)
        return False
    if sent != 1:
        logger.error('Email backend sent %s invites for share %s', sent, share.public_id)
        return False
    share.last_sent_at = timezone.now()
    share.save(update_fields=['last_sent_at'])
    return True


class ShareEmailThrottle(UserRateThrottle):
    """Caps the invites one patient emails: only requests that send one count."""
    scope = 'phr_share_email'

    def allow_request(self, request, view):
        if not getattr(view, 'sends_email', lambda request: False)(request):
            return True
        return super().allow_request(request, view)


class _PatientShareView(PhrView):
    def get_throttles(self):
        return [*super().get_throttles(), ShareEmailThrottle()]

    def own_share(self, person, share_id: str) -> RecordShare | None:
        return (
            RecordShare.objects.filter(person=person, public_id=share_id, revoked_at__isnull=True)
            .prefetch_related('scans').first()
        )


def _invalid(errors: dict[str, str]) -> Response:
    return Response(errors, status=http.HTTP_400_BAD_REQUEST)


EMAIL_FAILED = {'detail': 'We couldn’t send the invite. Try again in a moment.'}


class SharesView(_PatientShareView):
    """``GET``: who has access (not removed). ``POST``: grant access."""
    http_method_names = ['get', 'post', 'options']

    def sends_email(self, request) -> bool:
        return request.method == 'POST' and request.data.get('method') == RecordShare.EMAIL

    def build(self, person, record):
        shares = RecordShare.objects.filter(person=person, revoked_at__isnull=True).prefetch_related('scans')
        return {'shares': [share_json(s) for s in shares]}

    def post(self, request):
        person, _record, error = self.resolve(request)
        if error:
            return error
        data = request.data
        recipient = data.get('recipient')
        name = str(data.get('name') or '').strip()
        method = RecordShare.QR if recipient == RecordShare.CHECKIN else data.get('method')
        duration = data.get('duration') or '24h'
        email = str(data.get('email') or '').strip()
        errors = {}
        if recipient not in dict(RecordShare.RECIPIENTS):
            errors['recipient'] = 'Choose who it is for.'
        if not name:
            errors['name'] = 'Enter their name.'
        elif len(name) > NAME_MAX:
            errors['name'] = f'Use {NAME_MAX} characters or fewer.'
        if method not in dict(RecordShare.METHODS):
            errors['method'] = 'Choose how to share.'
        if duration not in DURATIONS:
            errors['duration'] = 'Choose how long they can view it.'
        if method == RecordShare.EMAIL:
            try:
                validate_email(email)
            except DjangoValidationError:
                errors['email'] = 'Enter an email address like name@example.com.'
        else:
            email = ''
        try:
            selection = clean_selection(data.get('selection'))
        except _Invalid as exc:
            errors.update(exc.errors)
        if errors:
            return _invalid(errors)

        with transaction.atomic():
            share = RecordShare.objects.create(
                person=person, public_id=new_public_id(), recipient=recipient, name=name, email=email,
                method=method, selection=selection, created_by=request.user,
                expires_at=timezone.now() + DURATIONS[duration],
            )
            if method == RecordShare.EMAIL and not _send_invite(share):
                transaction.set_rollback(True)
                return Response(EMAIL_FAILED, status=http.HTTP_502_BAD_GATEWAY)
        return Response(share_json(share), status=http.HTTP_201_CREATED)


class ShareView(_PatientShareView):
    """``PATCH``: change what they can see. ``DELETE``: remove their access."""
    http_method_names = ['patch', 'delete', 'options']

    def patch(self, request, share_id: str):
        person, _record, error = self.resolve(request)
        if error:
            return error
        share = self.own_share(person, share_id)
        if share is None:
            return Response({'detail': 'Not found.'}, status=404)
        if share.recipient == RecordShare.CHECKIN:
            return _invalid({'selection': 'A check-in code shares a fixed set of records. Create a new share instead.'})
        try:
            share.selection = clean_selection(request.data.get('selection'))
        except _Invalid as exc:
            return _invalid(exc.errors)
        share.save(update_fields=['selection'])
        return Response(share_json(share))

    def delete(self, request, share_id: str):
        person, _record, error = self.resolve(request)
        if error:
            return error
        share = self.own_share(person, share_id)
        if share is None:
            return Response({'detail': 'Not found.'}, status=404)
        share.revoked_at = timezone.now()
        share.save(update_fields=['revoked_at'])
        return Response(status=http.HTTP_204_NO_CONTENT)


class ShareRenewView(_PatientShareView):
    http_method_names = ['post', 'options']

    def post(self, request, share_id: str):
        person, _record, error = self.resolve(request)
        if error:
            return error
        share = self.own_share(person, share_id)
        if share is None:
            return Response({'detail': 'Not found.'}, status=404)
        duration = request.data.get('duration') or '24h'
        if duration not in DURATIONS:
            return _invalid({'duration': 'Choose how long they can view it.'})
        share.expires_at = timezone.now() + DURATIONS[duration]
        share.save(update_fields=['expires_at'])
        return Response(share_json(share))


class ShareResendView(_PatientShareView):
    http_method_names = ['post', 'options']

    def sends_email(self, request) -> bool:
        return True

    def post(self, request, share_id: str):
        person, _record, error = self.resolve(request)
        if error:
            return error
        share = self.own_share(person, share_id)
        if share is None or share.method != RecordShare.EMAIL:
            return Response({'detail': 'Not found.'}, status=404)
        if not share.is_active:
            return _invalid({'detail': 'This access has expired. Renew it first.'})
        if not _send_invite(share):
            return Response(EMAIL_FAILED, status=http.HTTP_502_BAD_GATEWAY)
        return Response(share_json(share))


class ShareOptionsView(PhrView):
    """What can be shared or printed, for the selection tree."""

    def build(self, person, record):
        return {'sections': catalog(person, record)}


# ---------------------------------------------------------------- Opening a shared record

class Gone(APIException):
    status_code = http.HTTP_410_GONE
    default_detail = 'This access has expired.'
    default_code = 'expired'


class ShareAccess:
    """``request.auth`` for a share token; the audit log records it as the client."""

    def __init__(self, share: RecordShare):
        self.share = share

    def __str__(self):
        return f'share:{self.share.public_id}'


class ShareTokenAuthentication(BaseAuthentication):
    def authenticate(self, request):
        token = request.META.get('HTTP_X_SHARE_TOKEN')
        if not token:
            return None
        share = share_for_token(token.strip())
        # A removed share looks like one that never existed.
        if share is None or share.revoked_at is not None:
            raise NotFound('This link doesn’t open a record.')
        if share.is_expired:
            raise Gone({'detail': 'This access has expired.', 'code': 'expired',
                        'expired_at': share.expires_at.isoformat()})
        return AnonymousUser(), ShareAccess(share)


class HasShare(BasePermission):
    def has_permission(self, request, view):
        return isinstance(request.auth, ShareAccess)


class SharedRecordMixin:
    """Turns a section view into its shared, filtered, read-only twin."""
    authentication_classes = [ShareTokenAuthentication]
    permission_classes = [HasShare]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'phr_shared'
    http_method_names = ['get', 'options']
    share_section: str | None = None

    def resolve(self, request):
        self.share = request.auth.share
        if self.share_section and allowed(self.share.selection, self.share_section) is None:
            return None, None, Response({'detail': 'Not shared.'}, status=404)
        person = self.share.person
        return person, PatientRecord.objects.filter(person=person).first(), None

    def build(self, person, record):
        data = super().build(person, record)
        return None if data is None else self.share_filter(data)

    def share_filter(self, data):
        return data

    def is_shared(self, section: str) -> bool:
        return allowed(self.share.selection, section) is not None

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        response['Referrer-Policy'] = 'no-referrer'
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response


def _shared_sections(share: RecordShare) -> list[str]:
    return [s for s in SHARE_SECTIONS if allowed(share.selection, s) is not None]


def _record_scan(request, share: RecordShare) -> None:
    if share.scans.filter(scanned_at__gte=timezone.now() - SCAN_WINDOW).exists():
        return
    place = {
        key: request.META.get('HTTP_' + header.upper().replace('-', '_'), '')[:100]
        for key, header in settings.PHR_SHARE_GEO_HEADERS.items()
    }
    ShareScan.objects.create(share=share, **place)


class SharedRecordView(SharedRecordMixin, PhrView):
    """Who shared, until when, and which sections. Opening it logs a scan."""

    def build(self, person, record):
        share = self.share
        _record_scan(self.request, share)
        return {
            'patient': {'name': patient_name(person), 'first_name': (person.given_name or '').strip() or None},
            'recipient': {'kind': share.recipient, 'name': share.name},
            'expires_at': share.expires_at.isoformat(),
            'sections': _shared_sections(share),
        }


class SharedStatusView(SharedRecordMixin, PhrView):
    def build(self, person, record):
        states = section_status(person, record)
        return {'sections': {s: states[s] for s in _shared_sections(self.share)}}


class SharedAboutView(SharedRecordMixin, AboutView):
    share_section = 'about'

    def share_filter(self, data):
        ok = allowed(self.share.selection, 'about')
        return {**data, 'fields': [f for f in data['fields'] if ok(about_group(f['key']))]}


class SharedDiagnosesView(SharedRecordMixin, DiagnosesView):
    share_section = 'diagnoses'

    def share_filter(self, data):
        ok = allowed(self.share.selection, 'diagnoses')
        return {'cancer': [d for d in data['cancer'] if ok(d['id'])], 'other': [d for d in data['other'] if ok(d['id'])]}


class SharedTherapyView(SharedRecordMixin, TherapyView):
    share_section = 'therapy'

    def share_filter(self, data):
        ok = allowed(self.share.selection, 'therapy')
        tracks = [{**t, 'lines': [l for l in t['lines'] if ok(l['id'])]} for t in data['tracks']]
        return {'tracks': [t for t in tracks if t['lines']]}


class SharedLabsView(SharedRecordMixin, LabsView):
    share_section = 'labs'

    def share_filter(self, data):
        ok = allowed(self.share.selection, 'labs')
        tests = [t for t in data['tests'] if ok(lab_group(t))]
        panels = {p for t in tests for p in t['panels']}
        filters = {
            # A cancer filter names the diagnosis.
            'diagnoses': data['filters']['diagnoses'] if self.is_shared('diagnoses') else [],
            'panels': [p for p in data['filters']['panels'] if p in panels],
        }
        return {'tests': tests, 'filters': filters}


class SharedLabHistoryView(SharedRecordMixin, LabHistoryView):
    share_section = 'labs'

    def share_filter(self, data):
        if not allowed(self.share.selection, 'labs')(lab_group(data)):
            return None
        if not self.is_shared('therapy'):
            return {k: v for k, v in data.items() if k != 'therapy'}
        # Only the lines the patient shared: a marker chart can carry another
        # cancer's lines (a PSA chart, the prostate cancer's).
        ok = allowed(self.share.selection, 'therapy')
        return {**data, 'therapy': [line for line in data.get('therapy', []) if ok(line.get('id'))]}


class SharedMedicationsView(SharedRecordMixin, MedicationsView):
    share_section = 'medications'

    def share_filter(self, data):
        ok = allowed(self.share.selection, 'medications')
        return {'medications': [m for m in data['medications'] if ok(medication_group(m))]}


class SharedMedicationDetailView(SharedRecordMixin, MedicationDetailView):
    share_section = 'medications'

    def share_filter(self, data):
        return data if allowed(self.share.selection, 'medications')(medication_group(data)) else None


class SharedProceduresView(SharedRecordMixin, ProceduresView):
    share_section = 'procedures'

    def share_filter(self, data):
        ok = allowed(self.share.selection, 'procedures')
        return {'procedures': [p for p in data['procedures'] if ok(p['id'])]}


class SharedGeneticsView(SharedRecordMixin, GeneticsView):
    share_section = 'genetics'

    def share_filter(self, data):
        ok = allowed(self.share.selection, 'genetics')
        return {'tests': [t for t in data['tests'] if ok(t['id'])]}


class SharedImagingView(SharedRecordMixin, ImagingView):
    share_section = 'imaging'

    def share_filter(self, data):
        ok = allowed(self.share.selection, 'imaging')
        return {'studies': [s for s in data['studies'] if ok(s['id'])]}
