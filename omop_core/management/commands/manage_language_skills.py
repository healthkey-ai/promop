"""Manage a person's language skills (#812).

Rewritten. What it replaced minted its own concepts and then looked them up by
name, which broke four rules at once (see #812):

  - concept_ids 40000001-40000010, inside the range OHDSI assigns, rather than
    the local block at >= 2_000_000_000
  - standard_concept='S' on locally-authored rows, which only OHDSI assigns
  - a fabricated 'LANGUAGE' vocabulary, when SNOMED already carries 878
    Language-domain concepts
  - resolution by concept_name, falling back across *every* loaded vocabulary,
    so "English" could match anything so named

None of it was needed. This version mints nothing and identifies a language the
only way the codebase permits: by (vocabulary_id, concept_code), constrained to
the Language domain.

Because a code is not memorable, --find-language searches the loaded
vocabulary and prints codes to use. Searching by name is safe; *writing* by
name is not, and the two are kept apart deliberately.
"""
from django.core.management.base import BaseCommand

from omop_core.models import (
    Person, PersonLanguageSkill, Concept, SKILL_LEVEL_CHOICES,
)
from omop_core.services.patient_record_service import (
    LanguageSkillError, refresh_patient_record, set_language_skills_by_code,
)

_CAPABILITIES = [value for value, _label in SKILL_LEVEL_CHOICES]


class Command(BaseCommand):
    help = "Manage a person's language skills, addressing languages by SNOMED code"

    def add_arguments(self, parser):
        parser.add_argument(
            '--person-id', type=int,
            help='Person ID to manage languages for',
        )
        parser.add_argument(
            '--find-language', type=str, metavar='TEXT',
            help='Search loaded SNOMED Language concepts and print their codes',
        )
        parser.add_argument(
            '--set-language', type=str, metavar='CODE:CAPS',
            help=('Replace the capabilities for one language, e.g. '
                  '"297487008:speak,read". Capabilities: '
                  + ', '.join(_CAPABILITIES) + '. An empty list clears it.'),
        )
        parser.add_argument(
            '--set-primary', type=str, metavar='CODE',
            help='Mark the given language as the primary one, by SNOMED code',
        )
        parser.add_argument(
            '--list-languages', action='store_true',
            help='List the languages recorded for the person',
        )

    def handle(self, *args, **options):
        if options.get('find_language'):
            self.find_language(options['find_language'])
            return

        person_id = options.get('person_id')
        if not person_id:
            self.stdout.write(self.style.ERROR('Please provide --person-id'))
            return

        try:
            person = Person.objects.get(person_id=person_id)
        except Person.DoesNotExist:
            self.stdout.write(self.style.ERROR(f'Person {person_id} not found'))
            return

        if options['list_languages']:
            self.list_languages(person)
        elif options['set_language']:
            self.set_language(person, options['set_language'])
        elif options['set_primary']:
            self.set_primary(person, options['set_primary'])
        else:
            self.stdout.write(self.style.ERROR(
                'Nothing to do. Use --list-languages, --set-language, '
                '--set-primary or --find-language.'))

    # -- lookup ------------------------------------------------------------

    def find_language(self, text):
        """Print candidate codes. Read-only, so matching on name is safe here.

        The rule the old command broke is about resolving a *write* target by
        name. Showing an operator what is available so they can choose a code
        is the opposite: the ambiguity is on screen instead of silently
        resolved.
        """
        matches = (
            Concept.objects
            .filter(vocabulary_id='SNOMED', domain_id='Language',
                    concept_name__icontains=text)
            .order_by('concept_name')[:25]
        )
        if not matches:
            self.stdout.write(self.style.WARNING(
                f'No loaded SNOMED Language concept matches "{text}". '
                f'If the vocabulary has not been loaded, run '
                f'load_athena_vocabularies first.'))
            return
        for concept in matches:
            flag = '' if concept.standard_concept == 'S' else '  (non-standard)'
            self.stdout.write(
                f'  {concept.concept_code:<14} {concept.concept_name}{flag}')

    # -- read --------------------------------------------------------------

    def list_languages(self, person):
        skills = (
            PersonLanguageSkill.objects
            .filter(person=person)
            .select_related('language_concept')
            .order_by('language_concept__concept_name', 'skill_level')
        )
        if not skills:
            self.stdout.write('No languages recorded.')
            return

        by_language = {}
        primary_code = None
        for skill in skills:
            concept = skill.language_concept
            by_language.setdefault(
                (concept.concept_code, concept.concept_name), []
            ).append(skill.skill_level)
            if skill.is_primary:
                primary_code = concept.concept_code

        for (code, name), capabilities in by_language.items():
            marker = '  [primary]' if code == primary_code else ''
            self.stdout.write(f'  {code:<14} {name}: '
                              f'{", ".join(capabilities)}{marker}')

    # -- write -------------------------------------------------------------

    def set_language(self, person, spec):
        code, _, raw_capabilities = spec.partition(':')
        code = code.strip()
        if not code:
            self.stdout.write(self.style.ERROR(
                'Invalid format. Use "CODE:capability,capability" — for '
                'example "297487008:speak,read". Find a code with '
                '--find-language.'))
            return

        capabilities = [c.strip() for c in raw_capabilities.split(',') if c.strip()]
        try:
            created, removed = set_language_skills_by_code(
                person, {code: capabilities})
        except LanguageSkillError as exc:
            self.stdout.write(self.style.ERROR(str(exc)))
            return

        # The eight flattened PatientRecord columns and languages_skills are
        # derived, and nothing on PersonLanguageSkill triggers a refresh, so a
        # command that only wrote the row left the read model disagreeing with
        # the database until something else happened to re-derive it.
        refresh_patient_record(person)
        self.stdout.write(self.style.SUCCESS(
            f'{code}: {created} added, {removed} removed'))

    def set_primary(self, person, code):
        # A person holds one row per capability, so a language can have up to
        # four. The primary flag lives on a single representative row, so pick
        # the earliest deterministically rather than whichever the database
        # happens to return.
        language_skill = (
            PersonLanguageSkill.objects
            .filter(person=person,
                    language_concept__vocabulary_id='SNOMED',
                    language_concept__concept_code=code)
            .order_by('created_date', 'id')
            .first()
        )
        if language_skill is None:
            self.stdout.write(self.style.ERROR(
                f'Person has no skill recorded for {code}. Add it first with '
                f'--set-language.'))
            return

        # Clear first, then set: the partial unique index allows only one
        # primary row per person, so setting before clearing would collide.
        PersonLanguageSkill.objects.filter(person=person).update(is_primary=False)
        PersonLanguageSkill.objects.filter(pk=language_skill.pk).update(is_primary=True)

        refresh_patient_record(person)
        self.stdout.write(self.style.SUCCESS(
            f'Set {language_skill.language_concept.concept_name} as primary'))
