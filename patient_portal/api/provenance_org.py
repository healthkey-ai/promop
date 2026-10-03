"""The hospital a request names as the source of the clinical data it writes (#1690)."""

from __future__ import annotations

import re
from typing import Any

from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from omop_core.models import Organization
from omop_core.services.provenance_attribution import ATTRIBUTION, with_attribution

from .permissions import get_request_org, is_machine_request

PROVENANCE_ORG_HEADER = 'X-Provenance-Organization-ID'
PROVENANCE_ORG_META = 'HTTP_X_PROVENANCE_ORGANIZATION_ID'

_MAX_ID = 2 ** 63 - 1
_ID_PATTERN = re.compile(r'[1-9][0-9]{0,18}')
_MALFORMED = 'Must be a positive integer organization id.'


class ProvenanceConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'Row already attributed to another organization.'
    default_code = 'provenance_conflict'

    def __init__(self, payload: dict[str, Any]):
        super().__init__()
        self.detail = payload


def parse_organization_id(raw: Any) -> int:
    if isinstance(raw, bool):
        raise ValidationError({'organization_id': _MALFORMED})
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and _ID_PATTERN.fullmatch(raw.strip()):
        value = int(raw.strip())
    else:
        raise ValidationError({'organization_id': _MALFORMED})
    if not 1 <= value <= _MAX_ID:
        raise ValidationError({'organization_id': _MALFORMED})
    return value


def resolve_explicit_organization(request, raw: Any, *, present: bool,
                                  require_active: bool = False) -> Organization | None:
    request_organization = get_request_org(request)
    if not present:
        return request_organization

    organization = Organization.objects.filter(pk=parse_organization_id(raw)).first()
    if organization is None:
        raise ValidationError({'organization_id': 'Organization not found.'})
    if require_active and not organization.is_active:
        raise ValidationError({'organization_id': 'Organization is inactive.'})
    if request_organization is not None:
        if organization != request_organization:
            raise PermissionDenied(
                {'organization_id': 'Organization does not match the authenticated service.'})
        return organization
    if not (is_machine_request(request) or getattr(request.user, 'is_staff', False)):
        raise PermissionDenied(
            {'organization_id': 'Explicit organization requires a machine or staff credential.'})
    return organization


def header_organization(request) -> tuple[Organization | None, bool]:
    present = PROVENANCE_ORG_META in request.META
    organization = resolve_explicit_organization(
        request, request.META.get(PROVENANCE_ORG_META), present=present, require_active=True)
    return organization, present


def raise_on_foreign_attribution(model_cls, object_ids, organization: Organization | None) -> None:
    if organization is None:
        return
    rows = with_attribution(model_cls.objects.filter(pk__in=list(object_ids))).order_by('pk')
    conflicts = [
        {'id': pk, 'organization_id': other[0]}
        for pk, attribution in rows.values_list('pk', ATTRIBUTION)
        if (other := [org_id for org_id in attribution if org_id != organization.pk])
    ]
    if conflicts:
        raise ProvenanceConflict({
            'detail': 'Row already attributed to another organization.',
            'conflicts': conflicts[:20],
        })
