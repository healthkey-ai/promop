"""Give reviewed catalog genes their OMOP Genomic concept ids (cancerbot #5673).

Before this migration every priority genomics field stored its parent variant
under LOINC 81252-9, whose own domain is Observation, so the writer fell back
to concept 0. A gene had no identity beyond its display symbol.

The reviewed catalog now names, for each gene, the standard OMOP Genomic
Measurement concept whose ``concept_code`` is the HGNC id (BRCA1 = 35950928 /
1100, and so on). The migration updates only mappings that still carry an exact
seeded legacy fingerprint -- no curator attribution, no edited note, the
original recipe and one of the two known seeded concepts -- in one conditional
UPDATE so a concurrent curator save cannot be overwritten.

Reversible: the forward writes a note that records the exact prior concept,
provenance and note, and the reverse restores those values only for rows that
still carry this migration's note and otherwise unchanged recipe.
"""
import re

from django.db import migrations
from django.db.models import Q
from django.utils import timezone

# field_name -> OMOP Genomic concept id (concept_code is the HGNC id).
GENE_CONCEPTS = {
    'genomics_brca1': 35950928,
    'genomics_brca2': 35950178,
    'genomics_pik3ca': 35953202,
    'genomics_tp53': 35948926,
    'genomics_esr1': 35948983,
    'genomics_palb2': 35949597,
    'genomics_kras': 35948791,
    'genomics_nras': 35950119,
    'genomics_braf': 35948173,
    'genomics_fam46c': 35952051,
    'genomics_dis3': 35948265,
    'genomics_xbp1': 35961405,
    'genomics_bcl2': 35958594,
    'genomics_ezh2': 35964476,
    'genomics_kmt2d': 35953669,
    'genomics_crebbp': 35949526,
    'genomics_notch1': 35947617,
    'genomics_notch2': 35960756,
    'genomics_sf3b1': 35948718,
    'genomics_atm': 35960740,
    'genomics_nsd2': 35950774,
    'genomics_cdkn2a': 35946721,
    'genomics_smarca4': 35949914,
    'genomics_ccnd1': 35963336,
}

LEGACY_CODE = '81252-9'
LEGACY_PROVENANCE = ('', 'system_generated', 'genomics-catalog v1', 'genomics-catalog v2')
PROVENANCE = 'genomics-catalog v3'
MIGRATION_NOTE = (
    'Genomics catalog v3 migration 0325: gene identity is the OMOP Genomic '
    'concept whose concept_code is the HGNC id (cancerbot #5673).'
)
LEGACY_NOTE = (
    'Genomics catalog v1: approved storage mapping per implementation request. '
    'Source: CancerBot a840f8d9af2c35477e2b6b76f981b29f02c21eec. '
    'Unmapped source text uses concept 0; clinical nomenclature requires expert review.'
)
PALB2_NOTE = (
    'Genomics catalog v2: reviewed PALB2 naming; '
    'https://github.com/cancerbot-org/cancerbot/issues/4812#issuecomment-5633297037. '
    'Storage mapping only; example annotations remain unvalidated.'
)
NOTE_VERSION = {'': 'empty', LEGACY_NOTE: 'v1', PALB2_NOTE: 'v2'}
NOTE_TEXT = {code: note for note, code in NOTE_VERSION.items()}
_PRIOR = re.compile(
    r' Replaces concept (none|\d+) \(LOINC 81252-9\), '
    r'provenance "([^"]*)", note (empty|v1|v2)\.$',
)


def _usable(concept):
    """Mirror the writer gate: a standard Measurement in the genomic vocabulary."""
    return bool(concept is not None
                and concept.vocabulary_id == 'OMOP Genomic'
                and concept.domain_id.lower() == 'measurement'
                and concept.standard_concept == 'S'
                and concept.invalid_reason is None)


def _source_values(field_name):
    """The seeded source keys for the field, including the retired PALB1 key."""
    source = 'genomics:' + field_name.removeprefix('genomics_')
    if field_name == 'genomics_palb2':
        # 0233 renamed the field but kept the original source key.
        return [source, 'genomics:palb1']
    return [source]


