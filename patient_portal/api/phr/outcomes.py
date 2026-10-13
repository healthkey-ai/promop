"""My outcomes: the patient's outcome surveys and where they stand.

The surveys are PROlog instruments (``prolog_surveys``), named by
``settings.PHR_OUTCOME_SURVEYS``. Each is due a year after the patient last
completed it; one never completed is due now. PROlog's own repeat schedule
(RUN-5) is for invited participants on a fixed calendar, so it can't say
this. Answering goes through PROlog's run API (``/api/v1/prolog/run/``),
which also serves a completed response's answers; this is only the index.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from django.conf import settings
from django.utils import timezone


def _a_year_after(day: date) -> date:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:  # 29 February
        return day.replace(year=day.year + 1, day=28)


def _active_versions():
    from prolog_surveys.models import LifecycleStatus, SurveyVersion

    slugs = list(getattr(settings, 'PHR_OUTCOME_SURVEYS', []))
    versions = {
        v.survey.slug: v
        for v in SurveyVersion.objects.filter(status=LifecycleStatus.ACTIVE, survey__slug__in=slugs)
        .select_related('survey')
    }
    return [versions[slug] for slug in slugs if slug in versions]


def has_outcomes() -> bool:
    return bool(_active_versions())


def outcomes(person, record=None) -> dict[str, Any]:
    from prolog_surveys.engine.localize import pick
    from prolog_surveys.models import ResponseStatus, SurveyResponse

    today = timezone.localdate()
    surveys = []
    for version in _active_versions():
        definition = version.cached_definition or {}
        language = definition.get('default_language', 'en')
        mine = SurveyResponse.objects.filter(participant_id=person.person_id, survey_version__survey=version.survey)
        done = list(mine.filter(status=ResponseStatus.SUBMITTED, submitted_at__isnull=False).order_by('-submitted_at'))
        # PROlog resumes only a response on the active version (its POST /responses/).
        draft = (
            mine.filter(status=ResponseStatus.IN_PROGRESS, survey_version=version).order_by('-started_at').first()
        )
        last = timezone.localdate(done[0].submitted_at) if done else None
        next_due = _a_year_after(last) if last else None
        if next_due is None:
            status = 'due_now'
        elif next_due <= today:
            status = 'overdue'
        else:
            status = 'upcoming'
        surveys.append({
            'slug': version.survey.slug,
            'title': pick(definition.get('title') or {}, language, language),
            'estimated_minutes': definition.get('estimated_minutes'),
            'status': status,
            'last_completed': last.isoformat() if last else None,
            'next_due': next_due.isoformat() if next_due else None,
            'completed': len(done),
            'latest_response': str(done[0].id) if done else None,
            'draft_response': str(draft.id) if draft else None,
        })
    return {'surveys': surveys}
