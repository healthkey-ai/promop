"""#2453: the (person, source_record_key) unique constraint on measurement and
observation."""
from datetime import date

import pytest
from django.db import IntegrityError, transaction

from omop_core.models import Measurement, Observation
from tests.factories import ConceptFactory, PersonFactory


pytestmark = pytest.mark.django_db


def _row(model, pk, person, concept, key):
    if model is Measurement:
        return Measurement(
            measurement_id=pk, person=person, measurement_concept=concept,
            measurement_type_concept=concept, measurement_date=date(2026, 11, 1),
            source_record_key=key,
        )
    return Observation(
        observation_id=pk, person=person, observation_concept=concept,
        observation_type_concept=concept, observation_date=date(2026, 11, 1),
        source_record_key=key,
    )


@pytest.mark.parametrize('model', [Measurement, Observation])
def test_unkeyed_rows_never_collide(model):
    person, concept = PersonFactory(), ConceptFactory()
    model.objects.bulk_create(
        [_row(model, pk, person, concept, None) for pk in (1, 2)])
    assert model.objects.filter(person=person).count() == 2


@pytest.mark.parametrize('model', [Measurement, Observation])
def test_a_key_is_unique_per_person(model):
    person, other, concept = PersonFactory(), PersonFactory(), ConceptFactory()
    key = 'urn:issuer:facility/Observation/abc'
    model.objects.bulk_create([
        _row(model, 1, person, concept, key),
        _row(model, 2, other, concept, key),
    ])
    with pytest.raises(IntegrityError), transaction.atomic():
        model.objects.bulk_create([_row(model, 3, person, concept, key)])
