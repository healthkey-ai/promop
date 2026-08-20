import pytest

from omop_core.models import Concept
from omop_core.test_utils import ensure_test_concept_zero


pytestmark = pytest.mark.django_db


def test_concept_zero_fixture_creates_valid_omop_sentinel():
    Concept.objects.filter(concept_id=0).delete()

    concept = ensure_test_concept_zero()

    assert concept.concept_id == 0
    assert concept.concept_name == "No matching concept"
    assert concept.vocabulary_id == "None"
    assert concept.domain_id == "Metadata"
    assert concept.concept_class_id == "Undefined"
