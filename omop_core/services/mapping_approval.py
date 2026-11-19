"""The two-person rule for code mappings: a proposer may not approve.

Approval is the one mapping transition that rewrites stored patient data, so
the curator who proposed a destination cannot also be the one who signs it off
(#2726). ``proposed_by`` records that curator on ``SourceCodeConceptMapping``
and ``SourceCodeAxisVariant``:

* set when a person creates the row, moves its destination, or re-opens it
  from rejected;
* left alone by a note fix, an un-approval or any machine write, so it names
  the person behind the destination now on the row;
* null for a machine proposal (Suggest, importers, reverse Suggest), which
  any approver may sign off.

``MAPPING_SEPARATE_APPROVER`` (default on) is the deployment switch.
"""
from django.conf import settings
from rest_framework.exceptions import PermissionDenied

#: The reason shown to the curator, and in a 403 body's ``code``.
SELF_APPROVAL_CODE = 'self_approval'
SELF_APPROVAL_MESSAGE = (
    'You proposed this mapping, so another approver must approve it. '
    'Ask an organization admin who did not propose it.'
)


class SelfApprovalError(PermissionDenied):
    """The approver is the person who proposed what they are approving.

    A DRF ``PermissionDenied``, so every view that lets it propagate answers
    403 ``{"detail": ..., "code": "self_approval"}``; the group job catches it
    to skip the row.
    """

    def __init__(self, message: str = SELF_APPROVAL_MESSAGE):
        super().__init__(detail={'detail': message, 'code': SELF_APPROVAL_CODE})


def enabled() -> bool:
    return bool(getattr(settings, 'MAPPING_SEPARATE_APPROVER', True))


#: The issuer of the identity a service token authenticates as.
SERVICE_ISSUER = 'urn:service'


def is_person(user) -> bool:
    """A signed-in human. A service credential is an identity too, but a machine."""
    return bool(user is not None and getattr(user, 'is_authenticated', False)
                and getattr(user, 'pk', None) is not None
                and getattr(user, 'issuer', None) != SERVICE_ISSUER)


def blocked_for(user, proposed_by_id) -> bool:
    """Whether ``user`` may not approve a row proposed by ``proposed_by_id``."""
    return bool(enabled() and is_person(user) and proposed_by_id is not None
                and proposed_by_id == user.pk)


def check_can_approve(user, proposed_by_id) -> None:
    """Raise ``SelfApprovalError`` when ``user`` is the row's proposer."""
    if blocked_for(user, proposed_by_id):
        raise SelfApprovalError()
