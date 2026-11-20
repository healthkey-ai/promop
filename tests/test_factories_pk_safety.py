"""Fixture IDs share the runtime allocator, without touching DB-free builds."""
from datetime import date

import pytest

from omop_core.models import Measurement, Observation
from omop_core.services.pk import next_pk
from tests.factories import ConceptFactory, MeasurementFactory, ObservationFactory, PersonFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(params=[(MeasurementFactory, Measurement, 'measurement'),
                        (ObservationFactory, Observation, 'observation')])
def fact_setup(request):
    factory, model, prefix = request.param
    values = {
        'person': PersonFactory(), f'{prefix}_concept': ConceptFactory(),
        f'{prefix}_type_concept': ConceptFactory(), f'{prefix}_date': date(2024, 1, 15),
    }
    return factory, model, model._meta.pk.name, values


def runtime_fact(model, pk_name, values, payload):
    row = model(**{pk_name: next_pk(model, pk_name)}, **values, value_as_string=payload)
    row._skip_patient_record_refresh = True
    row.save(force_insert=True)
    return row


def test_factory_and_runtime_allocator_cannot_collide(fact_setup):
    factory, model, pk_name, values = fact_setup
    original = runtime_fact(model, pk_name, values, 'original runtime payload')
    # Per-call override reproduces the old collision; it is not a global counter
    # reset, ID-range workaround or dependency on test execution order.
    fixture = factory(__sequence=original.pk - 1, **values, value_as_string='fixture payload')
    later = runtime_fact(model, pk_name, values, 'later runtime payload')
    assert len({original.pk, fixture.pk, later.pk}) == 3
    assert model.objects.filter(pk__in=[original.pk, fixture.pk, later.pk]).count() == 3
    original.refresh_from_db()
    assert original.value_as_string == 'original runtime payload'


@pytest.mark.parametrize('mode', ['field', 'alias', 'alias_with_null_field', 'field_with_null_alias', 'both_same'])
def test_factory_preserves_explicit_pk(fact_setup, mode):
    factory, model, pk_name, values = fact_setup
    explicit = next_pk(model, pk_name)
    kwargs = {
        'field': {pk_name: explicit},
        'alias': {'pk': explicit},
        'alias_with_null_field': {'pk': explicit, pk_name: None},
        'field_with_null_alias': {pk_name: explicit, 'pk': None},
        'both_same': {pk_name: explicit, 'pk': explicit},
    }[mode]
    row = factory(**values, **kwargs)
    assert row.pk == explicit
    assert model.objects.filter(pk=explicit).exists()
    assert factory(**values).pk > explicit


def test_conflicting_explicit_pk_aliases_are_rejected(fact_setup):
    factory, model, pk_name, values = fact_setup
    before = model.objects.count()
    with pytest.raises(ValueError, match='Conflicting primary keys'):
        factory(**values, **{pk_name: 100, 'pk': 101})
    assert model.objects.count() == before


def test_unsaved_factory_build_does_not_query_database(fact_setup, django_assert_num_queries):
    factory, _, _, values = fact_setup
    with django_assert_num_queries(0):
        row = factory.build(**values)
    assert row.pk is None
    assert row._state.adding
