"""Import one person's lines of therapy from an ETL feed in one request.

    POST /api/v1/therapy-lines/bulk_upsert/
    {"person": 16813,
     "lines": [{"line_number": 1, "start_date": "2010-08-11",
                "end_date": "2012-11-15", "regimen": "DVT-PACE",
                "outcome": "stringent complete response"},
               {"line_number": 3, "start_date": "2021-07-15", "ongoing": true,
                "regimen": "Dara-CyPd"}]}

Each line upserts on (person, cancer, line_number) through the editor's writer.
Omitted fields stay as stored. ``end_date: null``, ``ongoing: true`` and
``outcome: null`` clear. Lines not named and existing drug links stay untouched.
The batch is all-or-nothing and derives the PatientRecord once.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from django.contrib.contenttypes.models import ContentType
from rest_framework import serializers, status
from rest_framework.response import Response

from omop_core.api.serializers import outcome_aliases
from omop_core.mapping.therapy import (
    is_assertable_regimen, normalize_regimen_name, resolve_regimen_label,
)
from omop_core.models import Concept, PatientRecord, Person, ProvenanceRecord
from omop_core.services.disease_episodes import disease_key, disease_slug, primary_disease_slug
from omop_core.services.episode_service import TherapyLineEpisodeResult, author_therapy_lines
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.therapy_transactions import (
    CONCURRENT_EDIT_MESSAGE, TherapyLineRejected, TherapyLineWriteConflict,
    lock_therapy_patients, run_therapy_write,
)
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.api.provenance_org import ProvenanceContext, raise_on_foreign_attribution

MAX_LINES = 100
SOURCE_SYSTEM = 'therapy-lines-api'
REGIMEN_MATCH_CONCEPT_ID = 'concept_id'

ImportLine = dict[str, Any]
ResolvedRegimens = dict[str, tuple[Concept | None, str | None]]


class PositionalTherapyLineListSerializer(serializers.ListSerializer):
    """Keep bounded per-line errors positional without changing DRF defaults."""

    def to_internal_value(self, data):
        try:
            return super().to_internal_value(data)
        except serializers.ValidationError as exc:
            errors = exc.detail
            if (isinstance(data, list) and len(data) <= MAX_LINES
                    and isinstance(errors, dict) and errors
                    and all(type(index) is int and 0 <= index < len(data) for index in errors)):
                raise serializers.ValidationError([
                    errors.get(index, {}) for index in range(len(data))
                ]) from exc
            raise


class TherapyLineImportLineSerializer(serializers.Serializer):
    """One line as the upstream source records it."""

    line_number = serializers.IntegerField(min_value=1)
    start_date = serializers.DateField()
    end_date = serializers.DateField(required=False, allow_null=True)
    ongoing = serializers.BooleanField(required=False)
    regimen = serializers.CharField(required=False, max_length=50)
    regimen_concept_id = serializers.IntegerField(required=False, min_value=1)
    outcome = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=60)
    disease = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=100)

    class Meta:
        list_serializer_class = PositionalTherapyLineListSerializer

    def validate_start_date(self, value: date) -> date:
        """Reject a line that has not started yet."""
        if value > date.today():
            raise serializers.ValidationError('A future therapy line is planned, not received treatment.')
        return value

    def validate(self, attrs: ImportLine) -> ImportLine:
        """Check the end date against ongoing and the start date."""
        end = attrs.get('end_date')
        if attrs.get('ongoing') and end is not None:
            raise serializers.ValidationError({'end_date': 'An ongoing line has no end_date.'})
        if end is not None and end < attrs['start_date']:
            raise serializers.ValidationError({'end_date': 'end_date cannot precede start_date.'})
        return attrs


class TherapyLineImportSerializer(serializers.Serializer):
    """One person and the lines to upsert for them."""

    person = serializers.PrimaryKeyRelatedField(queryset=Person.objects.all())
    lines = TherapyLineImportLineSerializer(many=True, allow_empty=False, max_length=MAX_LINES)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Reject duplicate lines, non-regimen concepts and unknown outcomes, naming each line."""
        person: Person = attrs['person']
        lines: list[ImportLine] = attrs['lines']
        record = PatientRecord.objects.filter(person=person).first()
        primary = record.disease if record else ''
        primary_key = disease_key(primary_disease_slug(person))
        concepts = Concept.objects.in_bulk(
            {line['regimen_concept_id'] for line in lines if line.get('regimen_concept_id')}
        )
        errors: list[dict[str, str]] = []
        seen: set[tuple[str, int]] = set()
        for line in lines:
            error: dict[str, str] = {}
            cancer = disease_key(disease_slug(line.get('disease')))
            # The writer files a line naming the primary cancer with the unnamed ones
            key = ('' if cancer == primary_key else cancer, line['line_number'])
            if key in seen:
                error['line_number'] = 'This line appears more than once in the request.'
            seen.add(key)
            concept_id = line.get('regimen_concept_id')
            if concept_id and not is_assertable_regimen(concepts.get(concept_id)):
                error['regimen_concept_id'] = f'{concept_id} is not a valid HemOnc or HK-Regimen regimen concept.'
            if line.get('outcome'):
                aliases = {
                    alias.casefold(): value
                    for alias, value in outcome_aliases(line.get('disease') or primary).items()
                }
                value = aliases.get(line['outcome'].strip().casefold())
                if value is None:
                    error['outcome'] = (
                        f"Unknown outcome {line['outcome']!r}; expected one of {sorted(set(aliases.values()))}."
                    )
                else:
                    line['outcome'] = value
            errors.append(error)
        if any(errors):
            raise serializers.ValidationError({'lines': errors})
        return attrs


