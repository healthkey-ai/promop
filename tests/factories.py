import factory
from omop_core.models import (
    Concept, Vocabulary, Domain, ConceptClass,
    Organization, Person, PatientRecord,
    Measurement, Observation, ConditionOccurrence, DrugExposure,
)


class OrganizationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Organization
        django_get_or_create = ('slug',)

    name = factory.Sequence(lambda n: f'Org {n}')
    slug = factory.Sequence(lambda n: f'org-{n}')


class VocabularyFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Vocabulary
        django_get_or_create = ('vocabulary_id',)

    vocabulary_id = 'LOINC'
    vocabulary_name = 'LOINC'
    vocabulary_reference = 'https://loinc.org'
    vocabulary_version = '2.73'
    vocabulary_concept_id = 0  # plain IntegerField, not a FK


class DomainFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Domain
        django_get_or_create = ('domain_id',)

    domain_id = 'Measurement'
    domain_name = 'Measurement'
    domain_concept_id = 0  # plain IntegerField


class ConceptClassFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ConceptClass
        django_get_or_create = ('concept_class_id',)

    concept_class_id = 'Lab Test'
    concept_class_name = 'Lab Test'
    concept_class_concept_id = 0  # plain IntegerField


class ConceptFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Concept
        django_get_or_create = ('concept_id',)

    concept_id = factory.Sequence(lambda n: 9_000_000 + n)
    concept_name = factory.Sequence(lambda n: f'Concept {n}')
    concept_code = factory.Sequence(lambda n: f'CODE-{n}')
    vocabulary = factory.SubFactory(VocabularyFactory)
    domain = factory.SubFactory(DomainFactory)
    concept_class = factory.SubFactory(ConceptClassFactory)
    standard_concept = 'S'
    valid_start_date = '1970-01-01'
    valid_end_date = '2099-12-31'
    invalid_reason = None


class PersonFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Person

    person_id = factory.Sequence(lambda n: n + 1)
    gender_concept = factory.SubFactory(ConceptFactory, concept_name='MALE', concept_code='M')
    year_of_birth = 1970
    race_concept = factory.SubFactory(ConceptFactory, concept_name='White', concept_code='RACE-WHITE')
    ethnicity_concept = factory.SubFactory(ConceptFactory, concept_name='Not Hispanic', concept_code='ETH-NOTHISP')
    location_id = None


class PatientRecordFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PatientRecord

    person = factory.SubFactory(PersonFactory)
    organization = factory.SubFactory(OrganizationFactory)
    disease = 'chronic lymphocytic leukemia'
    patient_age = 65
    gender = 'M'


class MeasurementFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Measurement

    measurement_id = factory.Sequence(lambda n: n + 1)
    person = factory.SubFactory(PersonFactory)
    measurement_concept = factory.SubFactory(ConceptFactory)
    measurement_date = '2024-01-15'
    measurement_type_concept = factory.SubFactory(
        ConceptFactory, concept_name='Lab result', concept_code='TYPE-LAB'
    )
    value_as_number = None
    value_as_string = None
    value_as_concept = None
    unit_concept = None
    qualifier_concept = None
    qualifier_source_value = None


class ObservationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Observation

    observation_id = factory.Sequence(lambda n: n + 1)
    person = factory.SubFactory(PersonFactory)
    observation_concept = factory.SubFactory(ConceptFactory)
    observation_date = '2024-01-15'
    observation_type_concept = factory.SubFactory(
        ConceptFactory, concept_name='Observation type', concept_code='TYPE-OBS'
    )
    value_as_number = None
    value_as_string = None
    value_as_concept = None


class ConditionOccurrenceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ConditionOccurrence

    condition_occurrence_id = factory.Sequence(lambda n: n + 1)
    person = factory.SubFactory(PersonFactory)
    condition_concept = factory.SubFactory(ConceptFactory)
    condition_start_date = '2022-06-01'
    condition_type_concept = factory.SubFactory(
        ConceptFactory, concept_name='Condition type', concept_code='TYPE-COND'
    )


class DrugExposureFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = DrugExposure

    drug_exposure_id = factory.Sequence(lambda n: n + 1)
    person = factory.SubFactory(PersonFactory)
    drug_concept = factory.SubFactory(ConceptFactory)
    drug_exposure_start_date = '2023-01-01'
    drug_exposure_end_date = '2023-06-30'
    drug_type_concept = factory.SubFactory(
        ConceptFactory, concept_name='Drug type', concept_code='TYPE-DRUG'
    )
