from collections import defaultdict
from django.contrib.contenttypes.models import ContentType
from django.db import connection, transaction
from django.db.models import Q as models_Q
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from omop_core.models import (
    CareSite, Concept, LoincClass, LoincCodeClass,
    Measurement, PatientInfo, ProvenanceRecord, VisitOccurrence,
)
from patient_portal.api.permissions import ScopedTokenPermission, get_request_org

from .serializers import LabResultCardSerializer, LabValueSerializer, MeasurementUpdateSerializer

MAX_VALUES_PER_CONCEPT = 10


def _load_visit_provenance(visit_ids):
    """Load lab_name + report_filename for a set of visit_occurrence_ids."""
    if not visit_ids:
        return {}

    visits = VisitOccurrence.objects.filter(
        visit_occurrence_id__in=visit_ids
    ).values('visit_occurrence_id', 'care_site_id', 'visit_source_value')

    care_site_ids = set()
    visit_data = {}
    for v in visits:
        visit_data[v['visit_occurrence_id']] = v
        if v['care_site_id']:
            care_site_ids.add(v['care_site_id'])

    care_site_names = {}
    if care_site_ids:
        care_site_names = dict(
            CareSite.objects.filter(care_site_id__in=care_site_ids)
            .values_list('care_site_id', 'care_site_name')
        )

    result = {}
    for vid, v in visit_data.items():
        result[vid] = {
            'lab_name': care_site_names.get(v['care_site_id']) if v['care_site_id'] else None,
            'report_filename': v['visit_source_value'],
        }
    return result


