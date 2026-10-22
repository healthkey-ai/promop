"""Disease relevance for extraction services, and their feedback on it (#1942).

See ``omop_core.services.disease_relevance`` for what a set contains, and
``docs/disease_relevance.md`` for the service contract.
"""
from django.db import transaction
from django.http import HttpResponseNotModified
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from omop_core.models import Concept, Person, RelevanceSignal
from omop_core.services import disease_relevance
from omop_core.services.genomics_catalog import disease_code
from patient_portal.api.permissions import (
    EtlWritePermission, ScopedTokenPermission, VocabReadPermission, is_service_token,
    service_token_scopes,
)

# Reads the patient's diseases and nothing else, so an extractor need not hold
# patient/*.read, which opens the whole record.
DISEASE_READ_SCOPE = 'system/disease.read'
_PATIENT_READ_SCOPES = frozenset({DISEASE_READ_SCOPE, 'patient/*.read', 'user/*.read'})
MAX_SIGNALS = 1000


class RelevanceReadPermission(VocabReadPermission):
    """Reference read scopes, or a service holding only the disease-read scope.

    The view then requires that scope (or patient access) for ``person_id``,
    and that scope alone reads only that.
    """

    def has_permission(self, request, view):
        if (is_service_token(request) and request.method in ('GET', 'HEAD', 'OPTIONS')
                and DISEASE_READ_SCOPE in service_token_scopes(request).split()):
            return True
        return super().has_permission(request, view)


def _etag(version):
    return f'"rel-{version}"'


def _etag_matches(header, etag):
    return any(tag.strip() in (etag, f'W/{etag}', '*') for tag in header.split(','))


def _may_read_diseases(request, person_id):
    if is_service_token(request):
        return bool(set(service_token_scopes(request).split()) & _PATIENT_READ_SCOPES)
    token = request.auth
    if hasattr(token, 'scope') and not (set(token.scope.split()) & _PATIENT_READ_SCOPES):
        return False
    from omop_core.authorization import can_access_patient
    return can_access_patient(request.user, person_id)


def _requested_diseases(request):
    """Disease codes from ?disease= (repeatable) or the patient's record."""
    person_id = request.query_params.get('person_id')
    raw = request.query_params.getlist('disease')
    if person_id and raw:
        raise ValidationError({'detail': 'Send disease or person_id, not both.'})
    if person_id:
        try:
            person_id = int(person_id)
        except ValueError:
            raise ValidationError({'person_id': 'Enter a whole number.'})
        if not _may_read_diseases(request, person_id):
            raise PermissionDenied(f'Reading a patient\'s diseases needs {DISEASE_READ_SCOPE} or patient access.')
        person = get_object_or_404(Person, person_id=person_id)
        return person_id, disease_relevance.patient_diseases(person)
    if not raw:
        raise ValidationError({'disease': 'Send at least one disease, or a person_id.'})
    codes = []
    for value in raw:
        code = disease_code(value) or value.strip().upper()
        if not disease_relevance.supported(code):
            raise ValidationError({'disease': f'Unsupported disease {value!r}. '
                                              f'Supported: {", ".join(disease_relevance.DISEASES)}.'})
        if code not in codes:
            codes.append(code)
    return None, codes


@extend_schema(
    summary='Disease relevance set',
    description=('Standard concepts that matter for one or more diseases, from approved field '
                 'mappings, the genomics priority catalog and accepted curation signals. Pin '
                 '`version` on a run; it changes whenever the content does. With `person_id`, '
                 'the sets of every supported cancer on the record are united.'),
    parameters=[
        OpenApiParameter('disease', str, many=True, description='BC, FL, MM, CLL or MCL (names accepted). Repeatable.'),
        OpenApiParameter('person_id', int, description=f'Use this patient\'s diseases. Needs {DISEASE_READ_SCOPE}.'),
    ],
    responses=inline_serializer('DiseaseRelevance', {
        'version': serializers.CharField(),
        'diseases': serializers.ListField(child=serializers.CharField()),
        'person_id': serializers.IntegerField(required=False),
        'concepts': serializers.ListField(child=serializers.DictField()),
        'unmapped_fields': serializers.DictField(),
    }),
)
@api_view(['GET'])
@permission_classes([RelevanceReadPermission])
def disease_relevance_view(request):
    person_id, codes = _requested_diseases(request)
    # The version is a hash of the content, so the content is built either way;
    # a match saves the transfer, not the work.
    payload = disease_relevance.relevance(codes)
    etag = _etag(f'{payload["version"]}-{"+".join(codes)}')
    if _etag_matches(request.META.get('HTTP_IF_NONE_MATCH', ''), etag):
        response = HttpResponseNotModified()
        response['ETag'] = etag
        return response
    if person_id is not None:
        payload['person_id'] = person_id
    response = Response(payload)
    response['ETag'] = etag
    response['Cache-Control'] = 'private, no-cache'
    return response


class _SignalSerializer(serializers.Serializer):
    disease = serializers.CharField()
    concept_id = serializers.IntegerField()
    document_count = serializers.IntegerField(min_value=1)
    document_total = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    period = serializers.CharField(max_length=32)

    def validate_disease(self, value):
        code = disease_code(value) or value.strip().upper()
        if not disease_relevance.supported(code):
            raise serializers.ValidationError(f'Unsupported disease {value!r}.')
        return code

    def validate(self, attrs):
        total = attrs.get('document_total')
        if total is not None and total < attrs['document_count']:
            raise serializers.ValidationError({'document_total': 'Must be at least document_count.'})
        return attrs


