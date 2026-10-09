"""Rejoin the two 0184 leaves.

0184_person_language_skill_constraints (#810) and 0184_source_code_concept_mapping
landed within minutes of each other, both depending on 0183. Django refuses to
run a graph with two leaves at all, so dev could not migrate -- which reddens
every open PR, not only the two involved.

Neither migration is wrong and neither changes here. This one is empty; it only
tells the graph they both come before whatever is next.

It carries ``replaces`` because it was first applied under the generated name
0185_merge_20260829_0817, and staging had already run it before the rename.
Renaming the file alone raises InconsistentMigrationHistory on every database
that applied the old name -- the check runs before any migration executes, so
start.sh's migrate would fail the deploy outright rather than degrade. Declaring
the replacement lets Django treat this as applied wherever the old name is
recorded, while a fresh database applies it normally under the new name.
"""
from django.db import migrations


class Migration(migrations.Migration):

    replaces = [('omop_core', '0185_merge_20260829_0817')]

    dependencies = [
        ('omop_core', '0184_person_language_skill_constraints'),
        ('omop_core', '0184_source_code_concept_mapping'),
    ]

    operations = [
    ]
