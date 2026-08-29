"""skill_concept and the flattened capability columns (#813).

Two halves of the same idea: make language capability something a query can act
on.

PersonLanguageSkill.skill_concept is the coded form of skill_level, mirroring
OMOP's value_as_concept / value_source_value pairing -- skill_level stays the
raw value the row was written with, skill_concept is what it resolves to. It is
nullable and resolved in save() rather than required, because a row can legally
be written before the HK-Language mint has landed, and refusing the write would
make the mint a hard dependency of storing a language at all.

The eight PatientRecord columns unroll the four capabilities over the two
languages that gate trials here. languages_skills is a display string; matching
"can read English" against it means parsing prose in a WHERE clause. They are
derived from PersonLanguageSkill and never written directly.

They are nullable on purpose, and the third value carries the weight: NULL means
nobody asked about that language, False means the person was asked and does not
have that capability. A trial needing English readers must exclude the second
and not the first -- with a two-valued column every patient never asked would
look like a patient who cannot read, and the filter would quietly drop them.

Backfill is a no-op: person_language_skill is empty on every database, so there
is nothing to derive from yet. The columns start NULL, which is the honest value
for a fact nobody has recorded.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0186_seed_language_capability_concepts'),
    ]

    operations = [
        migrations.AddField(
            model_name='patientrecord',
            name='english_read',
            field=models.BooleanField(blank=True, default=None, help_text='Derived: person reads English. NULL = not asked.', null=True),
        ),
        migrations.AddField(
            model_name='patientrecord',
            name='english_speak',
            field=models.BooleanField(blank=True, default=None, help_text='Derived: person speaks English. NULL = not asked.', null=True),
        ),
        migrations.AddField(
            model_name='patientrecord',
            name='english_understand',
            field=models.BooleanField(blank=True, default=None, help_text='Derived: person understands English. NULL = not asked.', null=True),
        ),
        migrations.AddField(
            model_name='patientrecord',
            name='english_write',
            field=models.BooleanField(blank=True, default=None, help_text='Derived: person writes English. NULL = not asked.', null=True),
        ),
        migrations.AddField(
            model_name='patientrecord',
            name='spanish_read',
            field=models.BooleanField(blank=True, default=None, help_text='Derived: person reads Spanish. NULL = not asked.', null=True),
        ),
        migrations.AddField(
            model_name='patientrecord',
            name='spanish_speak',
            field=models.BooleanField(blank=True, default=None, help_text='Derived: person speaks Spanish. NULL = not asked.', null=True),
        ),
        migrations.AddField(
            model_name='patientrecord',
            name='spanish_understand',
            field=models.BooleanField(blank=True, default=None, help_text='Derived: person understands Spanish. NULL = not asked.', null=True),
        ),
        migrations.AddField(
            model_name='patientrecord',
            name='spanish_write',
            field=models.BooleanField(blank=True, default=None, help_text='Derived: person writes Spanish. NULL = not asked.', null=True),
        ),
        migrations.AddField(
            model_name='personlanguageskill',
            name='skill_concept',
            field=models.ForeignKey(blank=True, db_column='skill_concept_id', help_text='HK-Language concept coding skill_level', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='person_language_skill_levels', to='omop_core.concept'),
        ),
    ]
