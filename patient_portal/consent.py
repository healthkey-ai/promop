"""Consent model v2: what a member has agreed to, and recording a change.

Purposes (``ConsentPurpose``) and legal documents (``LegalDocument``) are data,
each with versioned texts per language. A member's answers are an append-only
history (``ConsentEvent``, ``DocumentAcceptance``); their current position on
a purpose is the latest event for it. Every answer is also written to the
``ConsentLedgerEntry`` store, which holds the email address and the decision
and nothing that ties it to the account, so it outlives account deletion.

Enforcement points call ``has_consent(person, key)`` for one member and
``persons_granting(key)`` for a cohort. Everything else here serves the
member's own consent screens through ``api/phr/consent.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from django.conf import settings
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


def record_purpose_key() -> str:
    """The key of the permission that stands for keeping a record at all
    (``CONSENT_RECORD_PURPOSE_KEY``, ``my_record`` by default)."""
    return getattr(settings, 'CONSENT_RECORD_PURPOSE_KEY', 'my_record')


def withdrawal_closes_account(purpose_key: str, decision: str) -> bool:
    """Whether recording ``decision`` on ``purpose_key`` closes the account:
    a withdrawal of the record permission does. The member's consent view
    runs ``account_deletion.delete_member`` in the same transaction as the
    event and its ledger entry, after every decision in the batch is
    recorded, so that the ledger shows the withdrawal that ended the account."""
    return decision == ConsentEvent.WITHDRAWN and purpose_key == record_purpose_key()


# ------------------------------------------------------------------ texts


def _published(rows):
    now = timezone.now()
    return [r for r in rows if r.published_at is not None and r.published_at <= now]


def version_key(version: str) -> tuple:
    """Orders versions numerically where they are numbers: 10.0 after 9.0."""
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in version.split('.'))


def current_version(texts: Iterable) -> str | None:
    """The version that was first published most recently.

    A version's publication is its earliest published row in any language,
    so adding a translation of an older version later does not make that
    version current again. Two versions published at the same instant are
    ordered by version number.
    """
    first_published: dict[str, object] = {}
    for t in _published(texts):
        if t.version not in first_published or t.published_at < first_published[t.version]:
            first_published[t.version] = t.published_at
    if not first_published:
        return None
    return max(first_published, key=lambda v: (first_published[v], version_key(v)))


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
    """``available`` is whether this member can answer the purpose now: it
    has a published text and is not gated to other countries."""
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
    versions: list = field(default_factory=list)

    @property
    def current_version(self) -> str | None:
        return self.text.version if self.text else None

    @property
    def accepted_version(self) -> str | None:
        return self.latest.version if self.latest else None

    @property
    def needs_acceptance(self) -> bool:
        """Never accepted, or a significant change has been published since
        the version accepted (even if a minor one followed it)."""
        if self.text is None:
            return False
        if self.latest is None:
            return True
        if self.latest.version == self.text.version:
            return False
        published = _published(self.versions)
        accepted_at = min(
            (v.published_at for v in published if v.version == self.latest.version), default=None,
        )
        return any(
            v.requires_reacceptance and (accepted_at is None or v.published_at > accepted_at)
            for v in published
        )


def record_country(person) -> str:
    """The member's country as the record holds it (see docs/consent-v2.md
    for where that comes from). Empty when the record has none."""
    record = PatientRecord.objects.filter(person=person).only('country').first()
    return (record.country or '') if record else ''


def _available(purpose: ConsentPurpose, text, country: str) -> bool:
    return text is not None and purpose.available_in(country)


def latest_events(person) -> dict[int, ConsentEvent]:
    """The newest event per purpose for ``person``, in one query."""
    rows = (
        ConsentEvent.objects.filter(person=person)
        .order_by('purpose_id', '-recorded_at', '-id').distinct('purpose_id')
    )
    return {e.purpose_id: e for e in rows}


def purpose_states(person, language: str = DEFAULT_LANGUAGE) -> list[PurposeState]:
    purposes = ConsentPurpose.objects.filter(is_active=True).prefetch_related('texts')
    country = record_country(person)
    latest = latest_events(person)
    states = []
    for p in purposes:
        text = current_text(p.texts.all(), language)
        states.append(PurposeState(purpose=p, available=_available(p, text, country), text=text, latest=latest.get(p.id)))
    return states


def document_states(person, language: str = DEFAULT_LANGUAGE) -> list[DocumentState]:
    rows = (
        DocumentAcceptance.objects.filter(person=person)
        .order_by('document_id', '-accepted_at', '-id').distinct('document_id')
    )
    latest = {a.document_id: a for a in rows}
    return [
        DocumentState(
            document=d, text=current_text(d.versions.all(), language), latest=latest.get(d.id),
            versions=list(d.versions.all()),
        )
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
    latest = latest_events(person).get(purpose.id)
    if latest is None or latest.decision != ConsentEvent.GRANTED:
        return False
    return purpose.available_in(record_country(person))


def persons_granting(key: str) -> set[int]:
    """The ids of every person whose latest answer to the active purpose
    ``key`` is a grant, in two queries, for enforcement over a cohort (a
    mailing, a research export). The country gate is applied per person
    from the record, as ``has_consent`` does."""
    purpose = ConsentPurpose.objects.filter(key=key, is_active=True).first()
    if purpose is None:
        return set()
    latest = (
        ConsentEvent.objects.filter(purpose=purpose)
        .order_by('person_id', '-recorded_at', '-id').distinct('person_id')
    )
    granted = {e.person_id for e in latest if e.decision == ConsentEvent.GRANTED}
    if not granted or not purpose.gated_by_country:
        return granted
    countries = dict(
        PatientRecord.objects.filter(person_id__in=granted).values_list('person_id', 'country'),
    )
    return {pid for pid in granted if purpose.available_in(countries.get(pid))}


def pending_documents(person, language: str = DEFAULT_LANGUAGE) -> list[DocumentState]:
    """Documents the member must confirm before carrying on."""
    return [s for s in document_states(person, language) if s.needs_acceptance]


# ------------------------------------------------------------------ writes


def member_email(person) -> str:
    link = PatientUser.objects.filter(person=person, is_active=True).select_related('identity').first()
    return (link.identity.email or '') if link else ''


NO_EMAIL = 'An email address on the account is needed to record consent.'


def _ledger_email(person, email, what: str) -> str:
    addr = email if email is not None else member_email(person)
    if not addr:
        raise ValidationError(f'{what}: {NO_EMAIL}')
    return addr


class StaleVersion(ValidationError):
    """The member answered a version that is no longer the published one."""


def record_consent(
    person, purpose: ConsentPurpose | str, decision: str, *, version: str | None = None,
    language: str = DEFAULT_LANGUAGE, frequency: str = '', recorded_by=None,
    source: str = ConsentEvent.MEMBER, recorded_at=None, email: str | None = None,
) -> ConsentEvent:
    """Append one answer and its ledger entry, in one transaction.

    A member must name the ``version`` they saw, and it must be the purpose's
    current published version: older wording gets ``StaleVersion`` and the
    client refreshes. A member can only answer a purpose available to them
    (active, and not gated to other countries), and can only withdraw a
    grant that stands. A ``migration``, ``staff`` or ``system`` source may
    pass any version the purpose has, so a decision carried over from an
    earlier form keeps its own version, or none to mean the current one.
    """
    if isinstance(purpose, str):
        purpose = ConsentPurpose.objects.get(key=purpose)
    if source == ConsentEvent.MEMBER:
        if not purpose.is_active:
            raise ValidationError(f'{purpose.key} is not active.')
        if not purpose.available_in(record_country(person)):
            raise ValidationError(f'{purpose.key} is not available in your country.')
    if decision not in purpose.allowed_decisions():
        raise ValidationError(f'{purpose.key} does not take the answer {decision!r}.')
    if decision == ConsentEvent.WITHDRAWN and source == ConsentEvent.MEMBER:
        latest = latest_events(person).get(purpose.id)
        if latest is None or latest.decision != ConsentEvent.GRANTED:
            raise ValidationError(f'{purpose.key} is not granted, so there is nothing to withdraw.')
    texts = list(purpose.texts.all())
    current = current_version(texts)
    if current is None:
        raise ValidationError(f'{purpose.key} has no published text.')
    if version is None:
        if source == ConsentEvent.MEMBER:
            raise ValidationError(f'{purpose.key}: version is required.')
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
    addr = _ledger_email(person, email, purpose.key)
    with transaction.atomic():
        event = ConsentEvent.objects.create(
            person=person, purpose=purpose, text=text, version=version, language=text.language,
            decision=decision, frequency=frequency, source=source, recorded_by=recorded_by, recorded_at=when,
        )
        ConsentLedgerEntry.objects.create(
            email=addr, kind=ConsentLedgerEntry.CONSENT,
            key=purpose.key, version=version, language=text.language, decision=decision,
            frequency=frequency, recorded_at=when,
        )
    return event


def accept_document(
    person, document: LegalDocument | str, *, version: str | None = None,
    language: str = DEFAULT_LANGUAGE, recorded_by=None, source: str = ConsentEvent.MEMBER,
    recorded_at=None, email: str | None = None,
) -> DocumentAcceptance:
    """Append the member's acceptance of a document's current version, which
    the caller names so that stale wording is never accepted.

    Accepting a version already accepted is a no-op that returns the
    existing row, so a client retry does not duplicate the history.
    """
    if isinstance(document, str):
        document = LegalDocument.objects.get(key=document)
    versions = list(document.versions.all())
    current = current_version(versions)
    if current is None:
        raise ValidationError(f'{document.key} has no published version.')
    if version is None:
        raise ValidationError(f'{document.key}: version is required.')
    if version != current:
        raise StaleVersion(f'{document.key} is now version {current}, not {version}.')
    text = text_for(versions, version, language)
    existing = (
        DocumentAcceptance.objects.filter(person=person, document=document)
        .order_by('-accepted_at', '-id').first()
    )
    if existing is not None and existing.version == version:
        return existing

    when = recorded_at or timezone.now()
    addr = _ledger_email(person, email, document.key)
    with transaction.atomic():
        acceptance = DocumentAcceptance.objects.create(
            person=person, document=document, text=text, version=version, language=text.language,
            source=source, recorded_by=recorded_by, accepted_at=when,
        )
        ConsentLedgerEntry.objects.create(
            email=addr, kind=ConsentLedgerEntry.DOCUMENT,
            key=document.key, version=version, language=text.language, decision=ACCEPTED, recorded_at=when,
        )
    return acceptance
