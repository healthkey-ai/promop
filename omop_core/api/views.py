"""The authoring endpoint an embedding host serves.

The clinical work already lives in ``author_therapy_line``; this is the HTTP
shell. Host policy is a seam, not a copy: a host subclasses the viewset to add its
permission classes, its per-person write check, and its own ``PatientRecord``
payload shape.
"""
import logging

from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from omop_core.api.serializers import TherapyLineWriteSerializer
from omop_core.models import PatientRecord
from omop_core.signals import suppress_patient_record_refresh
from omop_oncology.models import Episode

logger = logging.getLogger(__name__)


GENERIC_WRITE_REFUSAL = 'The therapy line could not be saved as submitted.'
GENERIC_RETRACT_REFUSAL = 'The therapy line could not be retracted.'


def _refusal(exc, data, generic):
    """The 400 body for a refused write or retract.

    A :class:`TherapyLineValidationError` carries a catalog code, and the client
    gets the catalog's message and field for it: the refusals are written for the
    person editing the line, and "save failed" would throw that away. Any other
    ``ValueError`` is internal, so its text is withheld (CodeQL
    ``py/stack-trace-exposure``) and only its type is logged. Nothing taken from
    the exception is serialized either way; see ``refusal_payload``.
    """
    from omop_core.services.therapy_errors import TherapyLineValidationError, refusal_payload

    payload = refusal_payload(exc) if isinstance(exc, TherapyLineValidationError) else None
    if payload is None:
        logger.warning(
            'Therapy line request rejected (%s); details withheld from the client',
            type(exc).__name__,
        )
        return {'detail': generic}
    if payload.get('field') == 'drugs':
        # The client's own submission, echoed so it can point at the drug.
        payload['concept_ids'] = [
            drug.get('concept_id') for drug in (data or {}).get('drugs') or []
            if isinstance(drug, dict)
        ]
    return payload


