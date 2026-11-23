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
record carries both already. Whether the declarations are required is the
record's organisation's call (``Organization.requires_signup_declarations``):
a member app's org requires every sign-up declaration and document, because
the declaration is the age check (``signup_policy``); a hospital's org, whose
own claim page asks none, records whatever the form sends and requires none.
One promop instance serves both.
"""
from __future__ import annotations

from datetime import date

from django.core.exceptions import ValidationError

from omop_core.models import PERSON_YEAR_PLACEHOLDERS
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
                'requires': p.requires or None,
                'gated_by_country': p.gated_by_country,
                'enabled_countries': [str(c).upper() for c in p.enabled_countries] if p.gated_by_country else [],
                'text': _text(t),
            }
            for p, t in _published_purposes(language)
        ],
        'documents': [
            {
                'key': d.key, 'name': d.name,
                'text': _text(t),
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


_string = svc.required_string
_text = svc.text_payload


def apply_signup(person, record, *, email: str, data: dict, language: str = svc.DEFAULT_LANGUAGE,
                 recorded_by=None, today: date | None = None, refresh=None,
                 require_declarations: bool | None = None) -> dict:
    """Validate and write the sign-up form. Returns what was written, as
    ``{'country': ..., 'year_of_birth': ..., 'consents': n, 'documents': n}``.

    ``data`` is the request body: ``country``, ``year_of_birth`` and
    ``consent`` (``{"decisions": [...], "documents": [...]}`` in the PHR
    consent API's shape). Raises ``ValidationError`` (a 400) or
    ``svc.StaleVersion`` (a 409: the form showed wording that has since been
    replaced). Must run inside the caller's transaction. ``refresh`` is
    called after the country and year are written and before the consent is
    recorded, so a purpose gated by country sees the country just given.
    ``require_declarations`` defaults to the record's organisation's
    ``requires_signup_declarations``; False records what is sent and
    requires nothing.
    """
    if require_declarations is None:
        org = getattr(record, 'organization', None) if record is not None else None
        require_declarations = bool(org is not None and org.requires_signup_declarations)
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
    # Everything a refusal can name is checked here, before a fact is written
    # or the record derived: unknown keys, malformed items, stale versions, and
    # what the organisation requires.
    for key, item in decided.items():
        purpose = purposes.get(key)
        if purpose is None:
            raise ValidationError(f'Unknown purpose {key!r}.')
        _string(item.get('decision'), 'decision')
        version = _string(item.get('version'), 'version')
        current = svc.current_version(list(purpose.texts.all()))
        if current is not None and version != current:
            raise svc.StaleVersion(f'{key} is now version {current}, not {version}.')
    for key, item in accepted.items():
        document = documents.get(key)
        if document is None:
            raise ValidationError(f'Unknown document {key!r}.')
        version = _string(item.get('version'), 'version')
        current = svc.current_version(list(document.versions.all()))
        if current is not None and version != current:
            raise svc.StaleVersion(f'{key} is now version {current}, not {version}.')

    # Where the organisation requires it, every sign-up declaration available
    # to this member must be confirmed and every document accepted.
    if require_declarations:
        effective_country = written['country'] or svc.record_country(person)
        for purpose in purposes.values():
            if purpose.stage != SIGN_UP_STAGE or not purpose.available_in(effective_country):
                continue
            if svc.current_text(list(purpose.texts.all()), language) is None:
                continue
            item = decided.get(purpose.key)
            if item is None:
                raise ValidationError(f'{purpose.key} must be answered to sign up.')
            if purpose.kind == ConsentPurpose.DECLARATION and item.get('decision') != ConsentEvent.GRANTED:
                raise ValidationError(f'{purpose.key} must be confirmed to sign up.')
        for document in documents.values():
            if svc.current_text(list(document.versions.all()), language) is None:
                continue
            if document.key not in accepted:
                raise ValidationError(f'{document.key} must be accepted to sign up.')

    # The sign-up is the member's own, newer statement: a pending UI edit of
    # the same field on the record would otherwise be restored over the
    # derived value and leave the record's country disagreeing with the
    # Location the consent gate was checked against.
    if record is not None:
        superseded = set()
        if written['country'] is not None:
            superseded.add('country')
        if written['year_of_birth'] is not None:
            superseded |= {'date_of_birth', 'date_of_birth_precision'}
        pending = set(record.user_edited_fields or [])
        if pending & superseded:
            record.user_edited_fields = sorted(pending - superseded)
            record.save(update_fields=['user_edited_fields'])

    if written['country'] is not None:
        write_person_location(person, {'country': written['country']})
    year = written['year_of_birth']
    if year is not None and (person.year_of_birth in PERSON_YEAR_PLACEHOLDERS or person.year_of_birth != year):
        # A year the record already holds, from a hospital, keeps its month and
        # day: the member's year is support for the declaration, not a
        # correction. A different year is the member's own statement about
        # themselves and replaces the stored one, year-only.
        person.year_of_birth = year
        person.month_of_birth = None
        person.day_of_birth = None
        person.save(update_fields=['year_of_birth', 'month_of_birth', 'day_of_birth'])
    if refresh is not None:
        refresh(person)

    written['consents'] = len(svc.record_decisions(
        person, [(purposes[key], item) for key, item in decided.items()],
        language=language, recorded_by=recorded_by, email=email,
    ))
    for key, item in accepted.items():
        svc.accept_document(
            person, documents[key], version=item['version'], language=language,
            recorded_by=recorded_by, source=ConsentEvent.MEMBER, email=email,
        )
        written['documents'] += 1
    return written
