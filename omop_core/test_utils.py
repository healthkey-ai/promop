"""Test-only OMOP fixture helpers shared by Django test modules."""

from omop_core.models import Concept, ConceptClass, Domain, Vocabulary


def ensure_test_concept_zero():
    """Create the valid OMOP ``concept_id=0`` sentinel for an isolated test DB.

    Tests frequently use zero for unmapped concept foreign keys.  Do not rely on
    another test class or a vocabulary-load command having happened to seed it.
    This helper is deliberately test-only; production initialization remains the
    responsibility of migrations and vocabulary-loading commands.
    """
    vocabulary, _ = Vocabulary.objects.get_or_create(
        vocabulary_id="None",
        defaults={
            "vocabulary_name": "None",
            "vocabulary_reference": "",
            "vocabulary_version": "",
            "vocabulary_concept_id": 0,
        },
    )
    domain, _ = Domain.objects.get_or_create(
        domain_id="Metadata",
        defaults={"domain_name": "Metadata", "domain_concept_id": 0},
    )
    concept_class, _ = ConceptClass.objects.get_or_create(
        concept_class_id="Undefined",
        defaults={"concept_class_name": "Undefined", "concept_class_concept_id": 0},
    )
    return Concept.objects.update_or_create(
        concept_id=0,
        defaults={
            "concept_name": "No matching concept",
            "domain": domain,
            "vocabulary": vocabulary,
            "concept_class": concept_class,
            "concept_code": "No matching concept",
            "valid_start_date": "1970-01-01",
            "valid_end_date": "2099-12-31",
        },
    )[0]
