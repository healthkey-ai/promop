"""Carry the extractor's lab table in the curation tables, approved.

``_LOINC_LAB_FIELDS`` in ``patient_record_service`` is the checked-in inventory
of the lab fields derived from OMOP Measurement rows and their LOINC codes.
Lab derivation routes through the curation tables
(``resolve_lab_code_fields``): an approved, concept-resolved
FieldConceptMapping decides each field's canonical code and approved
SourceCodeConceptMapping LOINC aliases add the rest. Migration 0240 seeds the
mapping rows from ``field_curation_v1.json`` and 0285 approves the alias codes;
this migration closes the gaps an existing database may still carry, so every
field in the table is routed by the tables rather than by the table itself:

- a lab field with no mapping row at all gets one, approved, on the table's
  canonical code;
- a row still ``proposed`` exactly as 0240 seeded it (the same LOINC code,
  never reviewed) is approved. On the fixture that is
  ``creatinine_clearance_ml_min``, LOINC 2164-2, which the extractor has
  always derived from that code;
- a row whose concept is unresolved (seeded before Athena loaded) is pointed
  at the Concept when it is loaded now, so the resolver reads it.

A reviewed row, a rejected row, and a row a curator pointed at another code
are left alone: the tables are the authority, and this only records what the
extractor already did. Test databases are skipped, as in 0240.

Reverse is a no-op: the rows this migration approved or created stay in
place (status, provenance and notes are not restored, nor is a resolved
concept cleared). ``provenance='lab-extractor-table-v1'`` identifies them
for a manual rollback.
"""
from django.db import migrations


# field → (canonical LOINC code, Athena concept id), from field_curation_v1.json.
# Frozen here so the migration does not change when the service table does.
LAB_FIELD_CONCEPTS = {
    'albumin_g_dl': ('1751-7', 3024561),
    'alc_thousand_per_ul': ('731-0', 3004327),
    'alkaline_phosphatase_u_l': ('6768-6', 3035995),
    'alt_u_l': ('1742-6', 3006923),
    'amc_thousand_per_ul': ('742-7', 3033575),
    'anc_thousand_per_ul': ('751-8', 3013650),
    'ast_u_l': ('1920-8', 3013721),
    'beta2_microglobulin': ('1952-1', 3013201),
    'bilirubin_total_mg_dl': ('1975-2', 3024128),
    'bnp_pg_ml': ('42637-9', 3031569),
    'bun_mg_dl': ('3094-0', 3013682),
    'c_reactive_protein': ('1988-5', 3020460),
    'ca19_9_u_ml': ('25390-6', 3000659),
    'cea_ng_ml': ('2039-6', 3003785),
    'clonal_plasma_cells': ('93021-4', 37019751),
    'creatinine_clearance_ml_min': ('2164-2', 3005770),
    'egfr_ml_min_173m2': ('62238-1', 40764999),
    'ejection_fraction': ('8806-2', 3019817),
    'esr': ('30341-2', 3015183),
    'glucose_mg_dl': ('2345-7', 3004501),
    'hba1c_percent': ('4548-4', 3004410),
    'heartrate_variability': ('80404-7', 21491502),
    'hematocrit_percent': ('20570-8', 3009542),
    'hemoglobin_g_dl': ('718-7', 3000963),
    'inr': ('6301-6', 3022217),
    'kappa_flc': ('104544-2', 1091621),
    'kappa_lambda_ratio': ('104546-7', 1091281),
    'lambda_flc': ('33944-0', 3047169),
    'ldh_u_l': ('2532-0', 3016436),
    'magnesium_mg_dl': ('2601-3', 3012095),
    'monoclonal_protein_serum': ('33358-3', 3046299),
    'monoclonal_protein_urine': ('42482-0', 3034655),
    'phosphorus': ('2777-1', 3011904),
    'platelet_count_thousand_per_ul': ('777-3', 3024929),
    'potassium_meq_l': ('2823-3', 3023103),
    'psa_ng_ml': ('2857-1', 3013603),
    'pt_seconds': ('5902-2', 3034426),
    'ptt_seconds': ('3173-2', 3013466),
    'qtcf_value': ('8632-1', 2100000015),
    'rbc_million_per_ul': ('789-8', 3020416),
    'serum_bilirubin_level_direct': ('1968-7', 3027597),
    'serum_calcium_mg_dl': ('17861-6', 3006906),
    'serum_creatinine_mg_dl': ('2160-0', 3016723),
    'sodium_meq_l': ('2951-2', 3019550),
    'total_protein': ('2885-2', 3020630),
    'troponin_ng_ml': ('10839-9', 3021337),
    'wbc_count_thousand_per_ul': ('6690-2', 3000905),
}

