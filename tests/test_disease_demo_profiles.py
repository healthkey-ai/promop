from datetime import date
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command, CommandError

from omop_core.models import PatientRecord, Observation, Measurement
from omop_core.services.flipi import calculate_flipi, normalize_grade
from omop_core.services.patient_record_service import refresh_patient_record, recompute_patient_record_fields
from omop_core.services.sample_disease_profiles import profile_values, complete_demo_fhir_bundle
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.api.serializers import PatientRecordSerializer
from tests.factories import PatientRecordFactory, OrganizationFactory, MeasurementFactory, ConceptFactory

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('factors,expected', [(None,(None,None)),('',(0,'Low')),('age',(1,'Low')),('age,stage',(2,'Intermediate')),('age,stage,ldh',(3,'High')),('age,stage,hemoglobin,nodalAreas,ldh',(5,'High')),('age,age,Elevated LDH',(2,'Intermediate'))])
def test_flipi_counts_each_recognized_basis_once(factors,expected):
    assert calculate_flipi(factors)==expected
    with pytest.raises(ValueError): calculate_flipi('unrecognized')


@pytest.mark.parametrize('grade,expected',[('Grade 3a (>15 centroblasts/HPF)','3A'),('3b','3B'),(2,'2')])
def test_grade_preserves_subclassification(grade,expected):
    assert normalize_grade(grade)==expected


def test_score_is_computed_and_options_are_writable_without_omop_refresh():
    record=PatientRecordFactory(flipi_score_options=None)
    serializer=PatientRecordSerializer(record,data={'flipi_score_options':'age,stage,ldh','flipi_score':5},partial=True)
    assert serializer.is_valid(),serializer.errors
    assert 'flipi_score' not in serializer.validated_data
    with patch('omop_core.services.patient_record_service._build_snapshot') as snapshot:
        record=serializer.save()
        recompute_patient_record_fields(record,changed_fields={'flipi_score_options'})
        snapshot.assert_not_called()
    assert record.flipi_score==3
    assert record.flipi_risk_category=='High'


def test_backfill_recovers_legacy_grade_and_is_durable_without_touching_real_cohorts():
    record=PatientRecordFactory(organization=OrganizationFactory(slug='synthea-fl'),disease='Follicular Lymphoma',stage='IV',date_of_birth=date(1950,1,1),tumor_grade=None)
    other=PatientRecordFactory(tumor_grade=None)
    with suppress_patient_record_refresh():
        MeasurementFactory(person=record.person,measurement_concept_id=0,measurement_source_value='44648-4',value_as_string='Grade 3A',value_as_number=None)
    call_command('backfill_sample_disease_profiles',stdout=StringIO())
    record.refresh_from_db();assert record.tumor_grade is None
    call_command('backfill_sample_disease_profiles',confirm=True,stdout=StringIO())
    record.refresh_from_db();other.refresh_from_db()
    assert record.tumor_grade=='3A'
    assert record.gelf_criteria_status in {'Met','Not Met'}
    assert record.flipi_score==calculate_flipi(record.flipi_score_options)[0]
    assert other.tumor_grade is None
    before=(Observation.objects.count(),Measurement.objects.count())
    call_command('backfill_sample_disease_profiles',confirm=True,stdout=StringIO())
    assert (Observation.objects.count(),Measurement.objects.count())==before
    refreshed=refresh_patient_record(record.person)
    assert refreshed.tumor_grade=='3A'
    assert refreshed.flipi_score==record.flipi_score
    assert refreshed.number_of_nodal_sites==record.number_of_nodal_sites


def test_bc_additional_tests_only_for_applicable_subgroups():
    base=dict(person_id=1,stage='II',nodes_stage='N0',date_of_birth=date(1950,1,1),diagnosis_date=date(2024,1,1),estrogen_receptor_status='Positive',progesterone_receptor_status='Positive',her2_status='Negative')
    values=profile_values(SimpleNamespace(**base),'BC')
    assert 'oncotype_dx_score' in values
    assert 'pd_l1_combined_positive_score' not in values
    base.update(stage='IV',estrogen_receptor_status='Negative',progesterone_receptor_status='Negative')
    values=profile_values(SimpleNamespace(**base),'BC')
    assert 'oncotype_dx_score' not in values
    assert values['pd_l1_assay']=='22C3'
    assert 'pd_l1_combined_positive_score' in values


