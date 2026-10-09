"""Service credentials must obey method scopes across every permission variant."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.test import override_settings
from django.utils import timezone

from patient_portal.api.permissions import (
    EtlPatientCrudPermission,
    EtlWritePermission,
    SERVICE_TOKEN,
    LabSyncPermission,
    PatientCrudPermission,
    PatientDeletePermission,
    ScopedTokenPermission,
    VocabReadPermission,
)
from patient_portal.api.providers.base import TokenClaims


PERMISSIONS = (
    ScopedTokenPermission, VocabReadPermission, LabSyncPermission,
    PatientCrudPermission, PatientDeletePermission,
)
ETL_PERMISSIONS = (EtlWritePermission, EtlPatientCrudPermission)
METHODS = ('GET', 'HEAD', 'OPTIONS', 'POST', 'PUT', 'PATCH', 'DELETE')


@pytest.mark.parametrize('permission_class', PERMISSIONS)
@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('scope,allowed_methods', (
    ('', ()),
    ('openid patient/Observation.read', ()),
    ('patient/*.read', ('GET', 'HEAD', 'OPTIONS')),
    ('user/*.read', ('GET', 'HEAD', 'OPTIONS')),
    ('patient/*.write', ('POST', 'PUT', 'PATCH', 'DELETE')),
    ('user/*.write', ('POST', 'PUT', 'PATCH', 'DELETE')),
    ('  patient/*.read\tpatient/*.write  ', METHODS),
    ('system/*.read', ('GET', 'HEAD', 'OPTIONS')),
))
def test_service_scopes(permission_class, method, scope, allowed_methods):
    # Staff status must not turn a scoped credential into unrestricted access.
    request = SimpleNamespace(
        auth=SERVICE_TOKEN, method=method,
        user=SimpleNamespace(is_authenticated=True, is_staff=True),
    )
    allowed = method in allowed_methods
    if scope == 'system/*.read' and permission_class is not VocabReadPermission:
        allowed = False
    with override_settings(SERVICE_AUTH_SCOPES=scope):
        assert permission_class().has_permission(request, None) is allowed


@pytest.mark.parametrize('permission_class', ETL_PERMISSIONS)
@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('scope', ('system/etl.write', 'patient/*.read system/etl.write'))
def test_etl_capability_is_non_destructive(permission_class, method, scope):
    request = SimpleNamespace(
        auth=SERVICE_TOKEN, method=method,
        user=SimpleNamespace(is_authenticated=True, is_staff=False),
    )
    expected = method in ('POST', 'PUT', 'PATCH')
    if method in ('GET', 'HEAD', 'OPTIONS'):
        expected = 'patient/*.read' in scope
    with override_settings(SERVICE_AUTH_SCOPES=scope):
        assert permission_class().has_permission(request, None) is expected


@pytest.mark.parametrize('permission_class', PERMISSIONS)
@pytest.mark.parametrize('method', METHODS)
def test_etl_capability_is_rejected_by_ordinary_permissions(permission_class, method):
    request = SimpleNamespace(
        auth=SERVICE_TOKEN, method=method,
        user=SimpleNamespace(is_authenticated=True, is_staff=False),
    )
    with override_settings(SERVICE_AUTH_SCOPES='system/etl.write'):
        assert permission_class().has_permission(request, None) is False


@pytest.mark.parametrize('permission_class', ETL_PERMISSIONS)
def test_oauth_token_cannot_use_legacy_etl_capability(permission_class):
    token = SimpleNamespace(
        scope='system/etl.write', expires=timezone.now() + timedelta(hours=1),
    )
    request = SimpleNamespace(
        auth=token, method='POST',
        user=SimpleNamespace(is_authenticated=True, is_staff=False),
    )
    assert permission_class().has_permission(request, None) is False


@pytest.mark.parametrize('permission_class', PERMISSIONS)
@pytest.mark.parametrize('scope,method,expired,allowed', (
    ('patient/*.read', 'DELETE', False, False),
    ('patient/*.write', 'DELETE', False, True),
    ('patient/*.write', 'DELETE', True, False),
    ('patient/*.read', 'GET', True, False),
    ('system/*.read', 'GET', False, True),
    ('system/*.read', 'GET', True, False),
))
def test_oauth_scopes(permission_class, scope, method, expired, allowed):
    token = SimpleNamespace(
        scope=scope, expires=timezone.now() + timedelta(hours=-1 if expired else 1),
    )
    request = SimpleNamespace(
        auth=token, method=method,
        user=SimpleNamespace(is_authenticated=True, is_staff=True),
    )
    if scope == 'system/*.read' and permission_class is not VocabReadPermission:
        allowed = False
    assert permission_class().has_permission(request, None) is allowed


@pytest.mark.parametrize('auth', (
    None, TokenClaims(issuer='test', sub='patient', email='', name='', raw={}),
))
@pytest.mark.parametrize('authenticated', (True, False))
def test_patient_delete_exception_remains_for_end_users(auth, authenticated):
    request = SimpleNamespace(
        auth=auth, method='DELETE',
        user=SimpleNamespace(is_authenticated=authenticated, is_staff=False),
    )
    assert PatientDeletePermission().has_permission(request, None) is authenticated
