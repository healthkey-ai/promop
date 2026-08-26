"""Regimen/drug concept resolution for inbound data (issue #236, ADR 0001).

promop is the governed source of coded vocabulary data; downstream consumers
mirror the concept table.  It must therefore be impossible for an ingest path
to fabricate rows that claim membership in a governed vocabulary.  This module
is the single choke point for resolving a regimen/drug *name* to a concept:

  1. ``validate_hemonc_regimen`` — is this concept a currently-valid standard
     HemOnc Regimen?  (Used to vet inbound concept_ids.)
  2. ``match_hemonc_regimen_by_name`` — exact match against HemOnc regimen
     names and synonyms, restricted to validated rows.
  3. ``get_or_create_quarantine_regimen`` / ``get_or_create_quarantine_drug`` /
     ``get_or_create_quarantine_observation`` /
     ``get_or_create_quarantine_procedure`` — mint under the local
     HK-Regimen / HK-Drug / HK-Observation / HK-Procedure quarantine
     vocabularies (``standard_concept=None``, ``source='HealthKey'``) and
     record a ``RegimenMappingGap`` row so the unmatched name is visible for
     curation.

Nothing in this module ever writes to the HemOnc vocabulary or to any other
licensed/external vocabulary (RxNorm, CVX, LOINC, SNOMED, ...).
"""
import hashlib
import logging
import re
import unicodedata
from datetime import date

from django.db.models import F, Q

from omop_core.models import (
    Concept,
    ConceptClass,
    Domain,
    RegimenMappingGap,
    Vocabulary,
)
from omop_core.services.pk import next_pk

logger = logging.getLogger(__name__)

HEMONC_VOCAB_ID = 'HemOnc'
REGIMEN_CONCEPT_CLASS_ID = 'Regimen'
HK_REGIMEN_VOCAB_ID = 'HK-Regimen'
HK_DRUG_VOCAB_ID = 'HK-Drug'
HK_OBSERVATION_VOCAB_ID = 'HK-Observation'
HK_PROCEDURE_VOCAB_ID = 'HK-Procedure'
SOURCE_HEALTHKEY = 'HealthKey'

# HK-Regimen / HK-Drug are seeded by migration 0118; the others are created
# here on first use (same on-demand pattern as HK-Labs in lab_results/sync.py).
_HK_VOCAB_DEFAULTS = {
    HK_REGIMEN_VOCAB_ID: 'HealthKey local regimen quarantine',
    HK_DRUG_VOCAB_ID: 'HealthKey local drug quarantine',
    HK_OBSERVATION_VOCAB_ID: 'HealthKey local observation quarantine',
    HK_PROCEDURE_VOCAB_ID: 'HealthKey local procedure quarantine',
}


def normalize_regimen_name(name):
    """Lowercase, strip, and collapse internal whitespace — the dedupe key used
    for RegimenMappingGap rows."""
    return re.sub(r'\s+', ' ', (name or '').strip().lower())


def _slug(name, prefix):
    """Normalize a name to a stable concept_code slug: '<prefix>:<slug>'."""
    s = unicodedata.normalize('NFKD', (name or '').lower())
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return f'{prefix}:{s}'[:50]


def validate_hemonc_regimen(concept):
    """True iff *concept* is a currently-valid standard HemOnc Regimen.

    Inbound concept_ids (e.g. embedded in a FHIR bundle) must pass this check
    before they are persisted anywhere — a stale, deprecated, non-standard,
    non-regimen, or foreign-vocabulary id is rejected.
    """
    if concept is None:
        return False
    return (
        concept.vocabulary_id == HEMONC_VOCAB_ID
        and concept.concept_class_id == REGIMEN_CONCEPT_CLASS_ID
        and concept.standard_concept == 'S'
        and not concept.invalid_reason
    )


def _valid_hemonc_regimen_qs():
    """Validated HemOnc regimens.  invalid_reason may be NULL or '' depending
    on the load path — exclude anything truthy (keep in sync with
    validate_hemonc_regimen)."""
    return Concept.objects.filter(
        Q(invalid_reason__isnull=True) | Q(invalid_reason=''),
        vocabulary_id=HEMONC_VOCAB_ID,
        concept_class_id=REGIMEN_CONCEPT_CLASS_ID,
        standard_concept='S',
    )