def _prior_note(mapping):
    """The forward note, carrying everything the reverse needs to restore."""
    return (
        MIGRATION_NOTE
        + f' Replaces concept {"none" if mapping.concept_id is None else mapping.concept_id}'
        + f' (LOINC {LEGACY_CODE}), provenance "{mapping.provenance}",'
        + f' note {NOTE_VERSION.get(mapping.notes or "", "empty")}.'
    )


def apply_gene_concepts(apps, schema_editor):
    using = schema_editor.connection.alias
    Concept = apps.get_model('omop_core', 'Concept')
    Mapping = apps.get_model('omop_core', 'FieldConceptMapping')
    legacy = Concept.objects.using(using).filter(
        vocabulary_id='LOINC', concept_code=LEGACY_CODE,
    ).first()
    legacy_ids = [0] + ([legacy.pk] if legacy else [])
    for field_name, concept_id in GENE_CONCEPTS.items():
        concept = Concept.objects.using(using).filter(pk=concept_id).first()
        if not _usable(concept):
            # The vocabulary release on this instance does not carry the gene
            # concept. Leave the mapping unmapped rather than invent an id; the
            # read-only audit reports it.
            continue
        mapping = Mapping.objects.using(using).filter(field_name=field_name).first()
        if mapping is None:
            continue
        # One conditional UPDATE over the exact seeded fingerprint: a curator
        # save between this read and the write changes a filtered value and the
        # row is left alone.
        Mapping.objects.using(using).filter(
            pk=mapping.pk,
            status='approved',
            reviewer_id__isnull=True,
            vocabulary_id='LOINC',
            concept_code=LEGACY_CODE,
        ).filter(
            Q(concept_id__in=legacy_ids) | Q(concept_id__isnull=True),
        ).filter(
            provenance__in=LEGACY_PROVENANCE,
            notes__in=list(NOTE_VERSION),
            source_value__in=_source_values(field_name),
            omop_table='measurement',
            value_kind='json',
            multiple=True,
            type_concept_id=32817,
            unit='',
            value_vocabulary='',
        ).update(
            concept=concept,
            vocabulary_id='OMOP Genomic',
            concept_code=str(concept.concept_code),
            provenance=PROVENANCE,
            notes=_prior_note(mapping),
            updated_at=timezone.now(),
        )


def restore_legacy_recipe(apps, schema_editor):
    using = schema_editor.connection.alias
    Concept = apps.get_model('omop_core', 'Concept')
    Mapping = apps.get_model('omop_core', 'FieldConceptMapping')
    for field_name, concept_id in GENE_CONCEPTS.items():
        concept = Concept.objects.using(using).filter(pk=concept_id).first()
        if concept is None:
            continue
        mapping = Mapping.objects.using(using).filter(
            field_name=field_name,
            concept_id=concept.pk,
            vocabulary_id='OMOP Genomic',
            concept_code=str(concept.concept_code),
            provenance=PROVENANCE,
            notes__startswith=MIGRATION_NOTE,
            source_value__in=_source_values(field_name),
            omop_table='measurement',
            value_kind='json',
            multiple=True,
            status='approved',
            reviewer_id__isnull=True,
            type_concept_id=32817,
            unit='',
            value_vocabulary='',
        ).first()
        if mapping is None:
            continue
        match = _PRIOR.search(mapping.notes or '')
        if match is None:
            continue
        prior_concept = None if match.group(1) == 'none' else int(match.group(1))
        mapping.concept_id = prior_concept
        mapping.vocabulary_id = 'LOINC'
        mapping.concept_code = LEGACY_CODE
        mapping.provenance = match.group(2)
        mapping.notes = NOTE_TEXT[match.group(3)]
        mapping.save(update_fields=[
            'concept', 'vocabulary_id', 'concept_code', 'provenance', 'notes', 'updated_at',
        ])


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0324_patientrecord_meets_lugano'),
    ]

    operations = [
        migrations.RunPython(apply_gene_concepts, restore_legacy_recipe),
    ]
