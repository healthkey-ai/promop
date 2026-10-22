"""Things the patient adds to their own record: medications and supplements,
non-cancer conditions, and procedures — plus a note on any medication.

Each item is a real OMOP row typed as patient self-report (concept 32865) with
a PATIENT_SELF provenance row, so PRomop's derivations see it like any other
fact. What OMOP has no column for (where it was done, an unknown date) is kept
in a PatientStatement beside the row. Only rows the patient authored can be
changed or removed; record rows never can.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response

from omop_core.models import ConditionOccurrence, DrugExposure, ProcedureOccurrence, ProvenanceRecord
from omop_core.services.pk import next_pk
from patient_portal.models import PatientStatement

from .records import _group_key, medication_summary_by_key, procedures
from .sources import PATIENT_SELF_REPORT_TYPE_CONCEPT_ID as SELF_REPORT, row_sources
from .views import _CANCER_WORDS, PhrView, diagnoses

S = PatientStatement
NAME_MAX = 50  # OMOP *_source_value columns
NOTE_MAX = 500
UNITS = ['mg', 'mcg', 'g', 'mL', 'IU', 'tablet(s)', 'capsule(s)', 'puff(s)', 'unit(s)', 'mg/m²']
FREQUENCIES = [
    'once daily', 'twice daily', 'three times daily', 'four times daily', 'every other day', 'weekly',
    'twice weekly', 'every 2 weeks', 'monthly', 'every 3 months', 'as needed',
]


def _sig(data) -> str:
    amount = data.get('amount')
    dose = f"{amount.normalize():f} {data.get('unit') or ''}".strip() if amount is not None else ''
    return ' · '.join(part for part in (dose, data.get('frequency') or '') if part)


def _authored_by_patient(model, rows) -> bool:
    sources = row_sources(model, rows, type_attr=_TYPE_ATTR[model], date_attr=_DATE_ATTR[model])
    return bool(rows) and all(sources[r.pk]['kind'] == 'patient' for r in rows)


_TYPE_ATTR = {DrugExposure: 'drug_type_concept', ConditionOccurrence: 'condition_type_concept',
              ProcedureOccurrence: 'procedure_type_concept'}
_DATE_ATTR = {DrugExposure: 'drug_exposure_start_date', ConditionOccurrence: 'condition_start_date',
              ProcedureOccurrence: 'procedure_date'}
_PK = {DrugExposure: 'drug_exposure_id', ConditionOccurrence: 'condition_occurrence_id',
       ProcedureOccurrence: 'procedure_occurrence_id'}


def _create(request, model, **fields):
    row = model.objects.create(**{_PK[model]: next_pk(model, _PK[model])}, **fields)
    ProvenanceRecord.objects.create(
        content_type=ContentType.objects.get_for_model(model), object_id=row.pk, source='PATIENT_SELF',
        source_user_id=f'{request.user.issuer}|{request.user.sub}',
    )
    return row


def _remember(request, person, subject, key, *, note='', details=None):
    S.objects.update_or_create(
        person=person, subject=subject, subject_key=key,
        defaults={'status': S.ENTRY, 'note': note, 'details': details or {}, 'created_by': request.user},
    )


def _name(value: str) -> str:
    return ' '.join(value.split())


# ---------------------------------------------------------------- medications


class MedicationEntrySerializer(serializers.Serializer):
    name = serializers.CharField(max_length=NAME_MAX)
    amount = serializers.DecimalField(max_digits=9, decimal_places=3, min_value=0, required=False, allow_null=True)
    unit = serializers.ChoiceField(choices=UNITS, required=False, allow_blank=True)
    frequency = serializers.ChoiceField(choices=FREQUENCIES, required=False, allow_blank=True)
    note = serializers.CharField(max_length=NOTE_MAX, required=False, allow_blank=True, default='')

    def validate_name(self, value):
        value = _name(value)
        if not value:
            raise serializers.ValidationError('Name the medication or supplement.')
        return value


class DoseSerializer(MedicationEntrySerializer):
    name = None


class MedicationsCreateView(PhrView):
    """POST: add a medication or supplement the patient takes, starting today."""

    http_method_names = ['post', 'options']

    def post(self, request):
        person, record, error = self.resolve(request)
        if error:
            return error
        data = MedicationEntrySerializer(data=request.data)
        data.is_valid(raise_exception=True)
        with transaction.atomic():
            row = _create(
                request, DrugExposure, person=person, drug_concept_id=0, drug_source_value=data.validated_data['name'],
                drug_exposure_start_date=timezone.localdate(), drug_type_concept_id=SELF_REPORT,
                sig=_sig(data.validated_data) or None,
            )
            key = _group_key(row)
            if data.validated_data['note'].strip():
                _remember(request, person, S.SUBJECT_MEDICATION_NOTE, key, note=data.validated_data['note'].strip())
        return Response(medication_summary_by_key(person, key), status=201)


class OwnMedicationView(PhrView):
    """PATCH: a new dose for a medication the patient added (kept as dose
    history). DELETE: remove it from the record entirely."""

    http_method_names = ['patch', 'delete', 'options']

    def _own_rows(self, person, item_id):
        rows = [r for r in DrugExposure.objects.filter(person=person, is_erroneous=False)
                .select_related('drug_concept', 'visit_occurrence') if _group_key(r) == item_id]
        return rows if _authored_by_patient(DrugExposure, rows) else None

    def patch(self, request, item_id: str):
        person, record, error = self.resolve(request)
        if error:
            return error
        rows = self._own_rows(person, item_id)
        if rows is None:
            return Response({'detail': 'Only a medication you added can be changed.'}, status=403)
        data = DoseSerializer(data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        today = timezone.localdate()
        open_rows = sorted((r for r in rows if r.drug_exposure_end_date is None),
                           key=lambda r: r.drug_exposure_start_date)
        if not open_rows:
            return Response({'detail': 'This medication has stopped; it can no longer be changed.'}, status=400)
        latest = open_rows[-1]
        sig = _sig(data.validated_data) or None
        with transaction.atomic():
            if latest.drug_exposure_start_date >= today:
                latest.sig = sig
                latest.save(update_fields=['sig'])
            else:
                latest.drug_exposure_end_date = today - timedelta(days=1)
                latest.save(update_fields=['drug_exposure_end_date'])
                _create(request, DrugExposure, person=person, drug_concept_id=latest.drug_concept_id,
                        drug_source_value=latest.drug_source_value, drug_exposure_start_date=today,
                        drug_type_concept_id=SELF_REPORT, sig=sig)
        return Response(medication_summary_by_key(person, item_id))

    def delete(self, request, item_id: str):
        person, record, error = self.resolve(request)
        if error:
            return error
        rows = self._own_rows(person, item_id)
        if rows is None:
            return Response({'detail': 'Only a medication you added can be removed.'}, status=403)
        with transaction.atomic():
            DrugExposure.objects.filter(pk__in=[r.pk for r in rows]).delete()
            S.objects.filter(person=person, subject__in=[S.SUBJECT_MEDICATION, S.SUBJECT_MEDICATION_NOTE],
                             subject_key=item_id).delete()
        return Response(status=204)


class NoteSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=NOTE_MAX, allow_blank=True)


class MedicationNoteView(PhrView):
    """PUT/DELETE the patient's own note on any medication."""

    http_method_names = ['put', 'delete', 'options']

    def put(self, request, item_id: str):
        person, record, error = self.resolve(request)
        if error:
            return error
        if medication_summary_by_key(person, item_id, with_statements=False) is None:
            return Response({'detail': 'Not found.'}, status=404)
        data = NoteSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        note = data.validated_data['note'].strip()
        if note:
            S.objects.update_or_create(person=person, subject=S.SUBJECT_MEDICATION_NOTE, subject_key=item_id,
                                       defaults={'status': S.NOTE, 'note': note, 'created_by': request.user})
        else:
            S.objects.filter(person=person, subject=S.SUBJECT_MEDICATION_NOTE, subject_key=item_id).delete()
        return Response(medication_summary_by_key(person, item_id))

    def delete(self, request, item_id: str):
        person, record, error = self.resolve(request)
        if error:
            return error
        S.objects.filter(person=person, subject=S.SUBJECT_MEDICATION_NOTE, subject_key=item_id).delete()
        return Response(status=204)


