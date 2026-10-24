"""The deployed commit under the logo (#2016)."""
import pytest
from rest_framework.test import APIClient

from patient_portal.api import version

pytestmark = pytest.mark.django_db


def test_the_deploys_commit_is_public_and_shortened(monkeypatch):
    monkeypatch.setenv('RENDER_GIT_COMMIT', '8798ec24f4cc16e47f8c0ceec69d38d7e5858b17')
    response = APIClient().get('/api/v1/version/')
    assert response.status_code == 200
    assert response.data == {'commit': '8798ec24f4cc16e47f8c0ceec69d38d7e5858b17', 'short': '8798ec24'}


def test_a_stray_environment_value_is_never_reflected(monkeypatch):
    monkeypatch.setenv('RENDER_GIT_COMMIT', 'not-a-commit; rm -rf /')
    monkeypatch.delenv('GIT_COMMIT', raising=False)
    monkeypatch.setattr(version, '_checkout_commit', lambda: '')
    assert APIClient().get('/api/v1/version/').data == {'commit': '', 'short': ''}


def test_a_checkout_answers_with_its_head(monkeypatch):
    monkeypatch.delenv('RENDER_GIT_COMMIT', raising=False)
    monkeypatch.delenv('GIT_COMMIT', raising=False)
    monkeypatch.setattr(version, '_checkout_commit', lambda: 'abcdef1234567')
    assert APIClient().get('/api/v1/version/').data['short'] == 'abcdef12'
