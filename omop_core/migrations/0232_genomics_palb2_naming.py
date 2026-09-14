"""Reviewed PALB2 identity; original OMOP facts and frozen v1 inputs survive.

Decision: cancerbot-org/cancerbot#4812, SamarElkassas, 2026-09-11.
This migrates projection/curation identities, never clinical source rows. A
rollback cannot safely relabel new PALB2 findings as the old misspelling.
"""
from django.db import migrations


def _correct_rows(rows):
    if not isinstance(rows, list):
        return rows
    result = []
    for row in rows:
        if isinstance(row, dict):
            row = dict(row)
            gene = row.get('gene')
            if isinstance(gene, str) and gene.strip().upper() == 'PALB1':
                row['source_gene'] = gene
                row['gene'] = 'PALB2'
            if row.get('marker_key') == 'palb1':
                row['marker_key'] = 'palb2'
        result.append(row)
    return result


def rename_projection(apps, schema_editor):
    using = schema_editor.connection.alias
    # Keep row IDs, reviewer/status, source keys and recipe details. Colliding
    # target identities abort the atomic migration rather than erase a review.
    for name in ('FieldConceptMapping', 'FieldSynonym', 'FieldChoice', 'FieldFormula'):
        apps.get_model('omop_core', name).objects.using(using).filter(
            field_name='genomics_palb1').update(field_name='genomics_palb2')
    Record = apps.get_model('omop_core', 'PatientRecord')
    fields = ('genomics_palb2', 'genetic_mutations', 'user_edited_fields')
    for record in Record.objects.using(using).only('pk', *fields).iterator(chunk_size=500):
        updates = {}
        for field in fields[:2]:
            before = getattr(record, field)
            after = _correct_rows(before)
            if after != before:
                updates[field] = after
        edited = record.user_edited_fields
        if isinstance(edited, list) and 'genomics_palb1' in edited:
            updates['user_edited_fields'] = list(dict.fromkeys(
                'genomics_palb2' if f == 'genomics_palb1' else f for f in edited))
        if updates:
            Record.objects.using(using).filter(pk=record.pk).update(**updates)


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0231_genomics_variant_name_recipe')]
    operations = [
        migrations.RenameField('patientrecord', 'genomics_palb1', 'genomics_palb2'),
        migrations.RunPython(rename_projection),
    ]
