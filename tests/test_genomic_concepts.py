"""The origin/interpretation maps are concept_ids with the names they claim.

The old maps stored SNOMED *codes* in id columns and the codes meant something
else entirely ("Struck by" by pathogenic). These tests pin the ids to their
names and the label normalisation the widget needs (cancerbot #5671).
"""
from io import StringIO

import pytest
from django.core.management import call_command

from omop_core.models import Concept, Measurement
from omop_core.services.genomic_concepts import (
    MUTATION_INTERPRETATION_CONCEPTS,
    MUTATION_ORIGIN_CONCEPTS,
    normalise_interpretation,
    normalise_origin,
)
from tests.factories import ConceptFactory, MeasurementFactory, PersonFactory

pytestmark = pytest.mark.django_db

EXPECTED_NAMES = {
    45880308: 'Germline',
    45880309: 'Somatic',
    45884094: 'Pathogenic',
    21498358: 'Likely pathogenic',
    45880925: 'Benign',
    21498359: 'Likely benign',
    21498841: 'Uncertain significance',
}


def test_the_maps_use_every_expected_id():
    assert set(MUTATION_ORIGIN_CONCEPTS) == {45880308, 45880309}
    assert set(MUTATION_INTERPRETATION_CONCEPTS) == set(EXPECTED_NAMES) - {
        45880308, 45880309,
    }


def test_value_to_id_is_pinned_so_a_swap_cannot_pass():
    # The previous bug was a value/id swap; asserting only the id set would let
    # benign and likely_benign trade places and pass every round trip.
    assert MUTATION_ORIGIN_CONCEPTS[45880308] == 'germline'
    assert MUTATION_ORIGIN_CONCEPTS[45880309] == 'somatic'
    assert MUTATION_INTERPRETATION_CONCEPTS[45884094] == 'pathogenic'
    assert MUTATION_INTERPRETATION_CONCEPTS[21498358] == 'likely_pathogenic'
    assert MUTATION_INTERPRETATION_CONCEPTS[45880925] == 'benign'
    assert MUTATION_INTERPRETATION_CONCEPTS[21498359] == 'likely_benign'
    assert MUTATION_INTERPRETATION_CONCEPTS[21498841] == 'vus'


def test_the_old_wrong_ids_are_gone():
    assert not ({255395001, 255461003} & set(MUTATION_ORIGIN_CONCEPTS))
    assert not ({30166007, 10828004, 42425007} & set(MUTATION_INTERPRETATION_CONCEPTS))


def test_no_mutation_detected_stays_unmapped_until_reviewed():
    # A negative finding cannot be represented by a writer with no status
    # field; mapping it to "Not detected" would record status="present".
    assert normalise_interpretation('No mutation detected') is None
    assert 45880296 not in MUTATION_INTERPRETATION_CONCEPTS


def test_the_loaded_vocabulary_has_the_expected_names_when_present():
    """Locally this is the real check; CI's empty vocabulary skips it."""
    if not Concept.objects.filter(concept_id=45884094).exists():
        pytest.skip('vocabulary not loaded in this test database')
    for concept_id, name in EXPECTED_NAMES.items():
        assert Concept.objects.get(pk=concept_id).concept_name == name


def test_widget_labels_normalise_to_the_stored_codes():
    assert normalise_interpretation('Pathogenic') == 'pathogenic'
    assert normalise_interpretation('Likely pathogenic') == 'likely_pathogenic'
    assert normalise_interpretation('Likely benign') == 'likely_benign'
    assert normalise_interpretation('VUS') == 'vus'
    assert normalise_interpretation('Uncertain') == 'vus'
    assert normalise_interpretation('uncertain_significance') == 'vus'
    assert normalise_interpretation('variant_of_uncertain_significance') == 'vus'
    assert normalise_interpretation('No mutation detected') is None
    assert normalise_interpretation('anything else') is None
    assert normalise_origin('Germline') == 'germline'
    assert normalise_origin('Somatic') == 'somatic'
    assert normalise_origin('other') is None


def test_audit_finds_a_source_only_genomic_measurement():
    """measurement_concept_id=0 + gene LOINC in source_value is still genomic.

    The derivation discovers that representation, so an audit that only reads
    measurement_concept would report "none" for a wrong fact it can see.
    """
    person = PersonFactory()
    zero = ConceptFactory(concept_id=0, concept_code='0', concept_name='No matching concept')
    wrong = ConceptFactory(
        concept_id=30166007, concept_code='SNOMED:30166007',
        concept_name='Struck by',
    )
    MeasurementFactory(
        person=person,
        measurement_concept=zero,
        measurement_source_value='21636-6',
        value_as_concept=wrong,
    )
    out = StringIO()

    call_command('audit_genomic_concept_ids', stdout=out)

    text = out.getvalue()
    assert 'measurements=1' in text
    assert 'Struck by' in text
    assert 'none' not in text


def _mutation_row(person, source_value, gene, value_source_value=None):
    question = Concept.objects.filter(concept_code='36908-2').first() or ConceptFactory(
        concept_id=45876022, concept_code='36908-2', concept_name='Gene mutations tested for')
    origin = Concept.objects.filter(pk=255395001).first() or ConceptFactory(
        concept_id=255395001, concept_code='255395001', concept_name='Origins')
    struck = Concept.objects.filter(pk=30166007).first() or ConceptFactory(
        concept_id=30166007, concept_code='30166007', concept_name='Struck by')
    return MeasurementFactory(
        person=person, measurement_concept=question, measurement_source_value=source_value,
        qualifier_source_value=gene, value_as_string='c.68_69delAG',
        qualifier_concept=origin, value_as_concept=struck,
        value_source_value=value_source_value,
    )


def test_legacy_cb_profile_rows_keep_their_origin_and_interpretation():
    """#2428: rows the previous CB-profile writer stored with SNOMED codes in
    the id columns must still read as the finding the patient recorded."""
    from omop_core.services.patient_record_service import _get_genetic_mutations
    person = PersonFactory()
    _mutation_row(person, 'cb-profile:genetic-mutation', 'BRCA1')

    [finding] = _get_genetic_mutations(person)['genetic_mutations']

    assert finding['origin'] == 'germline'
    assert finding['interpretation'] == 'pathogenic'


def test_the_legacy_ids_mean_nothing_on_a_fact_that_writer_did_not_author():
    """The same integers on an imported fact are real concepts ("Origins",
    "Struck by"), not an origin or interpretation."""
    from omop_core.services.patient_record_service import _get_genetic_mutations
    person = PersonFactory()
    _mutation_row(person, '36908-2', 'BRCA2')

    [finding] = _get_genetic_mutations(person)['genetic_mutations']

    assert 'origin' not in finding
    assert 'interpretation' not in finding


def test_the_earlier_promop_record_writer_rows_are_legacy_too():
    """2feb62c0..9034cc0b wrote the same ids under the generic question, marked
    by value_source_value rather than measurement_source_value."""
    from omop_core.services.patient_record_service import _get_genetic_mutations
    person = PersonFactory()
    _mutation_row(person, '36908-2', 'BRCA2', value_source_value='promop-genetic-mutation:BRCA2')

    [finding] = _get_genetic_mutations(person)['genetic_mutations']

    assert finding['origin'] == 'germline'
    assert finding['interpretation'] == 'pathogenic'
