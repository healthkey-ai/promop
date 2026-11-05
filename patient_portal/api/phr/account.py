"""``/api/v1/phr/account/`` — the signed-in member closing their account.

DELETE account/   ``{"confirm": "DELETE"}``; deletes the account (Identity,
                  PatientUser), the record, the member's clinical rows and
                  their PROlog responses in one transaction, and writes a
                  deletion log entry. The consent ledger stays. A login that
                  is staff or holds provider access is kept; only its
                  patient side goes.

The body's confirmation is required so that no client can close an account
with a stray request, and only a session or partner login may send it: an
OAuth2 token or service credential presented without the member is refused
(403). See docs/account-deletion.md.
"""
from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from patient_portal.account_deletion import (
    AlreadyDeleted, actor_snapshot, delete_member, end_session, patient_only_identity,
)
from patient_portal.api.permissions import InteractiveSessionForWrites
from patient_portal.models import DeletionLogEntry

from .consent import body_of
from .views import PhrView


class AccountView(PhrView):
    permission_classes = [IsAuthenticated, InteractiveSessionForWrites]
    http_method_names = ['delete', 'options']

    def delete(self, request):
        person, record, error = self.resolve(request)
        if error:
            return error
        if body_of(request).get('confirm') != 'DELETE':
            return Response({'detail': 'Request body must include {"confirm": "DELETE"}.'}, status=400)
        actor = actor_snapshot(request.user)
        person_id = person.person_id  # the instance loses its pk on delete
        identity = patient_only_identity(person)
        try:
            entry = delete_member(person, reason=DeletionLogEntry.ACCOUNT_CLOSED, identity=identity)
        except AlreadyDeleted:
            return Response({'detail': 'No record.'}, status=404)
        end_session(request, actor, person_id, identity)
        return Response({
            'detail': 'Your account and all of its data have been deleted.',
            'deleted_at': entry.deleted_at,
        })
