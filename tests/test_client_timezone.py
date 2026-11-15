from datetime import datetime, timezone as datetime_timezone
from types import SimpleNamespace

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.services.client_timezone import request_timezone, use_request_timezone
from patient_portal.models import Identity
from tests.factories import ConceptFactory, PatientRecordFactory


pytestmark = pytest.mark.django_db


def request_with_timezone(name=None):
    return SimpleNamespace(headers={'X-Client-Timezone': name} if name else {})


def test_request_timezone_uses_a_valid_browser_timezone():
    assert request_timezone(request_with_timezone('America/Los_Angeles')).key == (
        'America/Los_Angeles'
    )


def test_missing_or_invalid_request_timezone_uses_configured_default():
    configured = timezone.get_default_timezone()
    assert request_timezone(request_with_timezone()) == configured
    assert request_timezone(request_with_timezone('not/a-zone')) == configured


@pytest.mark.parametrize(
    ('zone', 'expected'),
    [
        ('America/Los_Angeles', '2026-01-01'),
        ('Pacific/Kiritimati', '2026-01-02'),
    ],
)
def test_local_date_follows_browser_zone_across_utc_midnight(monkeypatch, zone, expected):
    # At this instant it is still Jan 1 in Los Angeles, but already Jan 2 in
    # Kiritimati. A shared UTC "today" rejected the former user's Jan 1 input.
    now = datetime(2026, 1, 2, 1, 0, tzinfo=datetime_timezone.utc)
    monkeypatch.setattr(timezone, 'now', lambda: now)

    with use_request_timezone(request_with_timezone(zone)):
        assert timezone.localdate().isoformat() == expected


def test_native_patient_record_patch_uses_the_browser_calendar_day(monkeypatch):
    """The legacy/native PATCH validates dated edits in the client's zone too."""
    record = PatientRecordFactory()
    ConceptFactory(concept_code='718-7', concept_name='Hemoglobin [Mass/volume] in Blood')
    monkeypatch.setattr(
        timezone, 'now',
        lambda: datetime(2026, 1, 1, 12, 0, tzinfo=datetime_timezone.utc),
    )
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='native-local-day@example.test', is_staff=True,
    ))

    response = client.patch(
        f'/api/patient-info/{record.person_id}/',
        {'hemoglobin_level': {'value': 120, 'date': '2026-01-02'},
         'hemoglobin_level_units': 'G/L'},
        format='json',
        HTTP_X_CLIENT_TIMEZONE='Pacific/Kiritimati',
    )

    assert response.status_code == 200, response.data
