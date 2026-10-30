"""One partial-DOB contract for native and shared PatientRecord profile edits."""
from datetime import date, datetime

from django.utils import timezone


def normalize_birth_date(person, changes):
    """Return the date/precision pair, preserving omitted precision from Person.

    January/day 1 are read-model placeholders for partial dates, not assertions
    that those components are known. An explicit precision change is required
    to promote a partial date to a full date.
    """
    if 'date_of_birth_precision' in changes:
        precision = changes['date_of_birth_precision'] or 'day'
    elif person.year_of_birth and not person.month_of_birth:
        precision = 'year'
    elif person.year_of_birth and not person.day_of_birth:
        precision = 'month'
    else:
        precision = 'day'
    if 'date_of_birth' in changes:
        dob = changes['date_of_birth']
        if isinstance(dob, str):
            dob = date.fromisoformat(dob) if dob else None
    elif person.year_of_birth:
        try:
            dob = date(person.year_of_birth, person.month_of_birth or 1, person.day_of_birth or 1)
        except ValueError:
            dob = date(person.year_of_birth, 1, 1)
    else:
        dob = None
    if dob and precision == 'year':
        dob = dob.replace(month=1, day=1)
    elif dob and precision == 'month':
        dob = dob.replace(day=1)
    return dob, precision


def person_birth_values(dob, precision):
    return {
        'year_of_birth': dob.year if dob else None,
        'month_of_birth': dob.month if dob and precision != 'year' else None,
        'day_of_birth': dob.day if dob and precision == 'day' else None,
        'birth_datetime': timezone.make_aware(datetime.combine(dob, datetime.min.time()))
        if dob and precision == 'day' else None,
    }
