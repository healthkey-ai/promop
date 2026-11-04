"""Consent model v2: what a member has agreed to, and recording a change.

Purposes (``ConsentPurpose``) and legal documents (``LegalDocument``) are data,
each with versioned texts per language. A member's answers are an append-only
history (``ConsentEvent``, ``DocumentAcceptance``); their current position on
a purpose is the latest event for it. Every answer is also written to the
``ConsentLedgerEntry`` store, which holds the email address and the decision
and nothing that ties it to the account, so it outlives account deletion.

Enforcement points call ``has_consent(person, key)``. Everything else here
serves the member's own consent screens through ``api/phr/consent.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from omop_core.models import PatientRecord
from patient_portal.models import (
    ConsentEvent,
    ConsentLedgerEntry,
    ConsentPurpose,
    ConsentText,
    DocumentAcceptance,
    LegalDocument,
    LegalDocumentVersion,
    PatientUser,
)

DEFAULT_LANGUAGE = 'en'
ACCEPTED = 'accepted'


# ------------------------------------------------------------------ texts


def _published(rows):
    now = timezone.now()
    return [r for r in rows if r.published_at is not None and r.published_at <= now]


def current_version(texts: Iterable) -> str | None:
    """The version published most recently, across languages."""
    published = _published(texts)
    if not published:
        return None
    return max(published, key=lambda t: (t.published_at, t.id)).version


def text_for(texts: Iterable, version: str | None, language: str):
    """The text of ``version`` in ``language``, falling back to the default
    language and then to any language, so a missing translation shows the
    wording that exists rather than nothing."""
    if version is None:
        return None
    candidates = [t for t in _published(texts) if t.version == version]
    for lang in (language, DEFAULT_LANGUAGE):
        for t in candidates:
            if t.language == lang:
                return t
    return candidates[0] if candidates else None


def current_text(texts: Iterable, language: str):
    return text_for(texts, current_version(texts), language)


# ------------------------------------------------------------------ state


@dataclass
class PurposeState:
    purpose: ConsentPurpose
    available: bool
    text: ConsentText | None
    latest: ConsentEvent | None

    @property
    def current_version(self) -> str | None:
        return self.text.version if self.text else None

    @property
    def decision(self) -> str | None:
        return self.latest.decision if self.latest else None

    @property
    def granted(self) -> bool:
        return self.available and self.decision == ConsentEvent.GRANTED

    @property
    def outdated(self) -> bool:
        """Answered on an earlier version than the one now published."""
        return bool(self.latest and self.current_version and self.latest.version != self.current_version)


@dataclass
class DocumentState:
    document: LegalDocument
    text: LegalDocumentVersion | None
    latest: DocumentAcceptance | None

    @property
    def current_version(self) -> str | None:
        return self.text.version if self.text else None

    @property
    def accepted_version(self) -> str | None:
        return self.latest.version if self.latest else None

    @property
    def needs_acceptance(self) -> bool:
        """Never accepted, or the published version is a significant change
        since the one accepted."""
        if self.text is None:
            return False
        if self.latest is None:
            return True
        if self.latest.version == self.text.version:
            return False
        return self.text.requires_reacceptance


def record_country(person) -> str:
    record = PatientRecord.objects.filter(person=person).only('country').first()
    return (record.country or '') if record else ''


def latest_events(person) -> dict[int, ConsentEvent]:
    """The newest event per purpose for ``person``."""
    out: dict[int, ConsentEvent] = {}
    for event in ConsentEvent.objects.filter(person=person).order_by('recorded_at', 'id'):
        out[event.purpose_id] = event
    return out


def purpose_states(person, language: str = DEFAULT_LANGUAGE, *, include_inactive=False) -> list[PurposeState]:
    purposes = ConsentPurpose.objects.prefetch_related('texts')
    if not include_inactive:
        purposes = purposes.filter(is_active=True)
    country = record_country(person)
    latest = latest_events(person)
    return [
        PurposeState(
            purpose=p, available=p.available_in(country),
            text=current_text(p.texts.all(), language), latest=latest.get(p.id),
        )
        for p in purposes
    ]


def document_states(person, language: str = DEFAULT_LANGUAGE) -> list[DocumentState]:
    latest: dict[int, DocumentAcceptance] = {}
    for a in DocumentAcceptance.objects.filter(person=person).order_by('accepted_at', 'id'):
        latest[a.document_id] = a
    return [
        DocumentState(document=d, text=current_text(d.versions.all(), language), latest=latest.get(d.id))
        for d in LegalDocument.objects.filter(is_active=True).prefetch_related('versions')
    ]


def has_consent(person, key: str) -> bool:
    """Whether ``person`` currently grants the purpose ``key``.

    False when there is no such purpose, it is inactive, the member never
    answered, their latest answer is not a grant, or the purpose is gated to
    countries that do not include the member's.
    """
    purpose = ConsentPurpose.objects.filter(key=key, is_active=True).first()
    if purpose is None:
        return False
    latest = (
        ConsentEvent.objects.filter(person=person, purpose=purpose)
        .order_by('-recorded_at', '-id').first()
    )
    if latest is None or latest.decision != ConsentEvent.GRANTED:
        return False
    return purpose.available_in(record_country(person))


def pending_documents(person, language: str = DEFAULT_LANGUAGE) -> list[DocumentState]:
    """Documents the member must confirm before carrying on."""
    return [s for s in document_states(person, language) if s.needs_acceptance]


# ------------------------------------------------------------------ writes


def member_email(person) -> str:
    link = PatientUser.objects.filter(person=person).select_related('identity').first()
    return (link.identity.email or '') if link else ''


class StaleVersion(ValidationError):
    """The member answered a version that is no longer the published one."""


def record_consent(
    person, purpose: ConsentPurpose | str, decision: str, *, version: str | None = None,
    language: str = DEFAULT_LANGUAGE, frequency: str = '', recorded_by=None,
    source: str = ConsentEvent.MEMBER, recorded_at=None, email: str | None = None,
) -> ConsentEvent:
    """Append one answer and its ledger entry, in one transaction.

    ``version`` must be the purpose's current published version: a client
    that shows older wording gets ``StaleVersion`` and must refresh. A
    ``migration`` or ``staff`` source may pass any version the purpose has,
    so a decision carried over from an earlier form keeps its own version.
    """
    if isinstance(purpose, str):
        purpose = ConsentPurpose.objects.get(key=purpose)
    if decision not in purpose.allowed_decisions():
        raise ValidationError(f'{purpose.key} does not take the answer {decision!r}.')
    texts = list(purpose.texts.all())
    current = current_version(texts)
    if current is None:
        raise ValidationError(f'{purpose.key} has no published text.')
    if version is None:
        version = current
    if version != current and source == ConsentEvent.MEMBER:
        raise StaleVersion(f'{purpose.key} is now version {current}, not {version}.')
    text = text_for(texts, version, language)
    if text is None:
        raise ValidationError(f'{purpose.key} has no published version {version}.')

    if decision == ConsentEvent.GRANTED and purpose.asks_frequency:
        if frequency not in purpose.frequency_options:
            raise ValidationError(f'{purpose.key} needs a frequency from {purpose.frequency_options}.')
    else:
        frequency = ''

    when = recorded_at or timezone.now()
    with transaction.atomic():
        event = ConsentEvent.objects.create(
            person=person, purpose=purpose, text=text, version=version, language=text.language,
            decision=decision, frequency=frequency, source=source, recorded_by=recorded_by, recorded_at=when,
        )
        ConsentLedgerEntry.objects.create(
            email=email if email is not None else member_email(person), kind=ConsentLedgerEntry.CONSENT,
            key=purpose.key, version=version, language=text.language, decision=decision,
            frequency=frequency, recorded_at=when,
        )
    return event


def accept_document(
    person, document: LegalDocument | str, *, version: str | None = None,
    language: str = DEFAULT_LANGUAGE, recorded_by=None, recorded_at=None, email: str | None = None,
) -> DocumentAcceptance:
    """Append the member's acceptance of a document's current version."""
    if isinstance(document, str):
        document = LegalDocument.objects.get(key=document)
    versions = list(document.versions.all())
    current = current_version(versions)
    if current is None:
        raise ValidationError(f'{document.key} has no published version.')
    if version is None:
        version = current
    if version != current:
        raise StaleVersion(f'{document.key} is now version {current}, not {version}.')
    text = text_for(versions, version, language)

    when = recorded_at or timezone.now()
    with transaction.atomic():
        acceptance = DocumentAcceptance.objects.create(
            person=person, document=document, version=version, language=text.language,
            recorded_by=recorded_by, accepted_at=when,
        )
        ConsentLedgerEntry.objects.create(
            email=email if email is not None else member_email(person), kind=ConsentLedgerEntry.DOCUMENT,
            key=document.key, version=version, language=text.language, decision=ACCEPTED, recorded_at=when,
        )
    return acceptance
