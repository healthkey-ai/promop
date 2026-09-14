"""Complete disease status only for explicitly selected synthetic patients."""
import hashlib
from datetime import date

from django.db import transaction

from omop_core.models import ConditionOccurrence, Observation, PatientRecord
from omop_core.services.patient_record_service import (
    SAMPLE_DISEASE_STATUS_SOURCE_VALUE, condition_clinical_status, meaningful_disease_status,
)
from omop_core.services.pk import next_pk
from omop_core.signals import suppress_patient_record_refresh

# A varied demo distribution, not a clinical inference from stage or therapy.
SAMPLE_DISEASE_STATUSES = ('active', 'remission', 'relapse', 'stable', 'progressing')


def ensure_sample_patient_disease_status(record, *, dry_run=False):
    """Return (status, source), preserving recorded values and labeling fallbacks.

    Caller must explicitly select sample patients. Writes go directly to the
    projection and a durable OMOP fallback; no full patient refresh is needed.
    """
    if meaningful_disease_status(record.condition_clinical_status):
        return record.condition_clinical_status, 'existing patient status'
    with transaction.atomic(), suppress_patient_record_refresh():
        if not dry_run:
            record = PatientRecord.objects.select_for_update(of=('self',)).get(pk=record.pk)
        if meaningful_disease_status(record.condition_clinical_status):
            return record.condition_clinical_status, 'existing patient status'
        condition = ConditionOccurrence.objects.filter(person_id=record.person_id).select_related(
            'condition_status_concept').order_by('-condition_start_date').first()
        status = condition_clinical_status(condition)
        source = 'existing OMOP'
        if not status:
            rows = Observation.objects.filter(
                person_id=record.person_id, observation_source_value=SAMPLE_DISEASE_STATUS_SOURCE_VALUE,
                is_erroneous=False,
            ).order_by('-observation_date', '-observation_id')
            status = next((o.value_as_string for o in rows if meaningful_disease_status(o.value_as_string)), None)
        if not status:
            if meaningful_disease_status(record.progression):
                status = record.progression.strip()[:50]
                source = 'preserved progression'
            else:
                index = int(hashlib.sha256(f'disease-status:{record.person_id}'.encode()).hexdigest(), 16)
                status = SAMPLE_DISEASE_STATUSES[index % len(SAMPLE_DISEASE_STATUSES)]
                source = 'synthetic fallback'
            if not dry_run:
                Observation.objects.create(
                    observation_id=next_pk(Observation, 'observation_id'),
                    person_id=record.person_id, observation_concept_id=0,
                    observation_type_concept_id=0, observation_date=date.today(),
                    observation_source_value=SAMPLE_DISEASE_STATUS_SOURCE_VALUE,
                    qualifier_source_value=source, value_as_string=status,
                )
        if not dry_run:
            PatientRecord.objects.filter(pk=record.pk).update(condition_clinical_status=status)
        return status, source
