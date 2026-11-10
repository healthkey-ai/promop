"""Record which mapping notes a curator wrote (#2423).

``SourceCodeConceptMapping.notes`` is one column that Suggest, importers,
reconciliations and curators all write. ``curator_note_at`` says a curator
changed the text; the list's "with notes" filter and sticky-note marker read
it. From here the mapping dialog's save stamps it.

Existing rows have no such record, so they are backfilled by rule: the row
was saved by a curator (``updated_by`` is set, which only the curator edit
path does) and its note does not begin the way any machine-written note
does. The prefixes are frozen here. A curator who added a line *under* a
machine note is missed and marks the row by saving the note again; a wrong
mark would show a machine's text as a person's, which is worse.

Reverse drops the column, and with it which notes were curators'.
"""
from django.db import migrations, models

MACHINE_NOTE = (
    r'^\s*('
    r'(high|medium|low|unknown) confidence\M'
    r'|Jev wins \('
    r'|Both rankers declined'
    r'|No candidate concept'
    r'|No suitable concept'
    r'|Best-match fallback'
    r'|Ranking model unavailable'
    r'|Ranking failed'
    r'|Lexical match only'
    r'|Search expanded with'
    r'|All [0-9]+ candidates conflicted'
    r'|UMLS CUI bridge'
    r'|Unique exact standard concept'
    r'|Unambiguous extractor alias'
    r'|Source vocabulary label:'
    r'|Auto-proposed from'
    r'|Athena (recovery|export|destination recovery|SCCM reconciliation)'
    r'|This mapping is already supplied by Athena'
    r'|STCM reconciliation'
    r'|Narrative/result metadata label'
    r'|Curator priority [0-9]+\.'
    r'|Local SNOMED Maps to'
    r'|Standard SNOMED identi'
    r'|SNOMED (source identifi|OID duplicate)'
    r'|Active standard SNOMED identity'
    r')'
)


def mark_curator_notes(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE source_code_concept_mapping
               SET curator_note_at = updated_at
             WHERE updated_by_id IS NOT NULL
               AND notes ~ '\\S'
               AND notes !~* %s
            """,
            [MACHINE_NOTE],
        )


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0333_field_mapping_tab"),
    ]

    operations = [
        migrations.AddField(
            model_name="sourcecodeconceptmapping",
            name="curator_note_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(mark_curator_notes, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="sourcecodeconceptmapping",
            index=models.Index(
                condition=models.Q(("curator_note_at__isnull", False)),
                fields=["curator_note_at"],
                name="ix_sccm_curator_note",
            ),
        ),
    ]
