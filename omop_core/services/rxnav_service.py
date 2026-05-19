import json
import logging
import urllib.parse
import urllib.request
from datetime import date

from omop_core.models import Concept, ConceptClass, Domain, Vocabulary

logger = logging.getLogger('audit')

RXNAV_BASE = 'https://rxnav.nlm.nih.gov/REST'


def resolve_drug(drug_source_value: str):
    """
    Resolve a drug name to an OMOP Concept.

    Checks local Concept table first (RxNorm vocabulary, name or concept_code match).
    Falls back to RxNav API for unknown drugs; caches result as a new Concept row.
    Returns None if neither source can resolve the name. Never raises.
    """
    if not drug_source_value or not drug_source_value.strip():
        return None

    search_name = drug_source_value.strip().lower()

    # Check local vocab by name
    existing = Concept.objects.filter(
        concept_name__iexact=search_name,
        vocabulary_id__in=['RxNorm', 'RxNorm Extension'],
    ).first()
    if existing:
        return existing

    # Try RxNav
    try:
        rxcui, canonical_name = _rxnav_lookup(drug_source_value.strip())
        if not rxcui:
            return None

        # Re-check by concept_code (RXCUI) in case it was loaded from Athena
        existing = Concept.objects.filter(
            concept_code=str(rxcui),
            vocabulary_id='RxNorm',
        ).first()
        if existing:
            return existing

        # Cache the resolved concept
        return _create_rxnorm_concept(rxcui, canonical_name)

    except Exception as exc:
        logger.error('{"event": "rxnav_lookup_error", "drug": "%s", "error": "%s"}',
                     drug_source_value, exc)
        return None


def _rxnav_lookup(name: str):
    """Return (rxcui_str, canonical_name) for the active ingredient, or (None, None)."""
    url = f'{RXNAV_BASE}/drugs.json?name={urllib.parse.quote(name)}'
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read())

    for group in data.get('drugGroup', {}).get('conceptGroup', []):
        if group.get('tty') == 'IN':
            props = group.get('conceptProperties', [])
            if props:
                return props[0]['rxcui'], props[0]['name']
    return None, None


def _create_rxnorm_concept(rxcui: str, canonical_name: str):
    """Create and return a minimal Concept row for an RxNav-resolved drug."""
    vocab, _ = Vocabulary.objects.get_or_create(
        vocabulary_id='RxNorm',
        defaults={'vocabulary_name': 'RxNorm', 'vocabulary_concept_id': 0},
    )
    domain, _ = Domain.objects.get_or_create(
        domain_id='Drug',
        defaults={'domain_name': 'Drug', 'domain_concept_id': 13},
    )
    cc, _ = ConceptClass.objects.get_or_create(
        concept_class_id='Ingredient',
        defaults={'concept_class_name': 'Ingredient', 'concept_class_concept_id': 0},
    )
    max_id = Concept.objects.order_by('-concept_id').values_list('concept_id', flat=True).first() or 2_000_000_000
    concept, _ = Concept.objects.get_or_create(
        concept_code=str(rxcui)[:50],
        vocabulary=vocab,
        defaults={
            'concept_id': max_id + 1,
            'concept_name': canonical_name[:255],
            'domain': domain,
            'concept_class': cc,
            'standard_concept': 'S',
            'valid_start_date': date(1970, 1, 1),
            'valid_end_date': date(2099, 12, 31),
        },
    )
    return concept