def _resolve_person_id(request):
    """Return (person_id, error_response) from query param or authenticated user."""
    from omop_core.authorization import can_access_patient
    from patient_portal.models import PatientUser

    person_id = request.query_params.get('person_id')
    if person_id:
        try:
            pid = int(person_id)
        except (ValueError, TypeError):
            return None, Response(
                {'detail': 'person_id must be an integer.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Verify caller has access to this patient
        if request.user and request.user.is_authenticated:
            own_pid = None
            try:
                own_pid = PatientUser.objects.get(identity=request.user).person_id
            except PatientUser.DoesNotExist:
                pass
            if pid != own_pid and not can_access_patient(request.user, pid):
                org = get_request_org(request)
                if org is None or not PatientInfo.objects.filter(
                    person_id=pid, organization=org,
                ).exists():
                    return None, Response(
                        {'detail': 'Access denied.'},
                        status=status.HTTP_403_FORBIDDEN,
                    )
        return pid, None

    if not request.user or not request.user.is_authenticated:
        return None, Response(
            {'detail': 'person_id query parameter is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        pu = PatientUser.objects.get(identity=request.user)
        return pu.person_id, None
    except PatientUser.DoesNotExist:
        pass

    # Fallback: resolve by email on PatientInfo
    email_qs = PatientInfo.objects.filter(email=request.user.email)
    org = get_request_org(request)
    if org is not None:
        email_qs = email_qs.filter(organization=org)
    pi = email_qs.first()
    if pi is None:
        return None, Response(
            {'detail': 'No patient record linked to your account.'},
            status=status.HTTP_404_NOT_FOUND,
        )
    return pi.person_id, None


class LabResultsPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 200


def _compute_status(value, range_low, range_high):
    if value is None:
        return 'unknown'
    if range_low is not None and value < range_low:
        return 'below'
    if range_high is not None and value > range_high:
        return 'above'
    if range_low is not None or range_high is not None:
        return 'in_range'
    return 'unknown'


MEASUREMENT_TYPE_LABELS = {
    32817: 'ehr',
    32883: 'document_extraction',
    32865: 'patient_self_report',
}


from django.core.cache import cache as django_cache


def _build_category_cache():
    """Build LOINC concept_code → category display name cache."""
    key = 'loinc_category_cache'
    result = django_cache.get(key)
    if result is not None:
        return result
    class_names = dict(LoincClass.objects.values_list('code', 'display_name'))
    code_to_class = dict(LoincCodeClass.objects.values_list('loinc_num', 'loinc_class_id'))
    result = {
        loinc_num: class_names[class_code]
        for loinc_num, class_code in code_to_class.items()
        if class_code in class_names
    }
    django_cache.set(key, result, timeout=3600)
    return result


_CONCEPT_SUMMARY_SQL = """
WITH eff AS (
    SELECT
        CASE
            WHEN measurement_concept_id = 0
                 AND measurement_source_concept_id IS NOT NULL
            THEN measurement_source_concept_id
            ELSE measurement_concept_id
        END AS eff_id,
        measurement_date
    FROM measurement
    WHERE person_id = %s
)
SELECT
    eff.eff_id              AS effective_concept_id,
    MAX(eff.measurement_date) AS latest_date,
    c.concept_code,
    c.concept_name,
    c.vocabulary_id,
    COALESCE(
        lc.display_name,
        CASE WHEN c.vocabulary_id IN ('LOINC', 'HK-Labs')
             THEN 'Uncategorized'
             ELSE 'Other'
        END
    ) AS category
FROM eff
JOIN concept c ON c.concept_id = eff.eff_id
LEFT JOIN loinc_code_class lcc
       ON lcc.loinc_num = c.concept_code
      AND c.vocabulary_id = 'LOINC'
LEFT JOIN loinc_class lc
       ON lc.code = lcc.loinc_class_code
GROUP BY eff.eff_id, c.concept_code, c.concept_name, c.vocabulary_id,
         COALESCE(
             lc.display_name,
             CASE WHEN c.vocabulary_id IN ('LOINC', 'HK-Labs')
                  THEN 'Uncategorized'
                  ELSE 'Other'
             END
         )
ORDER BY category, latest_date DESC
"""


class ResultsSummaryView(APIView):
    """
    GET /api/lab-results/summary/?page=1&page_size=50[&person_id=X]

    person_id is optional — when omitted, resolved from the authenticated user's email.
    """
    permission_classes = [ScopedTokenPermission]

    def get(self, request):
        person_id, err = _resolve_person_id(request)
        if err:
            return err

        org = get_request_org(request)
        if org is not None:
            if not PatientInfo.objects.filter(person_id=person_id, organization=org).exists():
                return Response(
                    {'detail': 'Person not found in your organization.'},
                    status=status.HTTP_404_NOT_FOUND,
                )

        summaries = self._get_concept_summaries(person_id)

        paginator = LabResultsPagination()
        page = paginator.paginate_queryset(summaries, request)

        cards = self._hydrate_page(person_id, page)
        serializer = LabResultCardSerializer(cards, many=True)
        return paginator.get_paginated_response(serializer.data)

    @staticmethod
    def _get_concept_summaries(person_id):
        with connection.cursor() as cursor:
            cursor.execute(_CONCEPT_SUMMARY_SQL, [person_id])
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    _HYDRATE_SQL = """
    WITH ranked AS (
        SELECT
            m.measurement_id,
            CASE
                WHEN m.measurement_concept_id = 0
                     AND m.measurement_source_concept_id IS NOT NULL
                THEN m.measurement_source_concept_id
                ELSE m.measurement_concept_id
            END AS eff_id,
            m.measurement_date,
            m.value_as_number,
            m.value_as_string,
            m.range_low,
            m.range_high,
            m.measurement_type_concept_id,
            m.unit_concept_id,
            m.unit_source_value,
            m.visit_occurrence_id,
            ROW_NUMBER() OVER (
                PARTITION BY
                    CASE
                        WHEN m.measurement_concept_id = 0
                             AND m.measurement_source_concept_id IS NOT NULL
                        THEN m.measurement_source_concept_id
                        ELSE m.measurement_concept_id
                    END
                ORDER BY m.measurement_date DESC, m.measurement_id DESC
            ) AS rn
        FROM measurement m
        WHERE m.person_id = %s
          AND (
              m.measurement_concept_id = ANY(%s)
              OR (m.measurement_concept_id = 0
                  AND m.measurement_source_concept_id = ANY(%s))
          )
    )
    SELECT r.*,
           tc.concept_name AS type_concept_name,
           uc.concept_code AS unit_concept_code
    FROM ranked r
    LEFT JOIN concept tc ON tc.concept_id = r.measurement_type_concept_id
    LEFT JOIN concept uc ON uc.concept_id = r.unit_concept_id
    WHERE r.rn <= %s
    ORDER BY r.eff_id, r.measurement_date DESC, r.measurement_id DESC
    """

    @staticmethod
    def _hydrate_page(person_id, page_summaries):
        if not page_summaries:
            return []

        concept_ids = [s['effective_concept_id'] for s in page_summaries]

        with connection.cursor() as cursor:
            cursor.execute(
                ResultsSummaryView._HYDRATE_SQL,
                [person_id, concept_ids, concept_ids, MAX_VALUES_PER_CONCEPT],
            )
            columns = [col[0] for col in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

        groups = defaultdict(list)
        visit_ids = set()
        for row in rows:
            groups[row['eff_id']].append(row)
            if row['visit_occurrence_id']:
                visit_ids.add(row['visit_occurrence_id'])
        provenance = _load_visit_provenance(visit_ids)

        cards = []
        for summary in page_summaries:
            cid = summary['effective_concept_id']
            meas_rows = groups.get(cid, [])

            values = []
            for row in meas_rows:
                unit_str = row['unit_source_value']
                if not unit_str and row['unit_concept_code']:
                    unit_str = row['unit_concept_code']

                type_label = None
                if row['measurement_type_concept_id']:
                    type_label = MEASUREMENT_TYPE_LABELS.get(
                        row['measurement_type_concept_id'],
                    )
                    if type_label is None and row['type_concept_name']:
                        type_label = row['type_concept_name']

                lab_name = None
                report_filename = None
                if row['visit_occurrence_id']:
                    prov = provenance.get(row['visit_occurrence_id'])
                    if prov:
                        lab_name = prov.get('lab_name')
                        report_filename = prov.get('report_filename')

                values.append({
                    'measurement_id': row['measurement_id'],
                    'value': row['value_as_number'],
                    'value_string': row['value_as_string'],
                    'unit': unit_str,
                    'status': _compute_status(row['value_as_number'], row['range_low'], row['range_high']),
                    'measured_at': row['measurement_date'],
                    'range_low': row['range_low'],
                    'range_high': row['range_high'],
                    'source': type_label,
                    'lab_name': lab_name,
                    'report_filename': report_filename,
                })

            cards.append({
                'concept_id': cid,
                'concept_code': summary['concept_code'],
                'concept_name': summary['concept_name'],
                'vocabulary_id': summary['vocabulary_id'],
                'category': summary['category'],
                'values': values,
            })

        return cards


class ValuesView(APIView):
    """
    GET /api/lab-results/values/?concept_code=718-7&page=1&page_size=50[&person_id=X]

    Returns paginated measurement values plus concept metadata (concept_name,
    category, vocabulary_id) so the detail view doesn't need the summary endpoint.

    person_id is optional — when omitted, resolved from the authenticated user's email.
    """
    permission_classes = [ScopedTokenPermission]

    def get(self, request):
        person_id, err = _resolve_person_id(request)
        if err:
            return err

        concept_code = request.query_params.get('concept_code')
        if not concept_code:
            return Response(
                {'detail': 'concept_code query parameter is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org = get_request_org(request)
        if org is not None:
            if not PatientInfo.objects.filter(person_id=person_id, organization=org).exists():
                return Response(
                    {'detail': 'Person not found in your organization.'},
                    status=status.HTTP_404_NOT_FOUND,
                )

        concept = Concept.objects.filter(
            concept_code=concept_code,
            vocabulary_id__in=['LOINC', 'HK-Labs'],
        ).first()
        if not concept:
            return Response(
                {'detail': f'Concept with code {concept_code} not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        category_cache = _build_category_cache()
        if concept.vocabulary_id == 'LOINC':
            category = category_cache.get(concept.concept_code, 'Uncategorized')
        elif concept.vocabulary_id == 'HK-Labs':
            category = 'Uncategorized'
        else:
            category = 'Other'

        measurements = (
            Measurement.objects
            .filter(person_id=person_id)
            .filter(
                models_Q(measurement_concept_id=concept.concept_id) |
                models_Q(measurement_source_concept_id=concept.concept_id)
            )
            .select_related(
                'measurement_type_concept', 'unit_concept', 'visit_occurrence',
            )
            .order_by('-measurement_date', '-measurement_id')
        )

        paginator = LabResultsPagination()
        page = paginator.paginate_queryset(measurements, request)

        provenance = self._load_provenance(page)
        values = []
        for m in page:
            unit_str = m.unit_source_value
            if not unit_str and m.unit_concept:
                unit_str = m.unit_concept.concept_code

            type_label = None
            if m.measurement_type_concept_id:
                type_label = MEASUREMENT_TYPE_LABELS.get(m.measurement_type_concept_id)
                if type_label is None and m.measurement_type_concept:
                    type_label = m.measurement_type_concept.concept_name

            prov = provenance.get(m.visit_occurrence_id, {})
            values.append({
                'measurement_id': m.measurement_id,
                'value': m.value_as_number,
                'value_string': m.value_as_string,
                'unit': unit_str,
                'status': _compute_status(m.value_as_number, m.range_low, m.range_high),
                'measured_at': m.measurement_date,
                'range_low': m.range_low,
                'range_high': m.range_high,
                'source': type_label,
                'lab_name': prov.get('lab_name'),
                'report_filename': prov.get('report_filename'),
            })

        serializer = LabValueSerializer(values, many=True)
        paginated = paginator.get_paginated_response(serializer.data)
        paginated.data['concept_id'] = concept.concept_id
        paginated.data['concept_code'] = concept.concept_code
        paginated.data['concept_name'] = concept.concept_name
        paginated.data['vocabulary_id'] = concept.vocabulary_id
        paginated.data['category'] = category
        return paginated

    def _load_provenance(self, measurements):
        visit_ids = {m.visit_occurrence_id for m in measurements if m.visit_occurrence_id}
        return _load_visit_provenance(visit_ids)


class MeasurementDetailView(APIView):
    """
    GET/PATCH/DELETE /api/lab-results/measurements/<measurement_id>/

    Allows reading, updating, or deleting a single measurement.
    Scoped by person_id for multi-tenant safety.
    """
    permission_classes = [ScopedTokenPermission]

    def get_object(self, measurement_id, request):
        from omop_core.authorization import can_access_patient

        try:
            m = Measurement.objects.select_related(
                'measurement_type_concept', 'unit_concept', 'visit_occurrence',
            ).get(measurement_id=measurement_id)
        except Measurement.DoesNotExist:
            return None

        org = get_request_org(request)
        if org is not None:
            from omop_core.models import PatientInfo
            if not PatientInfo.objects.filter(
                person_id=m.person_id, organization=org
            ).exists():
                return None
        elif request.user and request.user.is_authenticated:
            from patient_portal.models import PatientUser
            own_pid = None
            try:
                own_pid = PatientUser.objects.get(identity=request.user).person_id
            except PatientUser.DoesNotExist:
                pass
            if m.person_id != own_pid and not can_access_patient(request.user, m.person_id):
                return None
        return m

    def get(self, request, measurement_id):
        m = self.get_object(measurement_id, request)
        if not m:
            return Response(
                {'detail': 'Measurement not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        unit_str = m.unit_source_value
        if not unit_str and m.unit_concept:
            unit_str = m.unit_concept.concept_code

        type_label = None
        if m.measurement_type_concept_id:
            type_label = MEASUREMENT_TYPE_LABELS.get(m.measurement_type_concept_id)
            if type_label is None and m.measurement_type_concept:
                type_label = m.measurement_type_concept.concept_name

        data = {
            'measurement_id': m.measurement_id,
            'value': m.value_as_number,
            'value_string': m.value_as_string,
            'unit': unit_str,
            'status': _compute_status(m.value_as_number, m.range_low, m.range_high),
            'measured_at': m.measurement_date,
            'range_low': m.range_low,
            'range_high': m.range_high,
            'source': type_label,
            'lab_name': None,
            'report_filename': None,
        }
        serializer = LabValueSerializer(data)
        return Response(serializer.data)

    def patch(self, request, measurement_id):
        m = self.get_object(measurement_id, request)
        if not m:
            return Response(
                {'detail': 'Measurement not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = MeasurementUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        field_map = {
            'value': 'value_as_number',
            'value_string': 'value_as_string',
            'measured_at': 'measurement_date',
            'range_low': 'range_low',
            'range_high': 'range_high',
        }
        updated = False
        for api_field, model_field in field_map.items():
            if api_field in data:
                setattr(m, model_field, data[api_field] if data[api_field] is not None else None)
                updated = True

        if updated:
            with transaction.atomic():
                m.save()
                source = self._provenance_source(request, m.person_id)
                ProvenanceRecord.objects.create(
                    source=source,
                    source_user_id=f"{getattr(request.user, 'issuer', '')}|{getattr(request.user, 'sub', '')}",
                    target_patient_id=str(m.person_id),
                    modification_reason='measurement_update',
                    content_type=ContentType.objects.get_for_model(Measurement),
                    object_id=measurement_id,
                )

        return Response({'detail': 'Updated.'}, status=status.HTTP_200_OK)

    @staticmethod
    def _provenance_source(request, person_id):
        from patient_portal.models import PatientUser
        try:
            own_pid = PatientUser.objects.get(identity=request.user).person_id
            if own_pid == person_id:
                return 'PATIENT_SELF'
        except (PatientUser.DoesNotExist, AttributeError):
            pass
        return 'ADMIN_CORRECTION'

    def delete(self, request, measurement_id):
        m = self.get_object(measurement_id, request)
        if not m:
            return Response(
                {'detail': 'Measurement not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        person_id = m.person_id
        source = self._provenance_source(request, person_id)
        with transaction.atomic():
            ProvenanceRecord.objects.create(
                source=source,
                source_user_id=f"{getattr(request.user, 'issuer', '')}|{getattr(request.user, 'sub', '')}",
                target_patient_id=str(person_id),
                modification_reason='measurement_delete',
                content_type=ContentType.objects.get_for_model(Measurement),
                object_id=measurement_id,
            )
            m.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class VisitDeleteView(APIView):
    """
    DELETE /api/lab-results/visits/<visit_id>/

    Deletes a VisitOccurrence and all its associated Measurements.
    Used by hk-labs to cascade-delete when an upload is removed.
    """
    permission_classes = [ScopedTokenPermission]

    def delete(self, request, visit_id):
        from omop_core.authorization import can_access_patient

        try:
            visit = VisitOccurrence.objects.get(visit_occurrence_id=visit_id)
        except VisitOccurrence.DoesNotExist:
            return Response(
                {'detail': 'Visit not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        is_service = getattr(request.user, 'issuer', '') == 'urn:service'
        if not is_service:
            org = get_request_org(request)
            if org is not None:
                from omop_core.models import PatientInfo
                if not PatientInfo.objects.filter(
                    person_id=visit.person_id, organization=org
                ).exists():
                    return Response(
                        {'detail': 'Visit not found.'},
                        status=status.HTTP_404_NOT_FOUND,
                    )
            elif request.user and request.user.is_authenticated:
                from patient_portal.models import PatientUser
                own_pid = None
                try:
                    own_pid = PatientUser.objects.get(identity=request.user).person_id
                except PatientUser.DoesNotExist:
                    pass
                if visit.person_id != own_pid and not can_access_patient(request.user, visit.person_id):
                    return Response(
                        {'detail': 'Visit not found.'},
                        status=status.HTTP_404_NOT_FOUND,
                    )

        with transaction.atomic():
            ProvenanceRecord.objects.create(
                source='ADMIN_CORRECTION',
                source_user_id=f"{getattr(request.user, 'issuer', '')}|{getattr(request.user, 'sub', '')}",
                target_patient_id=str(visit.person_id),
                modification_reason='visit_delete',
                content_type=ContentType.objects.get_for_model(VisitOccurrence),
                object_id=visit_id,
            )

            meas_count, _ = Measurement.objects.filter(
                visit_occurrence=visit,
            ).delete()
            visit.delete()

        return Response(
            {'deleted_measurements': meas_count},
            status=status.HTTP_200_OK,
        )