def match_hemonc_regimen_by_name(name):
    """Exact (case-insensitive) match on HemOnc regimen concept_name, falling
    back to concept_synonym rows.  Only validated regimens are eligible.

    Ordered by concept_id so duplicate-name hits resolve deterministically.
    """
    normalized = (name or '').strip()
    if not normalized:
        return None
    qs = _valid_hemonc_regimen_qs().order_by('concept_id')
    concept = qs.filter(concept_name__iexact=normalized).first()
    if concept:
        return concept
    return qs.filter(synonyms__concept_synonym_name__iexact=normalized).first()


def record_mapping_gap(*, source_system, source_value, matched_concept=None,
                       quarantine_concept=None, status=None):
    """Idempotently record a mapping gap.

    First sighting creates the row; repeat sightings bump ``last_seen`` and
    ``occurrence_count``.  Safe to call on every unmatched ingest.
    """
    # normalized_name is capped at 255 chars — truncate so over-long source
    # values fold into one row instead of raising DataError.
    normalized = normalize_regimen_name(source_value)[:255]
    if not normalized:
        return None
    gap, created = RegimenMappingGap.objects.get_or_create(
        source_system=source_system,
        normalized_name=normalized,
        defaults={
            'source_value': (source_value or '')[:255],
            'matched_concept': matched_concept,
            'quarantine_concept': quarantine_concept,
            'status': status or RegimenMappingGap.STATUS_UNMATCHED,
        },
    )
    if created:
        return gap

    update_fields = ['last_seen', 'occurrence_count']
    # F() keeps the increment atomic under concurrent ingest workers.
    gap.occurrence_count = F('occurrence_count') + 1
    if matched_concept is not None and gap.matched_concept_id != matched_concept.concept_id:
        gap.matched_concept = matched_concept
        update_fields.append('matched_concept')
    if quarantine_concept is not None and gap.quarantine_concept_id != quarantine_concept.concept_id:
        gap.quarantine_concept = quarantine_concept
        update_fields.append('quarantine_concept')
    if status is not None and gap.status == RegimenMappingGap.STATUS_UNMATCHED and status != RegimenMappingGap.STATUS_UNMATCHED:
        gap.status = status
        update_fields.append('status')
    gap.save(update_fields=update_fields)
    return gap


def _ensure_hk_vocab(vocabulary_id):
    vocab, _ = Vocabulary.objects.get_or_create(
        vocabulary_id=vocabulary_id,
        defaults={
            'vocabulary_name': _HK_VOCAB_DEFAULTS[vocabulary_id],
            'vocabulary_reference': 'https://healthkey.ai',
            'vocabulary_version': '1.0',
            'vocabulary_concept_id': 0,
        },
    )
    return vocab


def _get_or_create_quarantine_concept(*, vocabulary_id, domain_id,
                                      concept_class_id, concept_code,
                                      concept_name):
    concept = Concept.objects.filter(
        vocabulary_id=vocabulary_id, concept_code=concept_code,
    ).first()
    if concept is not None:
        if normalize_regimen_name(concept.concept_name) == normalize_regimen_name(concept_name):
            return concept
        # Slug collision: a different name already owns this code (punctuation
        # collapse / 50-char truncation can map distinct names to one slug).
        # Disambiguate with a short hash of the normalized name so the new
        # name gets its own stable concept row.
        # Not a security use: this only disambiguates a slug collision so two
        # distinct regimen names get distinct concept rows. Flagged explicitly so
        # the SAST gate does not have to carry it as a baselined false positive.
        digest = hashlib.sha1(
            normalize_regimen_name(concept_name).encode('utf-8'),
            usedforsecurity=False,
        ).hexdigest()[:8]
        concept_code = f'{concept_code[:41]}-{digest}'[:50]
        concept = Concept.objects.filter(
            vocabulary_id=vocabulary_id, concept_code=concept_code,
        ).first()
        if concept is not None:
            return concept

    domain, _ = Domain.objects.get_or_create(
        domain_id=domain_id,
        defaults={'domain_name': domain_id, 'domain_concept_id': 0},
    )
    vocabulary = _ensure_hk_vocab(vocabulary_id)
    concept_class, _ = ConceptClass.objects.get_or_create(
        concept_class_id=concept_class_id,
        defaults={
            'concept_class_name': concept_class_id,
            'concept_class_concept_id': 0,
        },
    )
    return Concept.objects.create(
        concept_id=next_pk(Concept, 'concept_id'),
        concept_name=concept_name[:255],
        domain=domain,
        vocabulary=vocabulary,
        concept_class=concept_class,
        standard_concept=None,
        source=SOURCE_HEALTHKEY,
        concept_code=concept_code,
        valid_start_date=date(1970, 1, 1),
        valid_end_date=date(2099, 12, 31),
    )