class TherapyLineViewSet(viewsets.ViewSet):
    """Author or edit one line of therapy.

    ``POST /v1/therapy-lines/`` and ``PATCH /v1/therapy-lines/<episode_id>/``.

    The caller sends what a clinician knows -- which line, which drugs, which
    dates, how it went -- and the service turns it into the CDM shape through the
    same ``upsert_therapy_line_episode`` every other path uses, so the tagging
    cannot drift.
    """

    permission_classes = [IsAuthenticated]

    # --- host seams -------------------------------------------------------

    def patient_write_denied(self, request, person):
        """Return a Response to refuse this write, or None to allow it.

        The default allows any authenticated caller, which is only appropriate
        when the host has already scoped the request to the caller's own record.
        A host that serves more than one patient must override this.
        """
        return None

    def patient_info_payload(self, record):
        """The re-derived record in the shape this host's client expects."""
        return None

    # --- shared implementation -------------------------------------------

    def _write(self, request, *, person, line_number, data, episode=None):
        from omop_core.services.episode_service import author_therapy_line
        from omop_core.services.therapy_errors import CONFLICT_MESSAGE
        from omop_core.services.therapy_transactions import TherapyLineWriteConflict

        denied = self.patient_write_denied(request, person)
        if denied is not None:
            return denied

        submitted = {key: data[key] for key in (
            'drugs', 'start_date', 'end_date', 'regimen_concept_id',
            'outcome', 'intent', 'discontinuation_reason', 'source_value',
        ) if key in data}
        try:
            with transaction.atomic():
                # Each exposure save would re-derive the whole record; derive
                # once, after the episode exists, so inference never reads a
                # half-written line.
                with suppress_patient_record_refresh():
                    result = author_therapy_line(
                        person,
                        line_number=line_number,
                        disease=data.get('disease') or None,
                        episode=episode,
                        replace=episode is not None,
                        **submitted,
                    )
        except TherapyLineWriteConflict:
            # The message is a constant; the exception is never serialized.
            logger.warning('Therapy line write lost the retry race; the client was told to retry')
            return Response({'detail': CONFLICT_MESSAGE}, status=status.HTTP_409_CONFLICT)
        except ValueError as exc:
            return Response(_refusal(exc, data, GENERIC_WRITE_REFUSAL),
                            status=status.HTTP_400_BAD_REQUEST)

        if result.episode is None:
            return Response(
                {'detail': (
                    'Treatment Regimen concept (32531) is not loaded, so the '
                    'therapy line could not be grouped. Load the Episode '
                    'vocabulary and retry.'
                )},
                status=status.HTTP_409_CONFLICT,
            )

        person.refresh_from_db()
        from omop_core.services.patient_record_service import refresh_patient_record
        record = refresh_patient_record(person)
        return Response(
            {
                'episode_id': result.episode.episode_id,
                'line_number': line_number,
                'created': result.created,
                'drug_exposure_ids': result.drug_exposure_ids,
                'drugs_created': result.drugs_created,
                'patient_info': self.patient_info_payload(record),
            },
            status=status.HTTP_201_CREATED if episode is None else status.HTTP_200_OK,
        )

    def create(self, request):
        serializer = TherapyLineWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._write(
            request,
            person=serializer.validated_data['person'],
            line_number=serializer.validated_data['line_number'],
            data=serializer.validated_data,
        )

    def destroy(self, request, pk=None):
        """Retract the line: withdraw the claim that this treatment happened.

        Distinct from clearing composition, which keeps the line and drops its
        links. Exposures are never deleted here -- they can be imported facts.
        """
        from omop_core.services.episode_service import retract_therapy_line
        from omop_core.services.patient_record_service import refresh_patient_record

        try:
            episode_id = int(pk)
        except (TypeError, ValueError):
            return Response({'detail': 'Invalid therapy line episode_id.'},
                            status=status.HTTP_404_NOT_FOUND)
        episode = Episode.objects.filter(episode_id=episode_id).select_related('person').first()
        if episode is None:
            return Response({'detail': 'Therapy line not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        denied = self.patient_write_denied(request, episode.person)
        if denied is not None:
            return denied
        try:
            retract_therapy_line(episode.person, episode)
        except ValueError as exc:
            return Response(_refusal(exc, None, GENERIC_RETRACT_REFUSAL),
                            status=status.HTTP_400_BAD_REQUEST)

        record = refresh_patient_record(episode.person)
        return Response({
            'episode_id': episode.episode_id,
            'retracted': True,
            'patient_info': self.patient_info_payload(record),
        })

    def partial_update(self, request, pk=None):
        try:
            episode_id = int(pk)
        except (TypeError, ValueError):
            return Response({'detail': 'Invalid therapy line episode_id.'},
                            status=status.HTTP_404_NOT_FOUND)

        episode = Episode.objects.filter(episode_id=episode_id).select_related('person').first()
        if episode is None:
            return Response({'detail': 'Therapy line not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        if episode.episode_number is None:
            return Response({'detail': 'Therapy line has no episode_number to edit.'},
                            status=status.HTTP_400_BAD_REQUEST)

        requested_line = request.data.get('line_number')
        if requested_line is not None:
            try:
                requested_line = int(requested_line)
            except (TypeError, ValueError):
                return Response({'detail': 'line_number must be an integer.'},
                                status=status.HTTP_400_BAD_REQUEST)
            if requested_line != episode.episode_number:
                return Response({'detail': 'Editing a therapy line cannot change its line_number.'},
                                status=status.HTTP_400_BAD_REQUEST)

        payload = dict(request.data)
        payload['person'] = episode.person_id
        payload['line_number'] = episode.episode_number
        serializer = TherapyLineWriteSerializer(data=payload, partial=True)
        serializer.is_valid(raise_exception=True)
        return self._write(
            request,
            person=episode.person,
            line_number=episode.episode_number,
            data=serializer.validated_data,
            episode=episode,
        )


class TherapyHistoryCompletenessViewSet(viewsets.ViewSet):
    """Confirm, or withdraw the confirmation, that the recorded lines are complete.

    ``POST /v1/therapy-history/completeness/`` ``{"person": <id>, "complete": true|false}``.

    A confirmation is what turns ``therapy_lines_count`` from a lower bound into an
    exact count, so it is a deliberate statement rather than a side effect of
    saving a line (#1924). It is a write about one patient, so it is gated exactly
    like authoring: the same permission classes and the same per-person check.
    ``urlpatterns_for`` builds this class from the host's therapy-line viewset
    (:meth:`for_host`), so a host that overrides authoring is covered here too.
    """

    permission_classes = [IsAuthenticated]

    #: The seams a host's therapy-line viewset may carry; copied by for_host.
    HOST_SEAMS = ('permission_classes', 'patient_write_denied', 'patient_info_payload',
                  'attestation_source')

    @classmethod
    def for_host(cls, therapy_line_viewset):
        """This endpoint under the host's therapy-line policy.

        Copies the authorization seams off the host's viewset, so the default
        is never more permissive than authoring. A host whose seams depend on
        other attributes of its own class passes its own ``completeness_viewset``
        to ``urlpatterns_for`` instead.
        """
        if therapy_line_viewset is None or therapy_line_viewset is TherapyLineViewSet:
            return cls
        seams = {}
        for name in cls.HOST_SEAMS:
            value = therapy_line_viewset.__dict__.get(name)
            if value is None:
                for base in therapy_line_viewset.__mro__[1:]:
                    if base is TherapyLineViewSet or base is viewsets.ViewSet:
                        break
                    if name in base.__dict__:
                        value = base.__dict__[name]
                        break
            if value is not None:
                seams[name] = value
        return type(f'{therapy_line_viewset.__name__}HistoryCompleteness', (cls,), seams)

    # --- host seams -------------------------------------------------------

    def patient_write_denied(self, request, person):
        """Return a Response to refuse this write, or None to allow it."""
        return None

    def patient_info_payload(self, record):
        """The re-derived record in the shape this host's client expects."""
        return None

    def attestation_source(self, request):
        """Who is making the statement: ``patient``, ``clinician`` or ``host``.

        Decided here, never taken from the body: a caller must not be able to
        label their own confirmation as somebody else's. Provenance only.
        """
        return 'patient'

    # --- shared implementation -------------------------------------------

    def create(self, request):
        from omop_core.models import Person
        from omop_core.services.therapy_history import (
            confirm_therapy_history, withdraw_therapy_history,
        )

        person_id = request.data.get('person')
        try:
            person = Person.objects.get(pk=int(person_id))
        except (TypeError, ValueError, Person.DoesNotExist):
            return Response({'detail': 'A valid person is required.'},
                            status=status.HTTP_400_BAD_REQUEST)

        denied = self.patient_write_denied(request, person)
        if denied is not None:
            return denied

        complete = _as_bool(request.data.get('complete'))
        if complete is None:
            return Response({'detail': 'complete must be true or false.'},
                            status=status.HTTP_400_BAD_REQUEST)

        # The services derive the record once; nothing derives it again here.
        if complete:
            record = confirm_therapy_history(person, source=self.attestation_source(request))
        else:
            record = withdraw_therapy_history(person)
        return Response({
            'therapy_history_complete': record.therapy_history_complete,
            'patient_info': self.patient_info_payload(record),
        })


def _as_bool(value):
    """A JSON boolean, or its form-encoded spellings; None when it is neither."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {'true', '1', 'yes'}:
            return True
        if lowered in {'false', '0', 'no'}:
            return False
    return None


def therapy_outcomes(request):
    """The disease's response-assessment catalog for the editor."""
    from rest_framework.response import Response as _Response
    from omop_core.services.treatment_catalog import outcomes_for_disease
    return _Response(outcomes_for_disease(request.query_params.get('disease', '')))


def therapy_regimen_list(request):
    from rest_framework.response import Response as _Response
    from omop_core.api.regimens import regimen_list_payload
    return _Response(regimen_list_payload(request.query_params))


def therapy_regimen_detail(request, code):
    from rest_framework.response import Response as _Response
    from omop_core.api.regimens import regimen_detail_payload
    payload = regimen_detail_payload(code)
    if payload is None:
        return _Response({'error': f'Regimen not found: {code}'},
                         status=status.HTTP_404_NOT_FOUND)
    return _Response(payload)


def concept_search(request):
    """Concept search for the editor: three-character minimum, ranked, paginated."""
    from rest_framework.response import Response as _Response
    from omop_core.api.concepts import (
        ConceptPagination, search_queryset, serialize_concept_base,
    )

    query = (request.query_params.get('q') or '').strip()
    if len(query) < 3:
        return _Response(
            {'detail': "Query parameter 'q' is required and must be at least 3 characters."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    queryset, _ = search_queryset(query, request.query_params)
    paginator = ConceptPagination()
    page = paginator.paginate_queryset(queryset, request)
    return paginator.get_paginated_response([serialize_concept_base(c) for c in page])