PROVENANCE = 'lab-extractor-table-v1'
NOTE = ('Approved from the extractor table (_LOINC_LAB_FIELDS), which has always '
        'derived this field from this LOINC code; lab derivation routes through '
        'FieldConceptMapping (#2286).')


def approve_lab_field_mappings(apps, schema_editor):
    Mapping = apps.get_model('omop_core', 'FieldConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')
    db = schema_editor.connection.alias
    existing = {m.field_name: m for m in Mapping.objects.using(db).filter(
        field_name__in=LAB_FIELD_CONCEPTS,
    )}
    # An approved row's (omop_table, source_value) is unique; a new row keys
    # itself on its code only when nothing approved already does.
    taken_sources = set(Mapping.objects.using(db).filter(
        status='approved', omop_table__iexact='measurement',
    ).exclude(source_value='').values_list('source_value', flat=True))
    concepts = {
        concept.concept_code: concept
        for concept in Concept.objects.using(db).filter(
            vocabulary_id='LOINC',
            concept_code__in=[code for code, _ in LAB_FIELD_CONCEPTS.values()],
        )
    }

    for field, (code, concept_id) in LAB_FIELD_CONCEPTS.items():
        concept = concepts.get(code)
        mapping = existing.get(field)
        if mapping is None:
            source_value = code if code not in taken_sources else ''
            Mapping.objects.using(db).create(
                field_name=field, concept=concept, vocabulary_id='LOINC',
                concept_code=code, omop_table='measurement',
                source_value=source_value, value_kind='number',
                status='approved', provenance=PROVENANCE, notes=NOTE,
            )
            taken_sources.add(source_value)
            continue
        same_code = (
            mapping.vocabulary_id == 'LOINC' and mapping.concept_code == code
            and mapping.omop_table.lower() == 'measurement'
        )
        if not same_code:
            # A curator pointed the field elsewhere, or 0240 never seeded it
            # this way; the tables decide.
            continue
        update = {}
        if mapping.status == 'proposed' and mapping.reviewer_id is None:
            update.update(status='approved', provenance=PROVENANCE,
                          notes=(mapping.notes + ' ' if mapping.notes else '') + NOTE)
            if mapping.source_value and mapping.source_value in taken_sources:
                # Approving it would collide with the row that already keys
                # this source value; the code still routes without one.
                update['source_value'] = ''
            elif mapping.source_value:
                taken_sources.add(mapping.source_value)
        if mapping.concept_id is None and concept is not None:
            update['concept'] = concept
        if update:
            for name, value in update.items():
                setattr(mapping, name, value)
            mapping.save(update_fields=[*update, 'updated_at'])


def forwards(apps, schema_editor):
    # Skip in test databases, as 0240 does: tests seed what they need, and
    # creating every lab mapping here would collide with the rows they create.
    name = str(schema_editor.connection.settings_dict.get('NAME', ''))
    if name.startswith('test_'):
        return
    approve_lab_field_mappings(apps, schema_editor)


def reverse(apps, schema_editor):
    # Approved and created rows are left as they are; see the module docstring.
    pass


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0325_add_dlbcl_disease_category')]
    operations = [migrations.RunPython(forwards, reverse)]
