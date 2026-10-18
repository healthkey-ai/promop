"""Bounded patient examples for the mapping queue, in one query per page."""
import json

from django.db import connection

from .source_fhir_context import based_on_labels

MAX_PATIENTS = 30
MAX_VALUES = 3


def attach_source_samples(mappings):
    mappings = {m.pk: m for m in mappings}
    if not mappings:
        return
    for mapping in mappings.values():
        mapping._source_samples = {'quantities': [], 'based_on': [], 'units': []}
    # Join the bounded mapping page in one pass. Match the exact source
    # identity, and never sample another organization's patient for an
    # organization-scoped mapping. One inventory row per patient/source.
    #
    # The window ORDER BY prefers rows that carry data: a row with a
    # quantity, unit, or non-empty metadata sorts before one without.
    # Without this, the first 30 patients by person_id may all have
    # empty fields, so quantities and basedOn would always be [].
    with connection.cursor() as cursor:
        cursor.execute('''
            SELECT mapping_id, quantity, unit, metadata FROM (
                SELECT m.id AS mapping_id,
                       COALESCE(sc.example_quantity_override, sc.example_quantity) AS quantity,
                       COALESCE(sc.source_unit_override, sc.source_unit) AS unit,
                       sc.source_metadata AS metadata,
                       row_number() OVER (
                           PARTITION BY m.id
                           ORDER BY
                               (CASE WHEN COALESCE(NULLIF(COALESCE(sc.example_quantity_override,
                                          sc.example_quantity), ''), NULLIF(COALESCE(
                                          sc.source_unit_override, sc.source_unit), ''))
                                     IS NOT NULL THEN 0 ELSE 1 END),
                               (CASE WHEN sc.source_metadata IS NOT NULL
                                      AND sc.source_metadata != '{}'::jsonb
                                     THEN 0 ELSE 1 END),
                               sc.person_id, sc.id
                       ) AS sample_rank
                FROM source_code_concept_mapping m
                JOIN patient_source_code sc ON sc.source_value = m.source_code
                    AND sc.source_vocabulary_id = m.source_vocabulary_id
                    AND sc.omop_table = m.omop_table
                WHERE m.id = ANY(%s)
                    AND (m.organization_id IS NULL OR EXISTS (
                        SELECT 1 FROM patient_record pr
                        WHERE pr.person_id = sc.person_id AND pr.organization_id = m.organization_id
                    ))
            ) samples WHERE sample_rank <= %s
            ORDER BY mapping_id, sample_rank
        ''', [list(mappings), MAX_PATIENTS])
        rows = cursor.fetchall()

    for mapping_id, quantity, unit, metadata in rows:
        # Raw cursors return JSON text on PostgreSQL; tolerate decoded values.
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        sample = mappings[mapping_id]._source_samples
        quantity = quantity.strip() if isinstance(quantity, str) else ''
        # An explicit correction to blank remains blank; do not resurrect the
        # original FHIR value after the curator cleared it.
        for key, values in (('quantities', [quantity]), ('units', [unit]), ('based_on', based_on_labels(metadata))):
            for value in values:
                if value and value not in sample[key] and len(sample[key]) < MAX_VALUES:
                    sample[key].append(value)
