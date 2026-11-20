"""A confidence only the server produced (#2668).

Provisional data reads a proposed mapping's ``suggestion_confidence`` as evidence,
so a mapping writer must not be able to type one in. The score endpoint (the
dialog's Calc) and suggest-one sign the number they computed together with what
it was computed for, and a save stores a client-supplied confidence only when
that token verifies against the source, table, destination and description of
the row being saved.

The signature binds ``(source vocabulary, source code, table, destination,
description hash, confidence)``, expires after an hour, and uses its own salt, so
no other signed value in the application can stand in for one. The description
is what the rankers were shown as the source's meaning, so a score cannot be
carried to a row that says something else. The source *metadata* the rankers also
read is not bound: for an existing row it comes from the stored row, for a new
row the client supplies it.
"""
import hashlib

from django.core import signing

from omop_core.services.source_vocabularies import canonical_source_vocabulary

SALT = 'omop_core.mapping.confidence'
MAX_AGE_SECONDS = 3600


def description_hash(text):
    """A short hash of the description, ignoring case and spacing."""
    normalized = ' '.join(str(text or '').split()).casefold()
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]


def _subject(source_vocabulary_id, source_code, omop_table, destination_concept_id, source_description):
    return [
        canonical_source_vocabulary(str(source_vocabulary_id or '').strip()),
        str(source_code or '').strip(),
        str(omop_table or '').strip(),
        int(destination_concept_id),
        description_hash(source_description),
    ]


def issue(*, source_vocabulary_id, source_code, omop_table, destination_concept_id,
          source_description, confidence):
    """A token for *confidence* (0-1) computed for this source, description and destination."""
    return signing.dumps(
        {'for': _subject(source_vocabulary_id, source_code, omop_table,
                         destination_concept_id, source_description),
         'confidence': float(confidence)},
        salt=SALT, compress=True,
    )


def verify(token, *, source_vocabulary_id, source_code, omop_table, destination_concept_id,
           source_description):
    """The confidence a token vouches for, or ``ValueError`` saying why it does not.

    Fails for a forged, tampered or expired token and for one computed for a
    different source, table, destination or description than the one being saved.
    """
    try:
        payload = signing.loads(token, salt=SALT, max_age=MAX_AGE_SECONDS)
    except signing.SignatureExpired:
        raise ValueError('The confidence has expired; calculate it again.') from None
    except (signing.BadSignature, TypeError):
        raise ValueError('The confidence was not computed by the server.') from None
    try:
        subject = _subject(source_vocabulary_id, source_code, omop_table,
                           destination_concept_id, source_description)
    except (TypeError, ValueError):
        raise ValueError('The confidence applies to a destination; choose one.') from None
    if not isinstance(payload, dict) or payload.get('for') != subject:
        raise ValueError('The confidence was computed for a different source, description or destination; '
                         'calculate it again.')
    confidence = payload.get('confidence')
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
        raise ValueError('The confidence was not computed by the server.')
    return float(confidence)
