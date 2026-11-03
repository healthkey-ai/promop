"""Give reviewed catalog genes their OMOP Genomic concept ids (cancerbot #5673).

Before this migration every priority genomics field stored its parent variant
under LOINC 81252-9, whose own domain is Observation, so the writer fell back
to concept 0. A gene had no identity beyond its display symbol.

The reviewed catalog now names, for each gene, the standard OMOP Genomic
Measurement concept whose ``concept_code`` is the HGNC id (BRCA1 = 35950928 /
1100, and so on). The migration updates only mappings that still carry the
untouched legacy recipe; a concept a curator already chose is left alone.

Reversible: the reverse restores the legacy recipe only where this migration's
provenance is still in force.
"""
from django.db import migrations

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
LEGACY_PROVENANCE = 'genomics-catalog v2'
PROVENANCE = 'genomics-catalog v3'
LEGACY_NOTE = (
    'Genomics catalog v1: approved storage mapping per implementation request. '
    'Source: CancerBot a840f8d9af2c35477e2b6b76f981b29f02c21eec. '
    'Unmapped source text uses concept 0; clinical nomenclature requires expert review.'
)
NOTE = (
    'Genomics catalog v3: gene identity is the OMOP Genomic concept whose '
    'concept_code is the HGNC id (cancerbot #5673).'
)


def _usable(concept):
    """Mirror the writer gate: a standard Measurement in the genomic vocabulary."""
    return bool(concept is not None
                and concept.vocabulary_id == 'OMOP Genomic'
                and concept.domain_id.lower() == 'measurement'
                and concept.standard_concept == 'S'
                and concept.invalid_reason is None)


def apply_gene_concepts(apps, schema_editor):
    using = schema_editor.connection.alias
    Concept = apps.get_model('omop_core', 'Concept')
    Mapping = apps.get_model('omop_core', 'FieldConceptMapping')
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
        if mapping.vocabulary_id != 'LOINC' or mapping.concept_code != LEGACY_CODE:
            # A curator already decided this field; never overwrite it.
            continue
        mapping.concept = concept
        mapping.vocabulary_id = 'OMOP Genomic'
        mapping.concept_code = str(concept.concept_code)
        mapping.provenance = PROVENANCE
        mapping.notes = NOTE
        mapping.save(update_fields=[
            'concept', 'vocabulary_id', 'concept_code', 'provenance', 'notes', 'updated_at',
        ])


def restore_legacy_recipe(apps, schema_editor):
    using = schema_editor.connection.alias
    Concept = apps.get_model('omop_core', 'Concept')
    Mapping = apps.get_model('omop_core', 'FieldConceptMapping')
    zero = Concept.objects.using(using).filter(pk=0).first()
    for field_name, concept_id in GENE_CONCEPTS.items():
        concept = Concept.objects.using(using).filter(pk=concept_id).first()
        if concept is None:
            continue
        mapping = Mapping.objects.using(using).filter(
            field_name=field_name,
            vocabulary_id='OMOP Genomic',
            concept_code=str(concept.concept_code),
            provenance=PROVENANCE,
        ).first()
        if mapping is None:
            continue
        mapping.concept = zero
        mapping.vocabulary_id = 'LOINC'
        mapping.concept_code = LEGACY_CODE
        mapping.provenance = LEGACY_PROVENANCE
        mapping.notes = LEGACY_NOTE
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
