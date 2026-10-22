"""What the patient says about their record: confirming prescriptions,
saying they stopped one, and why a line of therapy ended.

Statements sit beside the record and never change it. Every write is checked
against the record as it is now (an ended prescription cannot be "taking"),
and deleting the statement is the undo.
"""
from __future__ import annotations

from datetime import date

from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response

from patient_portal.models import PatientStatement

from .records import medication_summary_by_key
from .therapy import all_lines
from .views import PhrView

S = PatientStatement
NOTE_MAX = 500

# The design's reasons for a line of therapy ending, in its order.
END_REASONS = {
    'stopped_working': 'My treatment stopped working',
    'side_effects': 'I had too many side effects',
    'too_expensive': 'It was too expensive (financial issues)',
    'too_far': 'It was too far from where I live (travel issues)',
    'insurance': 'My insurance stopped covering it',
    'finished': 'I finished the planned course',
    'doctor_change': 'My doctor recommended a change',
    'clinical_trial': 'I moved onto a clinical trial',
    'caregiving_work': 'Caregiving or work made it hard to keep up',
    'break': 'I chose to take a break',
    'other': 'Another reason',
}


class MedicationStatementSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[S.TAKING, S.NOT_TAKING, S.TOOK, S.NOT_TAKEN, S.STOPPED])
    note = serializers.CharField(max_length=NOTE_MAX, required=False, allow_blank=True, default='')
    stopped_on = serializers.DateField(required=False, allow_null=True, default=None)


class EndReasonSerializer(serializers.Serializer):
    reason = serializers.ChoiceField(choices=list(END_REASONS))
    note = serializers.CharField(max_length=NOTE_MAX, required=False, allow_blank=True, default='')


class _StatementView(PhrView):
    """PUT records or replaces the patient's statement; DELETE undoes it."""

    http_method_names = ['put', 'delete', 'options']
    subject: str

    def put(self, request, item_id: str):
        person, record, error = self.resolve(request)
        if error:
            return error
        return self.write(request, person, record, item_id)

    def delete(self, request, item_id: str):
        person, record, error = self.resolve(request)
        if error:
            return error
        if not self.exists(person, record, item_id):
            return Response({'detail': 'Not found.'}, status=404)
        S.objects.filter(person=person, subject=self.subject, subject_key=item_id).delete()
        return Response(status=204)

    def save(self, request, person, item_id, **fields):
        S.objects.update_or_create(
            person=person, subject=self.subject, subject_key=item_id,
            defaults={**fields, 'created_by': request.user},
        )


class MedicationStatementView(_StatementView):
    subject = S.SUBJECT_MEDICATION

    def exists(self, person, record, item_id):
        return medication_summary_by_key(person, item_id, with_statements=False) is not None

    def write(self, request, person, record, item_id):
        # Judge the answer against the record alone, not a previous answer.
        med = medication_summary_by_key(person, item_id, with_statements=False)
        if med is None:
            return Response({'detail': 'Not found.'}, status=404)
        data = MedicationStatementSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        status, note, stopped_on = (data.validated_data[k] for k in ('status', 'note', 'stopped_on'))

        current = med['status'] == 'current'
        if status in (S.TAKING, S.NOT_TAKING) and not current:
            return Response({'status': ['This prescription has ended: answer "took" or "not taken".']}, status=400)
        if status in (S.TOOK, S.NOT_TAKEN) and current:
            return Response({'status': ['This prescription is current: answer "taking" or "not taking".']}, status=400)
        if status == S.STOPPED:
            previous = S.objects.filter(person=person, subject=self.subject, subject_key=item_id).first()
            own = med['source']['kind'] == 'patient'
            if not current or not (own or (previous and previous.status in (S.TAKING, S.STOPPED))):
                return Response({'status': ['Only a medication you take can be stopped.']}, status=400)
            if stopped_on is None:
                return Response({'stopped_on': ['Say when you stopped.']}, status=400)
            if stopped_on > timezone.localdate() or stopped_on < date.fromisoformat(med['started']):
                return Response({'stopped_on': ['The date must be between the start date and today.']}, status=400)
        else:
            stopped_on = None
        self.save(request, person, item_id, status=status, note=note.strip(), stopped_on=stopped_on, reason='')
        return Response(medication_summary_by_key(person, item_id))


class TherapyReasonView(_StatementView):
    subject = S.SUBJECT_THERAPY_LINE

    def _line(self, person, record, item_id):
        # Any cancer's line (#1739), not only the primary one's.
        return next((line for line in all_lines(person, record) if line['id'] == item_id), None)

    def exists(self, person, record, item_id):
        return self._line(person, record, item_id) is not None

    def write(self, request, person, record, item_id):
        line = self._line(person, record, item_id)
        if line is None:
            return Response({'detail': 'Not found.'}, status=404)
        if line['current']:
            return Response({'detail': 'This line of therapy has not ended.'}, status=400)
        data = EndReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        self.save(request, person, item_id, status=S.END_REASON, reason=data.validated_data['reason'],
                  note=data.validated_data['note'].strip(), stopped_on=None)
        return Response(self._line(person, record, item_id))
