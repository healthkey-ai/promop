import json
from io import StringIO

import pytest
from django.core.management import call_command

from omop_core.models import SourceCodeConceptMapping
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def _loinc_concept(concept_id, code):
    return ConceptFactory(
        concept_id=concept_id, concept_code=code,
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
        domain=DomainFactory(domain_id='Measurement'), standard_concept='S',
    )


def _write_hk_labs_fixtures(tmp_path):
    """Create minimal hk-labs fixture files under tmp_path."""
    common_dir = tmp_path / 'backend' / 'apps' / 'labs' / 'data'
    common_dir.mkdir(parents=True)
    common_file = common_dir / 'loinc_common.json'
    common_file.write_text(json.dumps({
        '_meta': {},
        'codes': [
            {'loinc_code': '6690-2', 'loinc_short_name': 'WBC', 'loinc_default_unit': '10^3/uL', 'value_type': 'numeric'},
            {'loinc_code': '718-7', 'loinc_short_name': 'Hemoglobin', 'loinc_default_unit': 'g/dL', 'value_type': 'numeric'},
        ],
    }))

    fixture_dir = tmp_path / 'backend' / 'apps' / 'labs' / 'fixtures'
    fixture_dir.mkdir(parents=True, exist_ok=True)
    manual_file = fixture_dir / 'curated_aliases_manual.json'
    manual_file.write_text(json.dumps([
        {'loinc_num': '110593-1', 'alias': 'b-Pregnanediol', 'source_field': 'curated_audit'},
    ]))
    return tmp_path


def test_creates_approved_mappings(tmp_path):
    _loinc_concept(1001, '6690-2')
    _loinc_concept(1002, '718-7')
    _loinc_concept(1003, '110593-1')
    root = _write_hk_labs_fixtures(tmp_path)

    out = StringIO()
    call_command('import_hklabs_crossmaps', f'--hk-labs-root={root}', stdout=out)

    assert SourceCodeConceptMapping.objects.count() == 3
    wbc = SourceCodeConceptMapping.objects.get(source_code='wbc')
    assert wbc.status == 'approved'
    assert wbc.origin_system == 'HK-LABS'
    assert wbc.source == 'HK-LABS'
    assert wbc.origin == 'import'
    assert wbc.source_vocabulary_id == ''
    assert wbc.destination_vocabulary_id == 'LOINC'
    assert wbc.target_concept.concept_code == '6690-2'

    preg = SourceCodeConceptMapping.objects.get(source_code='b-pregnanediol')
    assert preg.target_concept.concept_code == '110593-1'


def test_idempotent(tmp_path):
    _loinc_concept(1001, '6690-2')
    _loinc_concept(1002, '718-7')
    root = _write_hk_labs_fixtures(tmp_path)

    call_command('import_hklabs_crossmaps', f'--hk-labs-root={root}', stdout=StringIO())
    assert SourceCodeConceptMapping.objects.count() == 2
    call_command('import_hklabs_crossmaps', f'--hk-labs-root={root}', stdout=StringIO())
    assert SourceCodeConceptMapping.objects.count() == 2


def test_skips_missing_loinc(tmp_path):
    # No concepts created — all targets will be missing
    root = _write_hk_labs_fixtures(tmp_path)
    out = StringIO()
    call_command('import_hklabs_crossmaps', f'--hk-labs-root={root}', stdout=out)
    assert SourceCodeConceptMapping.objects.count() == 0
    assert 'missing target 3' in out.getvalue()


def test_dry_run(tmp_path):
    _loinc_concept(1001, '6690-2')
    _loinc_concept(1002, '718-7')
    root = _write_hk_labs_fixtures(tmp_path)

    out = StringIO()
    call_command('import_hklabs_crossmaps', f'--hk-labs-root={root}', '--dry-run', stdout=out)
    assert SourceCodeConceptMapping.objects.count() == 0
    assert 'Would create 2' in out.getvalue()
