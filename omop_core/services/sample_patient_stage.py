"""Stage completion for explicitly selected synthetic cohorts only."""
import hashlib
from datetime import date

from django.db import transaction

from omop_core.models import Observation, PatientRecord
from omop_core.services.genomics_catalog import disease_code
from omop_core.services.patient_record_service import (
    SAMPLE_STAGE_SOURCE_VALUE, _get_staging_data,
)
from omop_core.services.pk import next_pk
from omop_core.signals import suppress_patient_record_refresh

SAMPLE_ORG_DISEASES = {
    'synthea-bc': 'BC', 'abc-foundation': 'BC', 'bbc-foundation': 'BC',
    'synthea-mm': 'MM', 'synthea-fl': 'FL',
}
_STAGE_CHOICES = {
    'BC': ('I', 'IIA', 'IIB', 'IIIA', 'IIIB', 'IV'),
    'MM': ('ISS I', 'ISS II', 'ISS III'),
    'FL': ('I', 'II', 'III', 'IV'),
    'MCL': ('I', 'II', 'III', 'IV'),
    'CLL': ('Rai 0', 'Rai I', 'Rai II', 'Rai III', 'Rai IV'),
}


def ensure_sample_patient_stage(record, *, disease=None, asserted_stage=None, dry_run=False):
    """Return (stage, source); persist a missing stage as a labeled OMOP fact.

    Caller must explicitly select sample patients. Real source facts take
    precedence, then the existing projection, then a deterministic synthetic
    value. Never invent a vocabulary concept or relabel a synthetic fact as EHR.
    The per-patient lock prevents concurrent reruns from inserting duplicates.
    """
    with transaction.atomic(), suppress_patient_record_refresh():
        if not dry_run:
            record = PatientRecord.objects.select_for_update(of=('self',)).select_related(
                'person', 'organization').get(pk=record.pk)
        staging = _get_staging_data(record.person)
        stage = staging.get('stage')
        if stage:
            return stage, 'existing OMOP'
        stage = (asserted_stage or record.stage or '').strip()
        if stage.lower() in {'true', 'false', 'yes', 'no', 'unknown', 'n/a'}:
            stage = ''
        source = 'bundle stage' if asserted_stage else 'preserved projection'
        if not stage:
            disease = disease or SAMPLE_ORG_DISEASES.get(
                record.organization.slug.lower() if record.organization else '') or disease_code(record.disease)
            choices = _STAGE_CHOICES.get(disease)
            if not choices:
                return None, 'unsupported disease'
            if disease == 'BC':
                metastasis = staging.get('distant_metastasis_stage') or ''
                if metastasis.startswith('M1'):
                    choices = ('IV',)
                elif metastasis.startswith('M0'):
                    choices = choices[:-1]
            index = int(hashlib.sha256(f'{record.person_id}:{disease}'.encode()).hexdigest(), 16)
            stage = choices[index % len(choices)]
            source = 'synthetic fallback'
        if not dry_run:
            Observation.objects.create(
                observation_id=next_pk(Observation, 'observation_id'),
                person=record.person,
                observation_concept_id=0,
                observation_type_concept_id=0,
                observation_date=record.diagnosis_date or date.today(),
                observation_source_value=SAMPLE_STAGE_SOURCE_VALUE,
                qualifier_source_value=source,
                value_as_string=stage,
            )
        return stage, source
