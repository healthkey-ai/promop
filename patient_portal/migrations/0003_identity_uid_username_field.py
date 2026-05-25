"""Add uid field as USERNAME_FIELD, remove unique=True from sub.

Three steps:
1. Add uid as nullable (no unique yet), remove unique from sub
2. Backfill uid = issuer:sub for all existing rows
3. Make uid non-nullable + unique
"""
from django.db import migrations, models


def backfill_uid(apps, schema_editor):
    Identity = apps.get_model("patient_portal", "Identity")
    for identity in Identity.objects.all():
        identity.uid = f"{identity.issuer}:{identity.sub}"
        identity.save(update_fields=["uid"])


class Migration(migrations.Migration):

    dependencies = [
        ("patient_portal", "0002_patient_models"),
    ]

    operations = [
        # Step 1: add uid as nullable, drop unique from sub
        migrations.AddField(
            model_name="identity",
            name="uid",
            field=models.CharField(max_length=512, null=True, editable=False),
        ),
        migrations.AlterField(
            model_name="identity",
            name="sub",
            field=models.CharField(max_length=255),
        ),
        # Step 2: backfill
        migrations.RunPython(backfill_uid, migrations.RunPython.noop),
        # Step 3: make uid non-nullable + unique
        migrations.AlterField(
            model_name="identity",
            name="uid",
            field=models.CharField(max_length=512, unique=True, editable=False),
        ),
    ]
