"""The sign-up form's answers, written when a member claims their account.

A member signing up gives their **country**, their **year of birth** and the
**declarations** and **document acceptances** the deployment asks for at
sign-up (consent purposes with ``stage == 'sign_up'`` and every active legal
document). ``apply_signup`` validates all of it against the sign-up policy and
the consent texts, writes the country and year as OMOP facts (Location and
Person; the record is derived from them) and records every answer through
consent v2 in the caller's transaction, so an account is never created with
half its declarations.

The country and year are optional at the API: an invitation to a hospital
record carries both already. The declarations are not: when a deployment has
loaded sign-up purposes or documents, an acceptance that omits one is refused,
because the declaration is the age check (``signup_policy``).
"""
from __future__ import annotations

from datetime import date

from django.core.exceptions import ValidationError

from omop_core.services.field_write_service import write_person_location
from patient_portal import consent as svc
from patient_portal import signup_policy as policy
from patient_portal.models import ConsentEvent, ConsentPurpose, LegalDocument

SIGN_UP_STAGE = 'sign_up'


def _published_purposes(language: str):
    """(purpose, current text) for every active sign-up purpose with a published text."""
    rows = []
    for purpose in ConsentPurpose.objects.filter(is_active=True, stage=SIGN_UP_STAGE).prefetch_related('texts'):
        text = svc.current_text(list(purpose.texts.all()), language)
        if text is not None:
            rows.append((purpose, text))
    return rows


def _published_documents(language: str):
    """(document, current version text) for every active document with a published version."""
    rows = []
    for document in LegalDocument.objects.filter(is_active=True).prefetch_related('versions'):
        text = svc.current_text(list(document.versions.all()), language)
        if text is not None:
            rows.append((document, text))
    return rows


def signup_policy(language: str = svc.DEFAULT_LANGUAGE) -> dict:
    """What a sign-up form shows: the countries with their minimum ages, the
    sign-up declarations with their current wording and version, and the
    documents to accept. Public; it names no member."""
    return {
        'countries': policy.countries(),
        'purposes': [
            {
                'key': p.key, 'kind': p.kind, 'answers': p.allowed_decisions(), 'stage': p.stage,
                'gated_by_country': p.gated_by_country,
                'enabled_countries': [str(c).upper() for c in p.enabled_countries] if p.gated_by_country else [],
                'text': {'version': t.version, 'language': t.language, 'title': t.title,
                         'summary': t.summary, 'body': t.body},
            }
            for p, t in _published_purposes(language)
        ],
        'documents': [
            {
                'key': d.key, 'name': d.name,
                'text': {'version': t.version, 'language': t.language, 'title': t.title,
                         'summary': t.summary, 'body': t.body},
            }
            for d, t in _published_documents(language)
        ],
    }


def _items(value, name: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(i, dict) for i in value):
        raise ValidationError(f'{name} must be a list of objects.')
    return value


def _string(value, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f'{name} is required and must be a string.')
    return value


def apply_signup(person, record, *, email: str, data: dict, language: str = svc.DEFAULT_LANGUAGE,
                 recorded_by=None, today: date | None = None, refresh=None) -> dict:
    """Validate and write the sign-up form. Returns what was written, as
    ``{'country': ..., 'year_of_birth': ..., 'consents': n, 'documents': n}``.

    ``data`` is the request body: ``country``, ``year_of_birth`` and
    ``consent`` (``{"decisions": [...], "documents": [...]}`` in the PHR
    consent API's shape). Raises ``ValidationError`` (a 400) or
    ``svc.StaleVersion`` (a 409: the form showed wording that has since been
    replaced). Must run inside the caller's transaction. ``refresh`` is
    called after the country and year are written and before the consent is
    recorded, so a purpose gated by country sees the country just given.
    """
    written = {'country': None, 'year_of_birth': None, 'consents': 0, 'documents': 0}
    country = data.get('country')
    year = data.get('year_of_birth')
    if country is not None:
        written['country'] = policy.validate_country(country)
    if year is not None:
        if written['country'] is None:
            raise ValidationError('country is required with year_of_birth.')
        written['year_of_birth'] = policy.validate_year_of_birth(year, written['country'], today)

    consent = data.get('consent')
    if consent is None:
        consent = {}
    if not isinstance(consent, dict):
        raise ValidationError('consent must be an object.')
    decisions = _items(consent.get('decisions'), 'consent.decisions')
    acceptances = _items(consent.get('documents'), 'consent.documents')
    decided = {}
    for item in decisions:
        key = _string(item.get('purpose'), 'purpose')
        if key in decided:
            raise ValidationError('A sign-up names each purpose once.')
        decided[key] = item
    accepted = {}
    for item in acceptances:
        key = _string(item.get('document'), 'document')
        if key in accepted:
            raise ValidationError('A sign-up names each document once.')
        accepted[key] = item

    purposes = {p.key: p for p in ConsentPurpose.objects.filter(is_active=True).prefetch_related('texts')}
    documents = {d.key: d for d in LegalDocument.objects.filter(is_active=True).prefetch_related('versions')}
    for key in decided:
        if key not in purposes:
            raise ValidationError(f'Unknown purpose {key!r}.')
    for key in accepted:
        if key not in documents:
            raise ValidationError(f'Unknown document {key!r}.')

    # Every sign-up declaration available to this member must be confirmed, and
    # every document accepted. Checked before anything is written so the
    # refusal names what is missing.
    effective_country = written['country'] or svc.record_country(person)
    for purpose, _text in _published_purposes(language):
        if not purpose.available_in(effective_country):
            continue
        item = decided.get(purpose.key)
        if item is None:
            raise ValidationError(f'{purpose.key} must be answered to sign up.')
        if purpose.kind == ConsentPurpose.DECLARATION and item.get('decision') != ConsentEvent.GRANTED:
            raise ValidationError(f'{purpose.key} must be confirmed to sign up.')
    for document, _text in _published_documents(language):
        if document.key not in accepted:
            raise ValidationError(f'{document.key} must be accepted to sign up.')

    if written['country'] is not None:
        write_person_location(person, {'country': written['country']})
    if written['year_of_birth'] is not None:
        person.year_of_birth = written['year_of_birth']
        person.month_of_birth = None
        person.day_of_birth = None
        person.save(update_fields=['year_of_birth', 'month_of_birth', 'day_of_birth'])
    if refresh is not None:
        refresh(person)

    for key, item in decided.items():
        frequency = item.get('frequency') or ''
        svc.record_consent(
            person, purposes[key], _string(item.get('decision'), 'decision'),
            version=_string(item.get('version'), 'version'), language=language,
            frequency=frequency if isinstance(frequency, str) else '',
            recorded_by=recorded_by, source=ConsentEvent.MEMBER, email=email,
        )
        written['consents'] += 1
    for key, item in accepted.items():
        svc.accept_document(
            person, documents[key], version=_string(item.get('version'), 'version'), language=language,
            recorded_by=recorded_by, source=ConsentEvent.MEMBER, email=email,
        )
        written['documents'] += 1
    return written
