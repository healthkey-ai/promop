from django.core.management.base import BaseCommand
from django.db import transaction
from omop_core.models import Person, PersonLanguageSkill, Concept, Vocabulary, Domain, ConceptClass


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
            help='Add a language (format: "language_name:skill_level", e.g., "English:both")'
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

            if skill_level not in ['speak', 'write', 'both']:
                self.stdout.write(self.style.ERROR(f'Invalid skill level: {skill_level}. Use: speak, write, both'))
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
            language_skill, created = PersonLanguageSkill.objects.update_or_create(
                person=person,
                language_concept=language_concept,
                defaults={'skill_level': skill_level}
            )

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
            
            # Check if person has this language skill
            try:
                language_skill = PersonLanguageSkill.objects.get(
                    person=person,
                    language_concept=language_concept
                )
            except PersonLanguageSkill.DoesNotExist:
                self.stdout.write(self.style.ERROR(f'Person does not have {language_name} skill. Add it first.'))
                return

            # Remove primary from all other languages
            PersonLanguageSkill.objects.filter(person=person).update(is_primary=False)
            
            # Set this language as primary
            language_skill.is_primary = True
            language_skill.save()

            self.stdout.write(self.style.SUCCESS(f'Set {language_name} as primary language'))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error: {str(e)}'))
