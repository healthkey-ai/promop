"""Fail-closed adapter for validated OHDSI ARTEMIS line-of-therapy output.

ARTEMIS itself is an external R workflow.  This module intentionally does not
shell out to R or attempt to reimplement its algorithm: it accepts its compact,
versioned result document only after validating every reference against the
local OMOP CDM, then uses the one canonical Episode writer used by every other
therapy path.

Input contract (JSON)::

    {
      "schema_version": "1",
      "episodes": [{
        "person_id": 123,
        "line_number": 1,
        "start_date": "2024-01-01",
        "end_date": "2024-03-01",
        "drug_exposure_ids": [9001, 9002],
        "regimen_concept_id": 12345,
        "outcome": "Partial Response"
      }]
    }

The adapter fails before writing anything if the document is malformed, refers
to an unknown patient/concept/exposure, or attempts to link another patient's
exposure.  This makes the artifact boundary auditable and safe to run in CI
without installing R or ARTEMIS.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

from omop_core.models import Concept, DrugExposure, Person
from omop_core.services.episode_service import upsert_therapy_line_episode
from omop_oncology.models import Episode


ARTEMIS_SCHEMA_VERSION = "1"
_ROOT_KEYS = {"schema_version", "episodes"}
_EPISODE_KEYS = {
    "person_id", "line_number", "start_date", "end_date",
    "drug_exposure_ids", "regimen_concept_id", "outcome",
}


@dataclass(frozen=True)
class ArtemisEpisode:
    person: Person
    line_number: int
    start_date: date
    end_date: date | None
    drug_exposure_ids: tuple[int, ...]
    regimen_concept: Concept | None
    outcome: str | None


@dataclass(frozen=True)
class ArtemisMaterializationResult:
    created: int
    updated: int
    skipped_manual: int


def _error(message: str) -> ValidationError:
    return ValidationError(f"Invalid ARTEMIS output: {message}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_date(value: Any, field: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _error(f"{field} must be an ISO-8601 date or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise _error(f"{field} must be an ISO-8601 date") from exc


def validate_artemis_output(payload: Any) -> list[ArtemisEpisode]:
    """Validate an ARTEMIS result document entirely before materializing it."""
    if not isinstance(payload, dict):
        raise _error("root must be an object")
    if set(payload) != _ROOT_KEYS:
        raise _error("root must contain only schema_version and episodes")
    if payload["schema_version"] != ARTEMIS_SCHEMA_VERSION:
        raise _error(f"schema_version must be {ARTEMIS_SCHEMA_VERSION!r}")
    rows = payload["episodes"]
    if not isinstance(rows, list):
        raise _error("episodes must be a list")

    validated: list[ArtemisEpisode] = []
    seen_lines: set[tuple[int, int]] = set()
    for index, row in enumerate(rows):
        label = f"episodes[{index}]"
        if not isinstance(row, dict) or set(row) - _EPISODE_KEYS:
            raise _error(f"{label} contains unsupported fields")
        required = {"person_id", "line_number", "start_date", "drug_exposure_ids"}
        missing = required - set(row)
        if missing:
            raise _error(f"{label} is missing {', '.join(sorted(missing))}")
        if not _is_int(row["person_id"]) or row["person_id"] <= 0:
            raise _error(f"{label}.person_id must be a positive integer")
        if not _is_int(row["line_number"]) or row["line_number"] <= 0:
            raise _error(f"{label}.line_number must be a positive integer")
        key = (row["person_id"], row["line_number"])
        if key in seen_lines:
            raise _error(f"{label} duplicates person_id {key[0]} line_number {key[1]}")
        seen_lines.add(key)

        start_date = _parse_date(row["start_date"], f"{label}.start_date")
        if start_date is None:
            raise _error(f"{label}.start_date is required")
        end_date = _parse_date(row.get("end_date"), f"{label}.end_date")
        if end_date and end_date < start_date:
            raise _error(f"{label}.end_date cannot precede start_date")

        exposure_ids = row["drug_exposure_ids"]
        if not isinstance(exposure_ids, list) or not exposure_ids:
            raise _error(f"{label}.drug_exposure_ids must be a non-empty list")
        if not all(_is_int(value) and value > 0 for value in exposure_ids):
            raise _error(f"{label}.drug_exposure_ids must contain positive integers")
        if len(set(exposure_ids)) != len(exposure_ids):
            raise _error(f"{label}.drug_exposure_ids cannot contain duplicates")

        person = Person.objects.filter(person_id=row["person_id"]).first()
        if person is None:
            raise _error(f"{label}.person_id {row['person_id']} does not exist")
        exposures = list(DrugExposure.objects.filter(drug_exposure_id__in=exposure_ids))
        if len(exposures) != len(exposure_ids):
            raise _error(f"{label} references an unknown drug_exposure_id")
        if any(exposure.person_id != person.person_id for exposure in exposures):
            raise _error(f"{label} references a DrugExposure belonging to another person")

        regimen_concept = None
        regimen_concept_id = row.get("regimen_concept_id")
        if regimen_concept_id is not None:
            if not _is_int(regimen_concept_id) or regimen_concept_id <= 0:
                raise _error(f"{label}.regimen_concept_id must be a positive integer or null")
            regimen_concept = Concept.objects.filter(concept_id=regimen_concept_id).first()
            if regimen_concept is None:
                raise _error(f"{label}.regimen_concept_id {regimen_concept_id} does not exist")

        outcome = row.get("outcome")
        if outcome is not None and (not isinstance(outcome, str) or not outcome.strip() or len(outcome) > 60):
            raise _error(f"{label}.outcome must be a non-empty string up to 60 characters or null")
        validated.append(ArtemisEpisode(
            person=person, line_number=row["line_number"], start_date=start_date,
            end_date=end_date, drug_exposure_ids=tuple(exposure_ids),
            regimen_concept=regimen_concept, outcome=outcome,
        ))
    return validated


def _is_manual_episode(episode: Episode) -> bool:
    """Manual is the established explicit provenance marker for authored LOTs."""
    return (episode.episode_source_value or "").strip().lower() == "manual"


def materialize_artemis_output(payload: Any) -> ArtemisMaterializationResult:
    """Validate and atomically materialize ARTEMIS output through episode_service."""
    episodes = validate_artemis_output(payload)
    created = updated = skipped_manual = 0
    with transaction.atomic():
        for item in episodes:
            existing = Episode.objects.filter(
                person=item.person, episode_number=item.line_number,
            ).first()
            if existing is not None and _is_manual_episode(existing):
                skipped_manual += 1
                continue
            result = upsert_therapy_line_episode(
                item.person,
                line_number=item.line_number,
                regimen_concept=item.regimen_concept,
                regimen_source_concept=item.regimen_concept,
                start_date=item.start_date,
                end_date=item.end_date,
                drug_exposure_ids=item.drug_exposure_ids,
                outcome=item.outcome,
                source_value=f"ARTEMIS-LOT-{item.line_number}",
                today=item.start_date,
                replace_events=True,
            )
            if result.episode is None:
                # The canonical writer has a mandatory CDM concept dependency.
                # Rolling back makes that absence fail closed rather than producing
                # an incomplete partial ARTEMIS import.
                raise _error("required Treatment Regimen concept is not loaded")
            if result.created:
                created += 1
            else:
                updated += 1
    return ArtemisMaterializationResult(created, updated, skipped_manual)