# ---------------------------------------------------------------- conditions and procedures


class ConditionEntrySerializer(serializers.Serializer):
    name = serializers.CharField(max_length=NAME_MAX)
    diagnosed = serializers.DateField(required=False, allow_null=True)

    def validate_name(self, value):
        value = _name(value)
        if not value:
            raise serializers.ValidationError('Name the condition.')
        if _CANCER_WORDS.search(value):
            raise serializers.ValidationError('Cancer diagnoses come from your connected records.')
        return value

    def validate_diagnosed(self, value):
        if value and value > timezone.localdate():
            raise serializers.ValidationError('The date can’t be in the future.')
        return value


class ProcedureEntrySerializer(serializers.Serializer):
    name = serializers.CharField(max_length=NAME_MAX)
    date = serializers.DateField(required=False, allow_null=True)
    where = serializers.CharField(max_length=120, required=False, allow_blank=True, default='')
    note = serializers.CharField(max_length=NOTE_MAX, required=False, allow_blank=True, default='')

    def validate_name(self, value):
        value = _name(value)
        if not value:
            raise serializers.ValidationError('Name the procedure.')
        return value

    def validate_date(self, value):
        if value and value > timezone.localdate():
            raise serializers.ValidationError('The date can’t be in the future.')
        return value


class _EntryView(PhrView):
    """POST adds; PATCH changes and DELETE removes a row the patient added."""

    http_method_names = ['post', 'patch', 'delete', 'options']

    model = None
    subject = ''
    serializer = None

    def build_fields(self, person, data) -> dict:
        raise NotImplementedError

    def entry_details(self, data) -> tuple[str, dict]:
        raise NotImplementedError

    def respond(self, person, record, pk):
        raise NotImplementedError

    def post(self, request, item_id=None):
        person, record, error = self.resolve(request)
        if error:
            return error
        data = self.serializer(data=request.data)
        data.is_valid(raise_exception=True)
        with transaction.atomic():
            row = _create(request, self.model, person=person, **self.build_fields(person, data.validated_data))
            note, details = self.entry_details(data.validated_data)
            _remember(request, person, self.subject, str(row.pk), note=note, details=details)
        return Response(self.respond(person, record, row.pk), status=201)

    def _own(self, person, item_id):
        if not str(item_id).isdigit():
            return None
        row = self.model.objects.filter(person=person, pk=int(item_id), is_erroneous=False).select_related(
            'visit_occurrence').first()
        return row if row is not None and _authored_by_patient(self.model, [row]) else None

    def patch(self, request, item_id: str):
        person, record, error = self.resolve(request)
        if error:
            return error
        row = self._own(person, item_id)
        if row is None:
            return Response({'detail': 'Only something you added can be changed.'}, status=403)
        data = self.serializer(data=request.data)
        data.is_valid(raise_exception=True)
        with transaction.atomic():
            for field, value in self.build_fields(person, data.validated_data).items():
                setattr(row, field, value)
            row.save()
            note, details = self.entry_details(data.validated_data)
            _remember(request, person, self.subject, str(row.pk), note=note, details=details)
        return Response(self.respond(person, record, row.pk))

    def delete(self, request, item_id: str):
        person, record, error = self.resolve(request)
        if error:
            return error
        row = self._own(person, item_id)
        if row is None:
            return Response({'detail': 'Only something you added can be removed.'}, status=403)
        with transaction.atomic():
            S.objects.filter(person=person, subject=self.subject, subject_key=str(row.pk)).delete()
            row.delete()
        return Response(status=204)


