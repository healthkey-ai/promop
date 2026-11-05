"""``/api/v1/phr/account/`` — the signed-in member closing their account.

DELETE account/   ``{"confirm": "DELETE"}``; deletes the account (Identity,
                  PatientUser), the record, the member's clinical rows and
                  their PROlog responses in one transaction, and writes a
                  deletion log entry. The consent ledger stays.

The body's confirmation is required so that no client can close an account
with a stray request. See docs/account-deletion.md.
"""
from __future__ import annotations

from django.contrib.auth import logout
from rest_framework.response import Response

from patient_portal.account_deletion import delete_member
from patient_portal.models import DeletionLogEntry

from .consent import body_of
from .views import PhrView


class AccountView(PhrView):
    http_method_names = ['delete', 'head', 'options']

    def delete(self, request):
        person, record, error = self.resolve(request)
        if error:
            return error
        if body_of(request).get('confirm') != 'DELETE':
            return Response({'detail': 'Request body must include {"confirm": "DELETE"}.'}, status=400)
        entry = delete_member(person, reason=DeletionLogEntry.ACCOUNT_CLOSED, identity=request.user)
        # Only after the transaction committed: a rolled-back deletion must
        # not have flushed the session.
        logout(request)
        return Response({
            'detail': 'Your account and all of its data have been deleted.',
            'deleted_at': entry.deleted_at,
        })
