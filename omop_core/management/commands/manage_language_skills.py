from django.core.management.base import BaseCommand
from django.db import transaction
from omop_core.models import (
    Person, PersonLanguageSkill, Concept, Vocabulary, Domain, ConceptClass,
    SKILL_LEVEL_CHOICES,
)
from omop_core.services.patient_record_service import refresh_patient_record


class Command(BaseCommand):
    help = 'Manage language skills for persons'

    def add_arguments(self, parser):
        parser.add_argument(
            '--person-id',
            type=int,
            help='Person ID to manage languages for'
        )
        parser.add_argument(
            '--add-language',
            type=str,
            help='Add a language (format: "language_name:skill_level", e.g., "English:speak")'
        )
        parser.add_argument(
            '--set-primary',
            type=str,
            help='Set primary language by name'
        )
        parser.add_argument(
            '--list-languages',
            action='store_true',
            help='List all languages for the person'
        )
        parser.add_argument(
            '--create-sample-concepts',
            action='store_true',
            help='Create sample language concepts for testing'
        )

    def handle(self, *args, **options):
        if options['create_sample_concepts']:
            self.create_sample_language_concepts()
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
        elif options['add_language']:
            self.add_language(person, options['add_language'])
        elif options['set_primary']:
            self.set_primary_language(person, options['set_primary'])
        else:
            self.stdout.write(self.style.WARNING('Please specify an action'))

    def create_sample_language_concepts(self):
        """Create sample language concepts for testing"""
        with transaction.atomic():
            # Create or get vocabulary
            vocabulary, _ = Vocabulary.objects.get_or_create(
                vocabulary_id='LANGUAGE',
                defaults={
                    'vocabulary_name': 'Language Vocabulary',
                    'vocabulary_concept_id': 40000000
                }
            )

            # Create or get domain
            domain, _ = Domain.objects.get_or_create(
                domain_id='Meas Value',
                defaults={
                    'domain_name': 'Measurement Value',
                    'domain_concept_id': 21
                }
            )

            # Create or get concept class
            concept_class, _ = ConceptClass.objects.get_or_create(
                concept_class_id='Language',
                defaults={
                    'concept_class_name': 'Language',
                    'concept_class_concept_id': 40000001
                }
            )

            # Sample languages with their concept IDs
            languages = [
                (40000001, 'English'),
                (40000002, 'Spanish'),
                (40000003, 'French'),
                (40000004, 'German'),
                (40000005, 'Italian'),
                (40000006, 'Portuguese'),
                (40000007, 'Chinese'),
                (40000008, 'Japanese'),
                (40000009, 'Korean'),
                (40000010, 'Arabic'),
            ]

            for concept_id, language_name in languages:
                concept, created = Concept.objects.get_or_create(
                    concept_id=concept_id,
                    defaults={
                        'concept_name': language_name,
                        'domain': domain,
                        'vocabulary': vocabulary,
                        'concept_class': concept_class,
                        'standard_concept': 'S',
                        'concept_code': language_name.upper(),
                        'valid_start_date': '2024-01-01',
                        'valid_end_date': '2099-12-31',
                    }
                )
                if created:
                    self.stdout.write(f'Created language concept: {language_name}')

    def list_languages(self, person):
        """List all languages for a person"""
        language_skills = person.language_skills.all()
        if not language_skills:
            self.stdout.write('No languages recorded for this person')
            return

        self.stdout.write(f'Languages for Person {person.person_id}:')
        for skill in language_skills:
            primary_indicator = ' (PRIMARY)' if skill.is_primary else ''
            self.stdout.write(f'  {skill.language_concept.concept_name}: {skill.skill_level}{primary_indicator}')

        # Show summary
        summary = person.get_language_skills_summary()
        display = person.patient_record.get_languages_display() if hasattr(person, 'patient_record') else 'No PatientRecord'
        self.stdout.write(f'\nSummary: {display}')

    def add_language(self, person, language_spec):
        """Add a language skill"""
        try:
            language_name, skill_level = language_spec.split(':')
            language_name = language_name.strip()
            skill_level = skill_level.strip()

            valid_levels = [v for v, _label in SKILL_LEVEL_CHOICES]
            if skill_level not in valid_levels:
                self.stdout.write(self.style.ERROR(
                    f'Invalid skill level: {skill_level}. Use: {", ".join(valid_levels)}'))
                return

            # Find language concept (prefer LANGUAGE vocabulary)
            try:
                language_concept = Concept.objects.filter(
                    concept_name__iexact=language_name,
                    vocabulary__vocabulary_id='LANGUAGE'
                ).first()
                
                if not language_concept:
                    language_concept = Concept.objects.get(concept_name__iexact=language_name)
            except Concept.DoesNotExist:
                self.stdout.write(self.style.ERROR(f'Language concept "{language_name}" not found. Use --create-sample-concepts first.'))
                return

            # Create or update language skill
            # One row per capability: skill_level is part of the identity, so
            # adding "English:read" no longer overwrites "English:speak".
            language_skill, created = PersonLanguageSkill.objects.update_or_create(
                person=person,
                language_concept=language_concept,
                skill_level=skill_level,
                defaults={},
            )

            # The eight flattened PatientRecord columns and languages_skills are
            # derived, and nothing on PersonLanguageSkill triggers a refresh, so
            # a command that only wrote the row left the read model disagreeing
            # with the database until something else happened to re-derive it.
            refresh_patient_record(person)

            action = 'Created' if created else 'Updated'
            self.stdout.write(self.style.SUCCESS(f'{action} language skill: {language_name} - {skill_level}'))

        except ValueError:
            self.stdout.write(self.style.ERROR('Invalid format. Use: "language_name:skill_level"'))

    def set_primary_language(self, person, language_name):
        """Set a language as primary"""
        try:
            language_concept = Concept.objects.filter(
                concept_name__iexact=language_name,
                vocabulary__vocabulary_id='LANGUAGE'
            ).first()
            
            if not language_concept:
                language_concept = Concept.objects.filter(concept_name__iexact=language_name).first()
                
            if not language_concept:
                self.stdout.write(self.style.ERROR(f'Language concept "{language_name}" not found'))
                return
            
            # A person holds one row per capability, so a language can have up
            # to four. .get() here raised MultipleObjectsReturned as soon as
            # anyone recorded two capabilities in the same language. The primary
            # flag lives on a single representative row, so pick the earliest
            # deterministically rather than whichever the database returns.
            language_skill = (
                PersonLanguageSkill.objects
                .filter(person=person, language_concept=language_concept)
                .order_by('created_date', 'id')
                .first()
            )
            if language_skill is None:
                self.stdout.write(self.style.ERROR(f'Person does not have {language_name} skill. Add it first.'))
                return

            # Clear first, then set: the partial unique index allows only one
            # primary row per person, so setting before clearing would collide.
            PersonLanguageSkill.objects.filter(person=person).update(is_primary=False)
            PersonLanguageSkill.objects.filter(pk=language_skill.pk).update(is_primary=True)

            refresh_patient_record(person)
            self.stdout.write(self.style.SUCCESS(f'Set {language_name} as primary language'))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error: {str(e)}'))
