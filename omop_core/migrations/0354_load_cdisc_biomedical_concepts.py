"""Load CDISC Biomedical Concepts and propose OMOP concept -> BC mappings.

Frozen data migration: reads the repository-contained package
``data/cdisc_bc_20261102`` (CDISC Library, 2026-11-02, 1,588 BCs), checks its
digest, and upserts every BC. Then it proposes a BC for each OMOP concept the
Wearables code mappings point at. Proposals are ``proposed`` only: a curator
approves them, and nothing here touches a source code mapping or a clinical row.

A wearable code reaches its BC through the OMOP concept it already maps to,
so the crosswalk is curated once per concept. Concepts are named by
(vocabulary, code) so the rows mean the same on an instance that loads the
vocabulary later. Re-running is safe: BCs are upserted, and a proposal that
exists (whatever a curator since made of it) is left alone. Reverse is a no-op.
"""
import gzip
import hashlib
import json
import logging
from pathlib import Path

from django.db import migrations

logger = logging.getLogger(__name__)

PAYLOAD = (Path(__file__).resolve().parent.parent / 'data' / 'cdisc_bc_20261102'
           / 'biomedical_concepts.jsonl.gz')
PAYLOAD_SHA256 = '1b2f0f4f52a2243522e9d1b3469786950aa1834d6a4b8447783878f9a3e613e8'
ORIGIN = 'cdisc-bc-seed v1'

# (vocabulary, concept code, BC id, note). Curated by hand against the BC
# definitions; a wearable metric with no BC (steps, sleep, exercise time, RMSSD)
# is left out rather than forced onto a near miss.
CROSSWALK = [
    ('LOINC', '8867-4', 'C49677', ''),
    ('LOINC', '40443-4', 'C49677', 'Resting state; the BC is state-agnostic.'),
    ('HK-Wearable', 'HK-WEAR-WALK-HR', 'C49677', 'Walking state; the BC is state-agnostic.'),
    ('LOINC', '59408-5', 'C60832', ''),
    ('LOINC', '9279-1', 'C49678', ''),
    ('LOINC', '29463-7', 'C81328', ''),
    ('LOINC', '8302-2', 'C164634', ''),
    ('LOINC', '39156-5', 'C16358', ''),
    ('LOINC', '8310-5', 'C174446', ''),
    ('LOINC', '39106-0', 'C174371', 'Skin temperature is a peripheral temperature.'),
    ('LOINC', '61008-9', 'C174371', 'Skin temperature is a peripheral temperature.'),
    ('LOINC', '8480-6', 'C25298', ''),
    ('LOINC', '8462-4', 'C25299', ''),
    ('LOINC', '8280-0', 'C100948', ''),
    ('LOINC', '73708-0', 'C122232', 'Mass form of body fat.'),
    ('LOINC', '41982-0', 'C122232', 'Percentage form of body fat.'),
    ('LOINC', '91557-9', 'C71258', ''),
    ('LOINC', '80404-7', 'C199682', 'SDNN: standard deviation of NN intervals.'),
    ('LOINC', '94122-9', 'C174363', 'A wearable VO2 max is an estimate.'),
    ('LOINC', '94122-9', 'C123570', 'Alternative if the value is a measured peak.'),
    ('LOINC', '93819-1', 'C147491', 'Active energy expenditure.'),
    ('HK-Wearable', 'HK-WEAR-BASAL-ENERGY', 'C126083', 'Basal energy expenditure.'),
    ('LOINC', '2339-0', 'C105585', ''),
    ('LOINC', '99504-3', 'C105585', 'Interstitial fluid glucose (CGM).'),
    ('LOINC', '20150-9', 'C38084', ''),
    ('LOINC', '19870-5', 'C111361', ''),
    ('LOINC', '33452-4', 'C41372', ''),
]


def _read_payload():
    digest = hashlib.sha256(PAYLOAD.read_bytes()).hexdigest()
    if digest != PAYLOAD_SHA256:
        raise ValueError('CDISC Biomedical Concept package checksum mismatch.')
    with gzip.open(PAYLOAD, 'rt', encoding='utf-8') as stream:
        return [json.loads(line) for line in stream]


def load_concepts_and_proposals(apps, schema_editor):
    alias = schema_editor.connection.alias
    BC = apps.get_model('omop_core', 'BiomedicalConcept')
    Mapping = apps.get_model('omop_core', 'ConceptBiomedicalConceptMapping')
    rows = _read_payload()
    known = {r['bc_id'] for r in rows}
    missing = sorted({bc for _, _, bc, _ in CROSSWALK} - known)
    if missing:
        raise ValueError(f'Crosswalk names Biomedical Concepts not in the package: {missing}')
    fields = [k for k in rows[0] if k != 'bc_id']
    BC.objects.using(alias).bulk_create(
        [BC(**r) for r in rows], batch_size=500,
        update_conflicts=True, unique_fields=['bc_id'], update_fields=fields,
    )
    created = 0
    for vocabulary_id, code, bc_id, note in CROSSWALK:
        _, was_created = Mapping.objects.using(alias).get_or_create(
            target_vocabulary_id=vocabulary_id, target_concept_code=code, bc_id=bc_id,
            defaults={'status': 'proposed', 'origin_system': ORIGIN, 'notes': note},
        )
        created += was_created
    logger.info('Loaded %d CDISC Biomedical Concepts; proposed %d new concept mappings.',
                len(rows), created)


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0353_biomedical_concepts'),
    ]

    operations = [
        migrations.RunPython(load_concepts_and_proposals, migrations.RunPython.noop),
    ]
