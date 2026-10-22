"""OMOP assertions for supportive courses, with same-day correction semantics."""
from omop_core.services.omop_projection import project_single_value


def project_supportive_course(course):
    # Stable per-course keys keep two concurrent supportive therapies separate.
    # These course assertions have no standard observation concept. Keep concept
    # 0 stable even if the regimen changes, so same-day corrections reuse rows.
    values = {
        'regimen': course.regimen.code,
        'start': course.start_date,
        'end': course.end_date,
        'intent': course.intent,
        'discontinuation': course.discontinuation_reason,
    }
    for field, value in values.items():
        project_single_value(course.person, 'supportive_' + field, value, {
            'omop_table': 'observation',
            'concept_id': 0,
            'type_concept_id': 32817,
            'source_value': f'supportive:{course.pk}:{field}',
            'value_kind': 'string',
        })


def supportive_course_summary(person):
    courses = list(person.supportive_courses.select_related('regimen').order_by('start_date', 'pk'))
    if not courses:
        return {}
    starts = [c.start_date for c in courses if c.start_date]
    ends = [c.end_date for c in courses if c.end_date]
    return {
        'supportive_therapies': '; '.join(dict.fromkeys(c.regimen.title for c in courses)),
        'supportive_therapy_start_date': min(starts) if starts else None,
        'supportive_therapy_end_date': max(ends) if len(ends) == len(courses) else None,
        'supportive_therapy_intent': courses[-1].intent or None,
    }
