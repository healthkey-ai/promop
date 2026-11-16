"""Who may sign up, and the age they must declare.

The sign-up form offers the countries a deployment enrols from, and each
country has a **minimum age** (the age of majority: 18 in most of the pilot,
19 in several Canadian provinces and some US states). Both are configuration,
not code: ``PHR_SIGNUP_COUNTRIES`` lists the ISO 3166-1 alpha-2 codes offered
and ``PHR_SIGNUP_MINIMUM_AGES`` overrides the default of 18 per country. The
defaults below are the FLF pilot list: the UK, the EU and EEA, the US, Canada,
Australia and Switzerland.

A year of birth cannot prove that a member is of age (someone born 18 years
ago may still be 17), so the check at sign-up is the member's **declaration**,
recorded through consent v2. The year is support only: a year that cannot
reach the minimum age is refused, every other year is accepted with the
declaration.
"""
from __future__ import annotations

from datetime import date

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError

DEFAULT_MINIMUM_AGE = 18
# The oldest year of birth accepted: nobody signing up was born before this
# many years ago.
MAX_AGE = 120

# ISO 3166-1 alpha-2 code -> English short name. The pilot list.
COUNTRY_NAMES = {
    'GB': 'United Kingdom',
    'US': 'United States',
    'CA': 'Canada',
    'AU': 'Australia',
    'CH': 'Switzerland',
    # EU
    'AT': 'Austria', 'BE': 'Belgium', 'BG': 'Bulgaria', 'HR': 'Croatia', 'CY': 'Cyprus',
    'CZ': 'Czechia', 'DK': 'Denmark', 'EE': 'Estonia', 'FI': 'Finland', 'FR': 'France',
    'DE': 'Germany', 'GR': 'Greece', 'HU': 'Hungary', 'IE': 'Ireland', 'IT': 'Italy',
    'LV': 'Latvia', 'LT': 'Lithuania', 'LU': 'Luxembourg', 'MT': 'Malta', 'NL': 'Netherlands',
    'PL': 'Poland', 'PT': 'Portugal', 'RO': 'Romania', 'SK': 'Slovakia', 'SI': 'Slovenia',
    'ES': 'Spain', 'SE': 'Sweden',
    # EEA beyond the EU
    'IS': 'Iceland', 'LI': 'Liechtenstein', 'NO': 'Norway',
}

DEFAULT_COUNTRIES = tuple(COUNTRY_NAMES)


def _configured_codes() -> list[str]:
    codes = getattr(settings, 'PHR_SIGNUP_COUNTRIES', None) or DEFAULT_COUNTRIES
    return [str(c).strip().upper() for c in codes if str(c).strip()]


def _configured_ages() -> dict[str, int]:
    raw = getattr(settings, 'PHR_SIGNUP_MINIMUM_AGES', None) or {}
    if not isinstance(raw, dict):
        raise ImproperlyConfigured('PHR_SIGNUP_MINIMUM_AGES must be a JSON object of country code to age.')
    ages = {}
    for code, age in raw.items():
        if isinstance(age, bool) or not isinstance(age, int) or not 0 < age < MAX_AGE:
            raise ImproperlyConfigured(f'PHR_SIGNUP_MINIMUM_AGES[{code!r}] must be an age in years, got {age!r}.')
        ages[normalize_country(code)] = age
    return ages


def minimum_age(code: str) -> int:
    """The age a member signing up from ``code`` must declare they have reached."""
    return _configured_ages().get(normalize_country(code), DEFAULT_MINIMUM_AGE)


def countries() -> list[dict]:
    """The countries offered at sign-up, in the configured order, each with its
    name and minimum age. A configured code with no name here is offered under
    its code."""
    ages = _configured_ages()
    return [
        {'code': code, 'name': COUNTRY_NAMES.get(code, code), 'minimum_age': ages.get(code, DEFAULT_MINIMUM_AGE)}
        for code in _configured_codes()
    ]


def normalize_country(code) -> str:
    return str(code or '').strip().upper()


def validate_country(code) -> str:
    """The code as stored (upper-case alpha-2), or ``ValidationError`` when it is
    not one the deployment enrols from."""
    normalized = normalize_country(code)
    if normalized not in _configured_codes():
        raise ValidationError('country must be one of the countries offered at sign-up.')
    return normalized


def validate_year_of_birth(year, country: str, today: date | None = None) -> int:
    """The year as an int, or ``ValidationError`` when it is not a year, is
    older than ``MAX_AGE`` years, or cannot reach the country's minimum age.

    Someone born ``minimum_age`` years ago may or may not have had their
    birthday, so that year is accepted; the declaration decides. A year after
    that cannot be of age and is refused whatever the member declares.
    """
    today = today or date.today()
    if isinstance(year, bool) or not isinstance(year, (int, str)):
        raise ValidationError('year_of_birth must be a four-digit year.')
    text = str(year).strip()
    if not (text.isascii() and text.isdigit() and len(text) == 4):
        raise ValidationError('year_of_birth must be a four-digit year.')
    value = int(text)
    if value < today.year - MAX_AGE:
        raise ValidationError('year_of_birth is too long ago.')
    if value > today.year:
        raise ValidationError('year_of_birth is in the future.')
    needed = minimum_age(country)
    if today.year - value < needed:
        raise ValidationError(f'You must be at least {needed} to sign up.')
    return value
