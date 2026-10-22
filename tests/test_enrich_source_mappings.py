"""Tests for the enrich_source_mappings management command."""
import pytest
from io import StringIO

from django.core.management import call_command

from omop_core.models import (
    Concept,
    SourceCodeConceptMapping,
    UmlsConcept,
    UmlsRelease,
    UmlsSourceCode,
)
from tests.factories import (
    ConceptClassFactory,
    ConceptFactory,
    DomainFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db


@pytest.fixture()
def loinc_vocab():
    return VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')


@pytest.fixture()
def measurement_domain():
    return DomainFactory(domain_id='Measurement')


@pytest.fixture()
def lab_class():
    return ConceptClassFactory(concept_class_id='Lab Test')


@pytest.fixture()
def loinc_concept(loinc_vocab, measurement_domain, lab_class):
    return ConceptFactory(
        concept_name='Glucose [Mass/volume] in Serum',
        concept_code='2345-7',
        vocabulary=loinc_vocab,
        domain=measurement_domain,
        concept_class=lab_class,
        standard_concept=None,
    )


@pytest.fixture()
def umls_preferred(loinc_vocab):
    release = UmlsRelease.objects.create(release_version='2024AA')
    cui = UmlsConcept.objects.create(
        cui='C0017725', preferred_name='Glucose measurement',
        release=release,
    )
    return UmlsSourceCode.objects.create(
        concept=cui, root_source='LNC', code='2345-7',
        term_type='PT', name='Glucose [Mass/volume] in Serum or Plasma',
        is_preferred=True,
    )


@pytest.fixture()
def bare_mapping(loinc_vocab):
    """An SCCM row with no source_concept, description, or UMLS name."""
    return SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='LOINC',
        source_code='2345-7',
        source_code_description='',
        umls_source_name='',
        status='proposed',
    )


class TestEnrichSourceMappings:
    def test_populates_all_three_fields(self, bare_mapping, loinc_concept, umls_preferred):
        out = StringIO()
        call_command('enrich_source_mappings', stdout=out)
        bare_mapping.refresh_from_db()

        assert bare_mapping.source_concept == loinc_concept
        assert bare_mapping.umls_source_name == 'Glucose [Mass/volume] in Serum or Plasma'
        assert bare_mapping.source_code_description == 'Glucose [Mass/volume] in Serum'

    def test_dry_run_does_not_write(self, bare_mapping, loinc_concept, umls_preferred):
        out = StringIO()
        call_command('enrich_source_mappings', '--dry-run', stdout=out)
        bare_mapping.refresh_from_db()

        assert bare_mapping.source_concept is None
        assert bare_mapping.umls_source_name == ''
        assert 'Would update' in out.getvalue()

    def test_does_not_overwrite_curator_description(self, bare_mapping, loinc_concept, umls_preferred):
        bare_mapping.source_code_description = 'Curator wrote this'
        bare_mapping.save()

        out = StringIO()
        call_command('enrich_source_mappings', stdout=out)
        bare_mapping.refresh_from_db()

        # UMLS name set, source_concept set, but description preserved
        assert bare_mapping.source_concept == loinc_concept
        assert bare_mapping.umls_source_name == 'Glucose [Mass/volume] in Serum or Plasma'
        assert bare_mapping.source_code_description == 'Curator wrote this'

    def test_force_description_overwrites(self, bare_mapping, loinc_concept, umls_preferred):
        bare_mapping.source_code_description = 'Curator wrote this'
        bare_mapping.save()

        out = StringIO()
        call_command('enrich_source_mappings', '--force-description', stdout=out)
        bare_mapping.refresh_from_db()

        assert bare_mapping.source_code_description == 'Glucose [Mass/volume] in Serum'

    def test_umls_name_used_when_no_omop_concept(self, bare_mapping, umls_preferred):
        """When OMOP has no concept for the code, UMLS name fills description."""
        out = StringIO()
        call_command('enrich_source_mappings', stdout=out)
        bare_mapping.refresh_from_db()

        assert bare_mapping.source_concept is None
        assert bare_mapping.umls_source_name == 'Glucose [Mass/volume] in Serum or Plasma'
        assert bare_mapping.source_code_description == 'Glucose [Mass/volume] in Serum or Plasma'[:255]

    def test_idempotent(self, bare_mapping, loinc_concept, umls_preferred):
        """Running twice produces the same result."""
        call_command('enrich_source_mappings', stdout=StringIO())
        call_command('enrich_source_mappings', stdout=StringIO())
        bare_mapping.refresh_from_db()

        assert bare_mapping.source_concept == loinc_concept
        assert bare_mapping.umls_source_name == 'Glucose [Mass/volume] in Serum or Plasma'

    def test_no_rows_with_vocabulary(self):
        """No crash when there are no SCCM rows with a source vocabulary."""
        out = StringIO()
        call_command('enrich_source_mappings', stdout=out)
        assert 'No rows' in out.getvalue()