def test_fl_marrow_and_gelf_are_not_inferred_from_nodal_count_alone():
    record=SimpleNamespace(person_id=1,stage='II',date_of_birth=date(1950,1,1),number_of_nodal_sites=6,
                           bulky_disease=False,largest_lymph_node_size=2,b_symptoms=False,splenomegaly=False,
                           platelet_count_thousand_per_ul=200,anc_thousand_per_ul=2,bone_marrow_involvement=True)
    values=profile_values(record,'FL')
    assert values['gelf_criteria_status']=='Not Met'
    assert 'bone_marrow_involvement' not in values  # preserve the recorded answer
    assert profile_values(SimpleNamespace(person_id=2,stage='II'),'FL')['bone_marrow_involvement'] is False


def test_mm_ratio_and_type_follow_existing_chains():
    record=SimpleNamespace(person_id=1,kappa_flc=100,lambda_flc=400)
    values=profile_values(record,'MM')
    assert values['kappa_lambda_ratio']==.25
    assert values['myeloma_type'].endswith('lambda')
    assert 'oncotype_dx_score' not in values


def test_generated_sources_use_local_namespace_and_keep_existing_grade():
    bundle={'entry':[{'resource':{'resourceType':'Patient','id':'p1','birthDate':'1950-01-01'}},
                     {'resource':{'resourceType':'Observation','subject':{'reference':'Patient/p1'},'code':{'coding':[{'code':'demo:tumor_grade'}]},'valueString':'3B'}}]}
    complete_demo_fhir_bundle(bundle,'FL')
    grades=[e['resource'] for e in bundle['entry'] if e['resource'].get('code',{}).get('coding',[{}])[0].get('code')=='demo:tumor_grade']
    assert len(grades)==1
    factors=[e['resource'] for e in bundle['entry'] if e['resource'].get('code',{}).get('coding',[{}])[0].get('code')=='demo:flipi_score_options']
    assert len(factors)==1
    assert factors[0]['code']['coding'][0]['system'].startswith('https://healthkey.ai/')


def test_api_factor_save_recomputes_score_without_reverse_refresh():
    from rest_framework.test import APIClient
    from patient_portal.models import Identity
    client=APIClient()
    client.force_authenticate(Identity.objects.create_user(email='flipi-editor@example.test',is_staff=True))
    record=PatientRecordFactory(disease='Follicular Lymphoma')
    with patch('omop_core.services.patient_record_service._build_snapshot') as snapshot:
        response=client.patch(f'/api/patient-info/{record.person_id}/',{'flipi_score_options':'age,stage,ldh','gelf_criteria_options':'large_mass'},format='json')
        assert response.status_code==200,response.data
        snapshot.assert_not_called()
    assert response.data['flipi_score']==3
    assert response.data['flipi_risk_category']=='High'
    assert response.data['gelf_criteria_status']=='Met'
    response=client.patch(f'/api/patient-info/{record.person_id}/',{'flipi_score_options':'','gelf_criteria_options':''},format='json')
    assert response.status_code==200,response.data
    assert response.data['flipi_score']==0
    assert response.data['gelf_criteria_status']=='Not Met'


@pytest.mark.django_db(transaction=True)
def test_generated_assessments_survive_real_fhir_import(tmp_path):
    import json
    from patient_portal.models import Identity
    from omop_core.services.sample_disease_profiles import profile_fhir_observation
    Identity.objects.create_user(email='demo-import@example.test',is_staff=True,is_superuser=True)
    resources=[{'resourceType':'Patient','id':'demo-import-patient','name':[{'given':['Demo'],'family':'Lymphoma'}],'gender':'female','birthDate':'1950-01-01'}]
    resources.extend(profile_fhir_observation('demo-import-patient',key,value,'2024-01-01') for key,value in {
        'tumor_grade':'3A','flipi_score_options':'age,stage,ldh','gelf_criteria_options':'large_mass',
        'bone_marrow_involvement':True,'number_of_nodal_sites':6,
    }.items())
    file=tmp_path/'generated.json'
    file.write_text(json.dumps({'resourceType':'Bundle','type':'collection','entry':[{'resource':r} for r in resources]}))
    output=StringIO()
    call_command('import_fhir_bundle',file=str(file),org_slug='synthea-fl',stdout=output,stderr=output)
    record=PatientRecord.objects.get(organization__slug='synthea-fl')
    assert record.tumor_grade=='3A', (output.getvalue(), list(Measurement.objects.values('measurement_source_value','value_source_value','value_as_string','value_as_number')))
    assert record.flipi_score==3
    assert record.gelf_criteria_status=='Met'
    assert record.bone_marrow_involvement is True
    assert record.number_of_nodal_sites==6