class ConditionEntryView(_EntryView):
    model = ConditionOccurrence
    subject = S.SUBJECT_CONDITION
    serializer = ConditionEntrySerializer

    def build_fields(self, person, data):
        return {'condition_concept_id': 0, 'condition_source_value': data['name'],
                'condition_start_date': data.get('diagnosed') or timezone.localdate(),
                'condition_type_concept_id': SELF_REPORT}

    def entry_details(self, data):
        return '', {'date_unknown': not data.get('diagnosed')}

    def respond(self, person, record, pk):
        return next(c for c in diagnoses(person, record)['other'] if c['id'] == f'condition-{pk}')


class ProcedureEntryView(_EntryView):
    model = ProcedureOccurrence
    subject = S.SUBJECT_PROCEDURE
    serializer = ProcedureEntrySerializer

    def build_fields(self, person, data):
        return {'procedure_concept_id': 0, 'procedure_source_value': data['name'],
                'procedure_date': data.get('date') or timezone.localdate(),
                'procedure_type_concept_id': SELF_REPORT}

    def entry_details(self, data):
        details = {'date_unknown': not data.get('date')}
        if data.get('where', '').strip():
            details['where'] = data['where'].strip()
        return data.get('note', '').strip(), details

    def respond(self, person, record, pk):
        return next(p for p in procedures(person, record)['procedures'] if p['id'] == pk)
