"""Structural constraints for person_language_skill (#809).

The table enforced almost nothing: two rows could claim to be a person's primary
language, skill_level accepted any string up to ten characters, and any of the
2.4M concepts satisfied the language_concept FK. All three databases held zero
rows when this was written, so the constraints go on before there is anything to
repair -- and for the same reason the skill vocabulary can be replaced outright
rather than migrated.

skill_level becomes four independent capabilities: speak, read, write,
understand. They are not a scale. Understanding a language without reading it is
ordinary, and so is reading without speaking, so a person gets one row per
capability rather than one row carrying a combined level. That is why the unique
key gains skill_level: a combined level could say nothing about reading or
understanding, and forced two separate abilities to be asserted together.

The literals stay literals here. HK-Language concepts coding these four
capabilities are minted separately, alongside the skill_concept FK that resolves
to them -- this migration is the constraint layer they will hang off.

The language-domain rule is a trigger because a CHECK constraint cannot
subquery. The alternative -- a composite FK to concept(concept_id, domain_id) --
is fully declarative but couples this table to concept.domain_id, so a
vocabulary reload that moved a concept between domains would be blocked by the
FK. Given the --replace reload path (#680), that trade is not worth making for a
table with no rows. The trigger's known limit is the mirror image: it fires on
writes here, so it does not re-check a row whose concept later changes domain.
on_delete=PROTECT already stops the concept being deleted out from under a row.

Domain 'Language' is the discriminator: 878 concepts, all SNOMED, and what both
concepts named in #774 carry (4180186 "English language", 4182511 "Spanish
language" are Language/Qualifier Value).
"""

from django.db import migrations, models

_TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION person_language_skill_check_language_domain()
RETURNS TRIGGER AS $$
DECLARE
    concept_domain TEXT;
BEGIN
    SELECT domain_id INTO concept_domain
      FROM concept WHERE concept_id = NEW.language_concept_id;

    IF concept_domain IS DISTINCT FROM 'Language' THEN
        RAISE EXCEPTION
            'language_concept_id % is domain %, not Language',
            NEW.language_concept_id, COALESCE(concept_domain, '<absent>')
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER person_language_skill_language_domain_trigger
    BEFORE INSERT OR UPDATE OF language_concept_id ON person_language_skill
    FOR EACH ROW EXECUTE FUNCTION person_language_skill_check_language_domain();
"""

_TRIGGER_REVERSE_SQL = """
DROP TRIGGER IF EXISTS person_language_skill_language_domain_trigger
    ON person_language_skill;
DROP FUNCTION IF EXISTS person_language_skill_check_language_domain();
"""


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0183_update_m_protein_type_values'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='personlanguageskill',
            unique_together=set(),
        ),
        migrations.AlterField(
            model_name='personlanguageskill',
            name='is_primary',
            field=models.BooleanField(db_default=False, default=False, help_text="Is this the person's primary language?"),
        ),
        migrations.AlterField(
            model_name='personlanguageskill',
            name='skill_level',
            field=models.CharField(choices=[('speak', 'Speak'), ('read', 'Read'), ('write', 'Write'), ('understand', 'Understand')], help_text='One capability the person has in this language: speak, read, write or understand', max_length=10),
        ),
        migrations.AlterUniqueTogether(
            name='personlanguageskill',
            unique_together={('person', 'language_concept', 'skill_level')},
        ),
        migrations.AddConstraint(
            model_name='personlanguageskill',
            constraint=models.UniqueConstraint(condition=models.Q(('is_primary', True)), fields=('person',), name='person_language_skill_one_primary_per_person'),
        ),
        migrations.AddConstraint(
            model_name='personlanguageskill',
            constraint=models.CheckConstraint(condition=models.Q(('skill_level__in', ['speak', 'read', 'write', 'understand'])), name='person_language_skill_skill_level_valid'),
        ),
        migrations.RunSQL(sql=_TRIGGER_SQL, reverse_sql=_TRIGGER_REVERSE_SQL),
    ]
