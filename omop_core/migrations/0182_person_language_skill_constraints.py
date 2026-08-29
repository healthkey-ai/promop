"""Structural constraints for person_language_skill (#809).

The table enforced almost nothing: two rows could claim to be a person's primary
language, skill_level accepted any string up to ten characters, and any of the
2.4M concepts satisfied the language_concept FK. All three databases held zero
rows when this was written, so the constraints go on before there is anything to
repair.

skill_level stays as literals rather than becoming a concept FK. There is no
usable coded value set for the speak/write/both trichotomy: the nearest SNOMED
concepts are speech-pathology findings -- 4118707 "Able to speak", 4115799
"Unable to speak" -- which assert something clinically different, since a
patient who speaks a language but does not write it is not "unable to write".
37162770 "Language written" is an Observable Entity, a question rather than an
answer, and nothing codes "both". Coding these would repeat the mistake #807
avoided for "Performance Status" and "Prior Therapies".

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
        ('omop_core', '0181_seed_refractory_field_mappings'),
    ]

    operations = [
        migrations.AlterField(
            model_name='personlanguageskill',
            name='is_primary',
            field=models.BooleanField(db_default=False, default=False, help_text="Is this the person's primary language?"),
        ),
        migrations.AddConstraint(
            model_name='personlanguageskill',
            constraint=models.UniqueConstraint(condition=models.Q(('is_primary', True)), fields=('person',), name='person_language_skill_one_primary_per_person'),
        ),
        migrations.AddConstraint(
            model_name='personlanguageskill',
            constraint=models.CheckConstraint(condition=models.Q(('skill_level__in', ['speak', 'write', 'both'])), name='person_language_skill_skill_level_valid'),
        ),
        migrations.RunSQL(sql=_TRIGGER_SQL, reverse_sql=_TRIGGER_REVERSE_SQL),
    ]
