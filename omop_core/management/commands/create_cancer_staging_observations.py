from django.core.management.base import BaseCommand
from django.db import transaction
from omop_core.models import (
    Person, ConditionOccurrence, Observation, Concept, 
    Vocabulary, Domain, ConceptClass
)


class Command(BaseCommand):
    help = 'Create sample cancer staging observations following OMOP CDM best practices'

    def add_arguments(self, parser):
        parser.add_argument(
            '--person-id',
            type=int,
            help='Person ID to create staging observations for'
        )
        parser.add_argument(
            '--condition-occurrence-id',
            type=int,
            help='ConditionOccurrence ID to link staging observations to'
        )
        parser.add_argument(
            '--create-concepts',
            action='store_true',
            help='Create sample staging concepts'
        )

    def handle(self, *args, **options):
        if options['create_concepts']:
            self.create_staging_concepts()
            return

        person_id = options.get('person_id')
        condition_occurrence_id = options.get('condition_occurrence_id')
        
        if not person_id or not condition_occurrence_id:
            self.stdout.write(self.style.ERROR('Please provide --person-id and --condition-occurrence-id'))
            return

        try:
            person = Person.objects.get(person_id=person_id)
            condition = ConditionOccurrence.objects.get(condition_occurrence_id=condition_occurrence_id)
        except (Person.DoesNotExist, ConditionOccurrence.DoesNotExist) as e:
            self.stdout.write(self.style.ERROR(f'Record not found: {e}'))
            return

        self.create_staging_observations(person, condition)

    def create_staging_concepts(self):
        """Create sample concepts for cancer staging following OMOP standards"""
        with transaction.atomic():
            # Create CANCER_STAGING vocabulary
            vocabulary, _ = Vocabulary.objects.get_or_create(
                vocabulary_id='CANCER_STAGING',
                defaults={
                    'vocabulary_name': 'Cancer Staging Vocabulary',
                    'vocabulary_concept_id': 50000000
                }
            )

            # Get observation domain
            domain, _ = Domain.objects.get_or_create(
                domain_id='Observation',
                defaults={
                    'domain_name': 'Observation',
                    'domain_concept_id': 27
                }
            )

            # Create concept class for staging
            concept_class, _ = ConceptClass.objects.get_or_create(
                concept_class_id='Staging',
                defaults={
                    'concept_class_name': 'Cancer Staging',
                    'concept_class_concept_id': 50000001
                }
            )

            # Cancer staging concepts that should use standard vocabularies
            staging_concepts = [
                # Primary site concepts (should use ICD-O-3 Topography in real implementation)
                (50001001, 'Primary site of cancer', 'Cancer primary site observation'),
                (50001002, 'Lung, NOS', 'Primary site: Lung, not otherwise specified'),
                (50001003, 'Breast, NOS', 'Primary site: Breast, not otherwise specified'),
                (50001004, 'Colon, NOS', 'Primary site: Colon, not otherwise specified'),
                
                # Histology concepts (should use ICD-O-3 Morphology in real implementation)  
                (50002001, 'Histologic type', 'Cancer histologic type observation'),
                (50002002, 'Adenocarcinoma, NOS', 'Adenocarcinoma, not otherwise specified'),
                (50002003, 'Squamous cell carcinoma, NOS', 'Squamous cell carcinoma, not otherwise specified'),
                
                # TNM staging concepts
                (50003001, 'Clinical T category', 'Clinical T category'),
                (50003002, 'Clinical N category', 'Clinical N category'), 
                (50003003, 'Clinical M category', 'Clinical M category'),
                (50003004, 'Pathologic T category', 'Pathologic T category'),
                (50003005, 'Pathologic N category', 'Pathologic N category'),
                (50003006, 'Pathologic M category', 'Pathologic M category'),
                
                # Stage group concepts
                (50004001, 'Clinical stage group', 'Overall clinical stage group'),
                (50004002, 'Pathologic stage group', 'Overall pathologic stage group'),
                
                # TNM values
                (50005001, 'T1', 'T1 tumor category'),
                (50005002, 'T2', 'T2 tumor category'),
                (50005003, 'T3', 'T3 tumor category'),
                (50005004, 'T4', 'T4 tumor category'),
                (50005005, 'N0', 'N0 nodal category'),
                (50005006, 'N1', 'N1 nodal category'),
                (50005007, 'N2', 'N2 nodal category'),
                (50005008, 'M0', 'M0 metastasis category'),
                (50005009, 'M1', 'M1 metastasis category'),
                (50005010, 'Stage I', 'Stage I overall'),
                (50005011, 'Stage II', 'Stage II overall'),
                (50005012, 'Stage III', 'Stage III overall'),
                (50005013, 'Stage IV', 'Stage IV overall'),
            ]

            for concept_id, concept_name, description in staging_concepts:
                concept, created = Concept.objects.get_or_create(
                    concept_id=concept_id,
                    defaults={
                        'concept_name': concept_name,
                        'domain': domain,
                        'vocabulary': vocabulary,
                        'concept_class': concept_class,
                        'standard_concept': 'S',
                        'concept_code': f'CS_{concept_id}',
                        'valid_start_date': '2024-01-01',
                        'valid_end_date': '2099-12-31',
                    }
                )
                if created:
                    self.stdout.write(f'Created concept: {concept_name}')

    def create_staging_observations(self, person, condition):
        """Create staging observations for a cancer condition"""
        from datetime import date
        
        # Get observation type concept (create a simple one if EHR not available)
        try:
            ehr_concept = Concept.objects.get(concept_id=32817)  # EHR
        except Concept.DoesNotExist:
            # Create a simple observation type concept
            try:
                domain = Domain.objects.get(domain_id='Type Concept')
            except Domain.DoesNotExist:
                domain, _ = Domain.objects.get_or_create(
                    domain_id='Type Concept',
                    defaults={'domain_name': 'Type Concept', 'domain_concept_id': 58}
                )
            
            try:
                vocabulary = Vocabulary.objects.get(vocabulary_id='Type Concept')
            except Vocabulary.DoesNotExist:
                vocabulary, _ = Vocabulary.objects.get_or_create(
                    vocabulary_id='Type Concept',
                    defaults={'vocabulary_name': 'Type Concept', 'vocabulary_concept_id': 32817}
                )
            
            try:
                concept_class = ConceptClass.objects.get(concept_class_id='Type Concept')
            except ConceptClass.DoesNotExist:
                concept_class, _ = ConceptClass.objects.get_or_create(
                    concept_class_id='Type Concept',
                    defaults={'concept_class_name': 'Type Concept', 'concept_class_concept_id': 32817}
                )
            
            ehr_concept, _ = Concept.objects.get_or_create(
                concept_id=32817,
                defaults={
                    'concept_name': 'EHR observation',
                    'domain': domain,
                    'vocabulary': vocabulary,
                    'concept_class': concept_class,
                    'standard_concept': 'S',
                    'concept_code': 'EHR',
                    'valid_start_date': '2024-01-01',
                    'valid_end_date': '2099-12-31',
                }
            )

        staging_data = [
            # Primary site
            {
                'observation_concept_id': 50001001,  # Primary site of cancer
                'value_as_concept_id': 50001002,     # Lung, NOS
                'value_as_string': 'Lung, NOS'
            },
            # Histology
            {
                'observation_concept_id': 50002001,  # Histologic type
                'value_as_concept_id': 50002002,     # Adenocarcinoma, NOS
                'value_as_string': 'Adenocarcinoma, NOS'
            },
            # Clinical TNM
            {
                'observation_concept_id': 50003001,  # Clinical T category
                'value_as_concept_id': 50005002,     # T2
                'value_as_string': 'T2'
            },
            {
                'observation_concept_id': 50003002,  # Clinical N category
                'value_as_concept_id': 50005005,     # N0
                'value_as_string': 'N0'
            },
            {
                'observation_concept_id': 50003003,  # Clinical M category
                'value_as_concept_id': 50005008,     # M0
                'value_as_string': 'M0'
            },
            # Clinical stage group
            {
                'observation_concept_id': 50004001,  # Clinical stage group
                'value_as_concept_id': 50005011,     # Stage II
                'value_as_string': 'Stage II'
            },
        ]

        with transaction.atomic():
            observation_id = 50000000  # Start with high ID to avoid conflicts
            
            for staging_item in staging_data:
                try:
                    obs_concept = Concept.objects.get(concept_id=staging_item['observation_concept_id'])
                    value_concept = Concept.objects.get(concept_id=staging_item['value_as_concept_id'])
                    
                    observation = Observation.objects.create(
                        observation_id=observation_id,
                        person=person,
                        observation_concept=obs_concept,
                        observation_date=condition.condition_start_date,
                        observation_datetime=condition.condition_start_datetime or 
                                           condition.condition_start_date.strftime('%Y-%m-%d 00:00:00'),
                        observation_type_concept=ehr_concept,
                        value_as_concept=value_concept,
                        value_as_string=staging_item['value_as_string'],
                        visit_occurrence=condition.visit_occurrence,
                        # Link to condition using observation_event_id
                        observation_event_id=condition.condition_occurrence_id,
                        # Note: obs_event_field_concept would link to CONDITION_OCCURRENCE_ID concept
                        # but for simplicity we'll leave it null in this demo
                    )
                    
                    observation_id += 1
                    self.stdout.write(f'Created observation: {obs_concept.concept_name} = {value_concept.concept_name}')
                    
                except Concept.DoesNotExist as e:
                    self.stdout.write(self.style.ERROR(f'Concept not found: {e}'))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'Error creating observation: {e}'))

        self.stdout.write(self.style.SUCCESS(f'Created staging observations for condition {condition.condition_occurrence_id}'))
