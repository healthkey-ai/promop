"""Migration 0327 reserves concept_id 2100007900 for the BCMA marker (#2427).

The id is a fixed contract, while runtime minting can already have handed it
out. A collision must stop the migration by name, and touch nothing.
"""
import importlib
import pytest
from django.apps import apps

from omop_core.models import Concept
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db

migration = importlib.import_module('omop_core.migrations.0327_hk_genomics_bcma_marker')
BCMA_ID = 2_100_007_900
BCMA_CODE = 'HK-MARKER:BCMA-EXPRESSION'


@pytest.fixture(autouse=True)
def _no_preseeded_marker():
    Concept.objects.filter(pk=BCMA_ID).delete()
    Concept.objects.filter(vocabulary_id='HK-Genomics', concept_code=BCMA_CODE).delete()


def test_a_vacant_id_is_seeded_and_a_rerun_is_idempotent():
    migration.seed_bcma_marker(apps, None)
    migration.seed_bcma_marker(apps, None)
    concept = Concept.objects.get(pk=BCMA_ID)
    assert (concept.vocabulary_id, concept.concept_code) == ('HK-Genomics', BCMA_CODE)


def test_an_unrelated_concept_on_the_reserved_id_stops_the_migration_by_name():
    occupant = ConceptFactory(concept_id=BCMA_ID, concept_code='LOCAL-MINT-44',
                              concept_name='A runtime mint')
    with pytest.raises(RuntimeError, match='already used by .*LOCAL-MINT-44'):
        migration.seed_bcma_marker(apps, None)
    occupant.refresh_from_db()
    assert occupant.concept_code == 'LOCAL-MINT-44'
    assert not Concept.objects.filter(vocabulary_id='HK-Genomics', concept_code=BCMA_CODE).exists()


def test_the_marker_under_another_id_stops_the_migration_by_name():
    from omop_core.models import Vocabulary
    migration.seed_bcma_marker(apps, None)
    Concept.objects.filter(pk=BCMA_ID).delete()
    elsewhere = ConceptFactory(concept_id=BCMA_ID + 5, concept_code=BCMA_CODE,
                               concept_name='BCMA protein expression',
                               vocabulary=Vocabulary.objects.get(vocabulary_id='HK-Genomics'))
    with pytest.raises(RuntimeError, match=f'under concept_id {BCMA_ID + 5}'):
        migration.seed_bcma_marker(apps, None)
    assert not Concept.objects.filter(pk=BCMA_ID).exists()
    assert Concept.objects.filter(pk=elsewhere.pk, concept_code=BCMA_CODE).exists()
