"""Transition ConceptEmbedding to managed=True with VectorField.

The table already exists (created by 0204 via raw SQL on systems with pgvector).
This migration updates Django's model state:

1. AlterModelOptions removes managed=False so Django owns the schema going
   forward (pgvector is now available on all target databases).
2. AlterField changes the placeholder BinaryField to pgvector's VectorField
   so Django tooling knows the real column type.

Neither operation emits DDL because the table and column already exist.
"""
import pgvector.django.vector
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0205_add_suggest_strategy_to_sccm"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="conceptembedding",
            options={},
        ),
        migrations.AlterField(
            model_name="conceptembedding",
            name="embedding",
            field=pgvector.django.vector.VectorField(dimensions=384),
        ),
    ]
