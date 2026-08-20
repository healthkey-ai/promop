from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0119_conceptsynonym_ix_concept_synonym_name_trgm"),
        ("omop_core", "0119_org_invitation_group_access_redirect_url"),
    ]

    operations = []