class TherapyLineImportPersonSerializer(serializers.Serializer):
    """Resolve the target before validating fields that reveal patient data."""

    person = serializers.PrimaryKeyRelatedField(queryset=Person.objects.all())


def _author_kwargs(line: ImportLine, regimens: ResolvedRegimens) -> dict[str, Any]:
    """Translate a source line into author_therapy_lines kwargs."""
    # replace=True lets outcome: null clear. Drugs are never passed so their links survive
    kwargs: dict[str, Any] = {'line_number': line['line_number'], 'start_date': line['start_date'], 'replace': True}
    if line.get('ongoing'):
        kwargs['end_date'] = None
    elif 'end_date' in line:
        kwargs['end_date'] = line['end_date']
    if 'outcome' in line:
        kwargs['outcome'] = line['outcome'] or None
    if line.get('disease'):
        kwargs['disease'] = line['disease']
    if line.get('regimen'):
        kwargs['source_value'] = line['regimen']
    if line.get('regimen_concept_id'):
        kwargs['regimen_concept_id'] = line['regimen_concept_id']
    elif line.get('regimen'):
        kwargs['regimen_concept_id'] = regimens[line['regimen']][0].concept_id
    return kwargs


def _regimen_match(line: ImportLine, regimens: ResolvedRegimens) -> str | None:
    """How the line's regimen concept was chosen."""
    if line.get('regimen_concept_id'):
        return REGIMEN_MATCH_CONCEPT_ID
    if line.get('regimen'):
        return regimens[line['regimen']][1]
    return None


def _record_created_provenance(results: list[TherapyLineEpisodeResult], person: Person,
                               provenance: ProvenanceContext) -> None:
    """Attach provenance to newly created lines and their newly written facts."""
    episodes = [result.episode for result in results if result.created]
    observations = [obs for result in results for obs in result.created_observations]
    if not episodes and not observations:
        return
    records = []
    for model_rows in (episodes, observations):
        if not model_rows:
            continue
        content_type = ContentType.objects.get_for_model(model_rows[0])
        records.extend(
            ProvenanceRecord(
                source=provenance.source,
                source_user_id=provenance.source_user_id,
                target_patient_id=str(person.person_id),
                modification_reason=provenance.reason,
                organization=provenance.organization,
                content_type=content_type,
                object_id=row.pk,
            )
            for row in model_rows
        )
    ProvenanceRecord.objects.bulk_create(records)


def _rejected_response(exc: TherapyLineRejected, line_count: int) -> Response:
    """Report a refused line at its position in the request."""
    errors: list[dict[str, list[str]]] = [{} for _ in range(line_count)]
    errors[exc.index] = {'non_field_errors': [exc.message]}
    return Response({'lines': errors}, status=status.HTTP_400_BAD_REQUEST)


def import_therapy_lines(person: Person, lines: list[ImportLine], provenance: ProvenanceContext,
                          skip_refresh: bool, authorize_person=None) -> Response:
    """Upsert validated lines of one person and derive the record once."""

    def once():
        with suppress_patient_record_refresh():
            # Acquire patient roots before shared mapping rows, matching the
            # canonical writer's lock order on every complete retry attempt.
            lock_therapy_patients([person.pk])
            # This instance was resolved before waiting for the Person lock.
            # Refresh under the lock so retries and projection use current data.
            person.refresh_from_db()
            if authorize_person is not None:
                # Tenant ownership may have changed since the pre-validation
                # check. Re-authorize against the locked, current patient record
                # on every complete retry attempt before any writes.
                authorize_person(person)
            labels = sorted(
                {line['regimen'] for line in lines
                 if line.get('regimen') and not line.get('regimen_concept_id')},
                key=lambda label: (normalize_regimen_name(label)[:255], label),
            )
            regimens: ResolvedRegimens = {label: resolve_regimen_label(label, SOURCE_SYSTEM) for label in labels}
            results = author_therapy_lines(
                person, [_author_kwargs(line, regimens) for line in lines], asserted_by='import',
                write_guard=lambda model, ids: raise_on_foreign_attribution(
                    model, ids, provenance.organization,
                ),
                retry=False,
            )
            if results is None:
                # The writer marks this complete attempt for rollback, including
                # any regimens quarantined during preparation.
                return Response(
                    {'detail': (
                        'Treatment Regimen concept (32531) is not loaded, so the '
                        'therapy lines could not be grouped. Load the Episode '
                        'vocabulary and retry.'
                    )},
                    status=status.HTTP_409_CONFLICT,
                )
            if provenance.source:
                _record_created_provenance(results, person, provenance)
        if not skip_refresh:
            refresh_patient_record(person)
        return results, regimens

    try:
        result = run_therapy_write(once)
    except TherapyLineWriteConflict:
        return Response({'detail': CONCURRENT_EDIT_MESSAGE}, status=status.HTTP_409_CONFLICT)
    except TherapyLineRejected as exc:
        return _rejected_response(exc, len(lines))

    if isinstance(result, Response):
        return result
    results, regimens = result
    created = sum(1 for result in results if result.created)
    return Response({
        'person_id': person.person_id,
        'created': created,
        'updated': len(results) - created,
        'lines': [
            {
                'line_number': line['line_number'],
                'episode_id': result.episode.episode_id,
                'created': result.created,
                'regimen_concept_id': result.episode.episode_source_concept_id,
                'regimen_match': _regimen_match(line, regimens),
            }
            for line, result in zip(lines, results)
        ],
    }, status=status.HTTP_200_OK)