def get_or_create_quarantine_regimen(name, source_system='fhir-upload'):
    """Mint (or fetch) a quarantine regimen concept under HK-Regimen for an
    unmatched regimen name, and record the mapping gap.

    NEVER writes to the HemOnc vocabulary.
    """
    name = (name or '').strip()
    if not name:
        return None
    concept = _get_or_create_quarantine_concept(
        vocabulary_id=HK_REGIMEN_VOCAB_ID,
        domain_id='Drug',
        concept_class_id=REGIMEN_CONCEPT_CLASS_ID,
        concept_code=_slug(name, 'hkr'),
        concept_name=name,
    )
    record_mapping_gap(
        source_system=source_system,
        source_value=name,
        quarantine_concept=concept,
        status=RegimenMappingGap.STATUS_UNMATCHED,
    )
    return concept


def get_or_create_quarantine_drug(*, source_vocabulary_id, concept_code,
                                  concept_name, source_system='fhir-upload'):
    """Mint (or fetch) a quarantine drug concept under HK-Drug for a drug
    code/name that matched nothing in the licensed vocabularies, and record
    the mapping gap.

    NEVER mints under the source (licensed) vocabulary — RxNorm/CVX/LOINC/
    SNOMED rows only ever come from the vocabulary loader.
    """
    name = (concept_name or concept_code or '').strip()
    if not name:
        return None
    raw = f"{(source_vocabulary_id or 'unknown').lower()}-{(concept_code or name)}"
    concept = _get_or_create_quarantine_concept(
        vocabulary_id=HK_DRUG_VOCAB_ID,
        domain_id='Drug',
        concept_class_id='Clinical Drug',
        concept_code=_slug(raw, 'hkd'),
        concept_name=name,
    )
    record_mapping_gap(
        source_system=source_system,
        source_value=name,
        quarantine_concept=concept,
        status=RegimenMappingGap.STATUS_UNMATCHED,
    )
    return concept


def get_or_create_quarantine_observation(*, source_vocabulary_id, concept_code,
                                         concept_name, source_system='fhir-upload'):
    """Mint (or fetch) a quarantine observation concept under HK-Observation
    for a DiagnosticReport/observation code that matched nothing in the
    licensed vocabularies, and record the mapping gap.

    NEVER mints under the source (licensed) vocabulary — LOINC/SNOMED rows
    only ever come from the vocabulary loader.
    """
    name = (concept_name or concept_code or '').strip()
    if not name:
        return None
    raw = f"{(source_vocabulary_id or 'unknown').lower()}-{(concept_code or name)}"
    concept = _get_or_create_quarantine_concept(
        vocabulary_id=HK_OBSERVATION_VOCAB_ID,
        domain_id='Observation',
        concept_class_id='Clinical Observation',
        concept_code=_slug(raw, 'hko'),
        concept_name=name,
    )
    record_mapping_gap(
        source_system=source_system,
        source_value=name,
        quarantine_concept=concept,
        status=RegimenMappingGap.STATUS_UNMATCHED,
    )
    return concept


def get_or_create_quarantine_procedure(*, source_vocabulary_id, concept_code,
                                       concept_name, source_system='fhir-upload'):
    """Mint (or fetch) a quarantine procedure concept under HK-Procedure for a
    procedure code/name that matched nothing in the licensed vocabularies, and
    record the mapping gap.

    NEVER mints under the source (licensed) vocabulary — SNOMED/CPT4/HCPCS
    rows only ever come from the vocabulary loader.
    """
    name = (concept_name or concept_code or '').strip()
    if not name:
        return None
    raw = f"{(source_vocabulary_id or 'unknown').lower()}-{(concept_code or name)}"
    concept = _get_or_create_quarantine_concept(
        vocabulary_id=HK_PROCEDURE_VOCAB_ID,
        domain_id='Procedure',
        concept_class_id='Procedure',
        concept_code=_slug(raw, 'hkp'),
        concept_name=name,
    )
    record_mapping_gap(
        source_system=source_system,
        source_value=name,
        quarantine_concept=concept,
        status=RegimenMappingGap.STATUS_UNMATCHED,
    )
    return concept
