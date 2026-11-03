"""A symptom, complication or metastasis of a cancer is not the record's cancer (#2181)."""
import pytest

from omop_core.services.disease_episodes import disease_slug
from tests.test_primary_cancer import _condition, _person, _refreshed, ago

pytestmark = pytest.mark.django_db

BREAST, MYELOMA = 'Breast Cancer', 'Multiple myeloma (disorder)'


@pytest.mark.parametrize('later', [
    'Cancer-related fatigue',
    'Neoplastic (malignant) related fatigue',
    'Cancer associated pain',
    'Neoplasm related pain (acute) (chronic)',
    'Malignant bone pain',
    'Malignant cachexia',
    'Anemia in malignant neoplastic disease',
    'Anxiety associated with cancer diagnosis',
    'Myeloma-associated amyloidosis',
    'Neuropathy associated with multiple myeloma',
    'Pathologic fracture of vertebra at site of neoplasm',
    'Tumor lysis syndrome',
    'BAP1 tumor predisposition syndrome',
])
def test_a_later_symptom_or_complication_is_not_a_cancer(later):
    """The issue's patient A: the fatigue row is the newest on the record."""
    person = _person()
    _condition(person, BREAST, ago(600))
    _condition(person, later, ago(20))

    record = _refreshed(person)

    assert record.disease == BREAST
    assert record.disease_slugs == [disease_slug(BREAST)]


@pytest.mark.parametrize('later', [
    'Metastatic malignant neoplasm to bone',
    'Malignant neoplasm metastatic to adrenal gland',
    'Metastasis from malignant tumor of kidney',
    'Secondary malignant neoplasm of bone',
    'Secondary and unspecified malignant neoplasm of lymph nodes of multiple regions',
    'Widespread metastatic malignant neoplastic disease',
])
def test_a_later_metastasis_does_not_displace_the_primary(later):
    """The issue's patient B: myeloma, then a metastasis to bone."""
    person = _person()
    _condition(person, MYELOMA, ago(900))
    _condition(person, later, ago(20))

    record = _refreshed(person)

    assert record.disease_slug == disease_slug('Multiple Myeloma')
    assert record.disease_slugs == [record.disease_slug]


def test_a_metastasis_alone_is_still_the_cancer():
    """With no primary on the record, the metastasis is all that says it is a cancer."""
    person = _person()
    _condition(person, 'Metastatic malignant neoplasm to bone', ago(100))
    _condition(person, 'Cancer-related fatigue', ago(20))

    assert _refreshed(person).disease == 'Metastatic malignant neoplasm to bone'


@pytest.mark.parametrize('cancer', [
    'Metastatic malignant melanoma',
    'Metastatic non-small cell lung cancer',
    'Extranodal marginal zone B-cell lymphoma of mucosa-associated lymphoid tissue',
    'Acute myeloid leukemia with myelodysplasia-related changes',
])
def test_a_cancer_that_names_its_type_still_counts(cancer):
    """Metastatic, associated and related are not markers on their own."""
    person = _person()
    _condition(person, BREAST, ago(900))
    _condition(person, cancer, ago(20))

    record = _refreshed(person)

    assert record.disease_slugs == [disease_slug(cancer), disease_slug(BREAST)]


def test_only_symptoms_derive_no_cancer():
    person = _person(disease=None)
    _condition(person, 'Cancer-related fatigue', ago(20))
    _condition(person, 'Cancer associated pain', ago(10))

    record = _refreshed(person)

    assert record.disease_slugs == []
    assert not record.disease_slug