def _signal_payload(signal):
    return {
        'id': signal.id, 'source': signal.source, 'disease': signal.disease,
        'concept_id': signal.concept_id, 'period': signal.period,
        'document_count': signal.document_count, 'document_total': signal.document_total,
        'status': signal.status, 'reviewed_at': signal.reviewed_at, 'created_at': signal.created_at,
    }


def _source(request):
    service_id = getattr(request.auth, 'service_id', None)
    return service_id or (getattr(request.user, 'email', None) or 'service')[:100]


def _receive_signals(request):
    rows = request.data if isinstance(request.data, list) else [request.data]
    if len(rows) > MAX_SIGNALS:
        return Response({'detail': f'At most {MAX_SIGNALS} signals per request.'},
                        status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    serializer = _SignalSerializer(data=rows, many=True)
    serializer.is_valid(raise_exception=True)
    rows = serializer.validated_data
    known = set(Concept.objects.filter(concept_id__in={r['concept_id'] for r in rows})
                .values_list('concept_id', flat=True))
    unknown = sorted({r['concept_id'] for r in rows} - known)
    if unknown:
        raise ValidationError({'concept_id': f'Unknown concept(s): {", ".join(map(str, unknown))}.'})
    source = _source(request)
    created = updated = 0
    with transaction.atomic():
        decided = {(d, c): s for d, c, s in RelevanceSignal.objects.filter(
            disease__in={r['disease'] for r in rows},
            concept_id__in={r['concept_id'] for r in rows},
            status__in=('accepted', 'rejected'),
        ).order_by('reviewed_at').values_list('disease', 'concept_id', 'status')}
        for row in rows:
            # A re-sent period converges; a decided pair keeps its decision.
            _, was_created = RelevanceSignal.objects.update_or_create(
                source=source, disease=row['disease'], concept_id=row['concept_id'], period=row['period'],
                defaults={'document_count': row['document_count'],
                          'document_total': row.get('document_total')},
                create_defaults={'document_count': row['document_count'],
                                 'document_total': row.get('document_total'),
                                 'status': decided.get((row['disease'], row['concept_id']), 'proposed')},
            )
            created += was_created
            updated += not was_created
    return Response({'created': created, 'updated': updated}, status=status.HTTP_201_CREATED)


@extend_schema(
    summary='Absent-from-relevance signals',
    description=('POST: an extractor reports concepts that resolved cleanly but are not in a '
                 'disease\'s relevance set, as aggregate document counts (no PHI). One object or '
                 'an array; re-sending a period updates it. GET: the curator queue.'),
    request=_SignalSerializer(many=True),
    parameters=[
        OpenApiParameter('status', str, description='GET: proposed (default), accepted, rejected or all.'),
        OpenApiParameter('disease', str, description='GET: limit to one disease.'),
    ],
)
@api_view(['GET', 'POST'])
@permission_classes([EtlWritePermission])
def relevance_signals(request):
    if request.method == 'POST':
        return _receive_signals(request)
    from patient_portal.api.views import _can_manage_field_mappings
    if is_service_token(request) or not _can_manage_field_mappings(request.user):
        raise PermissionDenied('Professional access required.')
    queryset = RelevanceSignal.objects.order_by('disease', '-document_count', 'concept_id')
    wanted = request.query_params.get('status', 'proposed')
    if wanted != 'all':
        queryset = queryset.filter(status=wanted)
    if request.query_params.get('disease'):
        queryset = queryset.filter(disease=disease_code(request.query_params['disease'])
                                   or request.query_params['disease'].upper())
    signals = list(queryset[:500])
    names = dict(Concept.objects.filter(concept_id__in={s.concept_id for s in signals})
                 .values_list('concept_id', 'concept_name'))
    return Response({'results': [{**_signal_payload(s), 'concept_name': names.get(s.concept_id)}
                                 for s in signals]})


@extend_schema(
    summary='Accept or reject a relevance signal',
    request=inline_serializer('RelevanceSignalReview', {'status': serializers.ChoiceField(
        choices=['proposed', 'accepted', 'rejected'])}),
)
@api_view(['PATCH'])
@permission_classes([ScopedTokenPermission])
def relevance_signal_review(request, signal_id):
    from patient_portal.api.views import _can_approve_mappings
    if is_service_token(request) or not _can_approve_mappings(request.user):
        raise PermissionDenied('Accepting a relevance signal needs approval rights.')
    choice = request.data.get('status')
    if choice not in ('proposed', 'accepted', 'rejected'):
        raise ValidationError({'status': 'Choose proposed, accepted or rejected.'})
    signal = get_object_or_404(RelevanceSignal, pk=signal_id)
    reviewed = None if choice == 'proposed' else timezone.now()
    reviewer = None if choice == 'proposed' else request.user
    # The decision is about the (disease, concept) pair, so it applies to
    # every period's report of it.
    RelevanceSignal.objects.filter(disease=signal.disease, concept_id=signal.concept_id).update(
        status=choice, reviewer=reviewer, reviewed_at=reviewed, updated_at=timezone.now())
    signal.refresh_from_db()
    return Response(_signal_payload(signal))
