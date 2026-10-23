"""Deliberate answers, separate from full-form autosave (#1988)."""
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response

from omop_core.models import (
    PatientRecord, PreExistingConditionCategory, PreexistingConditionsAttestation,
)
from omop_core.services.pre_existing_conditions import condition_categories


class AnswerSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=['confirm_none', 'withdraw', 'set_categories'])
    expected_flag = serializers.BooleanField(allow_null=True)
    expected_categories = serializers.JSONField(allow_null=True)
    categories = serializers.ListField(child=serializers.CharField(), required=False)

    def validate(self, data):
        if data['action'] == 'set_categories':
            if 'categories' not in data:
                raise serializers.ValidationError({'categories': 'This field is required.'})
            codes = data['categories']
            known = set(PreExistingConditionCategory.objects.filter(
                code__in=codes).values_list('code', flat=True))
            if set(codes) - known:
                raise serializers.ValidationError({'categories': 'Select known category codes.'})
        elif 'categories' in data:
            raise serializers.ValidationError({'categories': 'Only allowed with set_categories.'})
        return data


def write_answer(request, record, *, source, revision_writer):
    serializer = AnswerSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    with transaction.atomic():
        record = PatientRecord.objects.select_for_update().get(pk=record.pk)
        if (record.no_pre_existing_conditions != data['expected_flag']
                or record.preexisting_conditions != data['expected_categories']):
            return Response({'detail': 'The answer changed. Reload before confirming.'}, status=409)
        previous = {
            'no_pre_existing_conditions': record.no_pre_existing_conditions,
            'preexisting_conditions': record.preexisting_conditions,
        }
        if data['action'] == 'confirm_none':
            # Malformed source data must not be silently interpreted as empty.
            if (record.preexisting_conditions not in (None, [])
                    or condition_categories(record.preexisting_conditions)):
                raise serializers.ValidationError({
                    'categories': 'Remove recorded categories before confirming none.',
                })
            record.no_pre_existing_conditions = True
            PreexistingConditionsAttestation.objects.update_or_create(
                person_id=record.person_id,
                defaults={'confirmed_at': timezone.now(), 'source': source},
            )
        else:
            record.no_pre_existing_conditions = None
            if data['action'] == 'set_categories':
                record.preexisting_conditions = list(dict.fromkeys(data['categories']))
            PreexistingConditionsAttestation.objects.filter(person_id=record.person_id).delete()
        # No formula or OMOP inference: these are explicitly authored categories.
        record.save(update_fields=['no_pre_existing_conditions', 'preexisting_conditions', 'updated_at'])
        revision_writer(record, previous, request)
    return Response({
        'no_pre_existing_conditions': record.no_pre_existing_conditions,
        'preexisting_conditions': record.preexisting_conditions,
    })
