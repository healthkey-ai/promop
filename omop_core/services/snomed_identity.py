"""Standard SNOMED identity takes precedence over an automatic drug crossmap."""
from datetime import date
from django.apps import apps
from django.db import connection

from omop_core.data_migrations.snomed_crossmap_v1 import (
    IDENTITY_ORIGIN, IMPORT_ORIGINS, identity_values, reconcile, standard_concepts,
)
from omop_core.models import Concept


def is_standard_snomed(concept):
    return bool(concept and concept.vocabulary_id == 'SNOMED' and concept.standard_concept == 'S'
                and not concept.invalid_reason
                and concept.valid_start_date <= date.today() <= concept.valid_end_date)


def snomed_identities(codes):
    return {c.concept_code: c for c in standard_concepts(Concept, 'default').filter(
        vocabulary_id='SNOMED', concept_code__in=codes,
    )}


def promote_crossmap_identity(mapping):
    if mapping.source_vocabulary_id != 'SNOMED' or mapping.origin_system not in IMPORT_ORIGINS:
        return None
    if reconcile(apps, connection, mapping_ids=[mapping.pk]):
        mapping.refresh_from_db()
        return mapping
    return None


def apply_import_identity(mapping, concept, imported_target):
    note = (f'Active standard SNOMED identity preferred to {mapping.origin_system} '
            f'crossmap destination {imported_target}.')
    mapping.notes = '\n'.join(filter(None, (mapping.notes, note)))
    for key, value in identity_values(concept).items():
        setattr(mapping, key, value)
    return mapping
