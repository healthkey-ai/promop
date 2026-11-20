from types import SimpleNamespace

from omop_core.services.source_system_context import source_system_context


EPIC_OID = 'urn:oid:1.2.840.114350.1.13.88.2.7.5.737384.2050'


def mapping(facilities, *, system=EPIC_OID, organization=None):
    return SimpleNamespace(
        organization_id=1 if organization else None,
        organization=SimpleNamespace(name=organization) if organization else None,
        source_vocabulary_id=system,
        source_metadata={'facilities': facilities},
    )


def facility(name, level='brand', confidence='high'):
    return {'name': name, 'level': level, 'confidence': confidence}


def test_unique_high_confidence_facility_is_observed_not_verified():
    context = source_system_context(mapping([
        facility('UPMC'), facility('UPMC', level='tenant'),
        facility('Patient alias', level='unknown', confidence='low'),
    ]))
    assert context == {'kind': 'observed', 'label': 'UPMC', 'names': ['UPMC'], 'name_count': 1}


def test_shared_oid_is_never_assigned_one_hospital_globally():
    upmc = source_system_context(mapping([facility('UPMC')]))
    hillman = source_system_context(mapping([facility('Hillman Cancer Center')]))
    ambiguous = source_system_context(mapping([facility('UPMC'), facility('Hillman Cancer Center')]))
    assert upmc['label'] == 'UPMC'
    assert hillman['label'] == 'Hillman Cancer Center'
    assert ambiguous == {
        'kind': 'multiple', 'label': 'Multiple observed facilities',
        'names': ['UPMC', 'Hillman Cancer Center'], 'name_count': 2,
    }


def test_shared_epic_url_is_also_attributed_per_code_not_per_system():
    system = 'http://open.epic.com/FHIR/StructureDefinition/observation-flowsheet-id'
    first = source_system_context(mapping([facility('Hospital A')], system=system))
    second = source_system_context(mapping([facility('Hospital B')], system=system))
    assert first['label'] == 'Hospital A'
    assert second['label'] == 'Hospital B'


def test_alias_only_and_standard_systems_are_unattributed():
    alias = [facility('Connection alias', level='unknown', confidence='low')]
    assert source_system_context(mapping(alias))['kind'] == 'none'
    assert source_system_context(mapping([facility('Hospital')], system='LOINC'))['kind'] == 'none'


def test_verified_organization_is_distinct_from_observed_facilities():
    context = source_system_context(mapping([facility('Attached Hospital')], organization='Verified Org'))
    assert context == {'kind': 'verified', 'label': 'Verified Org', 'names': [], 'name_count': 0}


def test_malformed_facility_metadata_does_not_claim_hospital():
    row = mapping([])
    row.source_metadata = {'facilities': [{'name': 'Hospital', 'level': 'brand'}]}
    assert source_system_context(row)['kind'] == 'none'
    row.source_metadata = {'facilities': 'Hospital'}
    assert source_system_context(row)['kind'] == 'none'
