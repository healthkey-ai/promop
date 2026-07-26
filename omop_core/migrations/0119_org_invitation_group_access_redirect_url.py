from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0118_seed_hk_vocabularies_remap_fhir_concepts"),
    ]

    operations = [
        migrations.AddField(
            model_name="groupaccess",
            name="redirect_url",
            field=models.URLField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="orginvitation",
            name="redirect_url",
            field=models.URLField(blank=True, default="", max_length=500),
        ),
    ]
