"""Explicit confirmations of the two derived negative states (#5666).

`no_active_infection_status` and `no_other_active_malignancies` are formulas
over recorded facts. A formula cannot distinguish "never assessed" from
"confirmed negative", and a generic PATCH is replaced by the next derivation.
This endpoint stores an attestation with a snapshot; while it exists the
confirmation wins, and withdrawing it returns the value to the source facts.

Only the negative is confirmed. A recorded active infection, or more than one
active malignancy, is the source fact to correct; the endpoint refuses to
overwrite it rather than turning a contradiction into an answer.
"""
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response

from omop_core.models import ExplicitNegativeAnswer, PatientRecord

FIELDS = {
    'no_active_infection_status': 'active_infection_status',
    'no_other_active_malignancies': 'active_malignancies',
}


class NegativeAnswerSerializer(serializers.Serializer):
    field = serializers.ChoiceField(choices=sorted(FIELDS))
    action = serializers.ChoiceField(choices=['confirm_negative', 'withdraw'])
    expected_value = serializers.BooleanField(allow_null=True)
    expected_source = serializers.JSONField(allow_null=True)


def source_value(record, field):
    return getattr(record, FIELDS[field])


def derived_value(record, field):
    if field == 'no_active_infection_status':
        source = record.active_infection_status
        return None if source is None else not source
    source = record.active_malignancies
    return None if source is None else len(source) <= 1


def conflict_detail(record, field):
    if field == 'no_active_infection_status':
        if record.active_infection_status is True:
            return 'An active infection is recorded; correct that fact before confirming none.'
        return None
    if len(record.active_malignancies or []) > 1:
        return 'More than one active malignancy is recorded; correct that list before confirming none.'
    return None


def write_revisions(record, previous, request):
    from omop_core.models import RecordRevision
    RecordRevision.objects.bulk_create([
        RecordRevision(
            patient_record=record, field=field,
            old_value=str(old), new_value=str(getattr(record, field)),
            changed_by=str(request.user.pk),
        )
        for field, old in previous.items() if old != getattr(record, field)
    ])


def write_answer(request, record, *, source, revision_writer=write_revisions):
    serializer = NegativeAnswerSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    field = data['field']
    with transaction.atomic():
        record = PatientRecord.objects.select_for_update().get(pk=record.pk)
        if (getattr(record, field) != data['expected_value']
                or source_value(record, field) != data['expected_source']):
            return Response(
                {'detail': 'The answer or its source changed. Reload before confirming.'},
                status=409,
            )
        previous = {field: getattr(record, field)}
        if data['action'] == 'confirm_negative':
            detail = conflict_detail(record, field)
            if detail:
                return Response({'detail': detail}, status=400)
            setattr(record, field, True)
            ExplicitNegativeAnswer.objects.update_or_create(
                person_id=record.person_id, field=field,
                defaults={
                    'answer': True,
                    'confirmed_at': timezone.now(),
                    'source': source,
                },
            )
        else:
            # Back to the source fact rather than a stale explicit value.
            setattr(record, field, derived_value(record, field))
            ExplicitNegativeAnswer.objects.filter(
                person_id=record.person_id, field=field,
            ).delete()
        record.save(update_fields=[field, 'updated_at'])
        revision_writer(record, previous, request)
    return Response({
        'field': field,
        'value': getattr(record, field),
        'confirmed': data['action'] == 'confirm_negative',
        'source': source_value(record, field),
    })
