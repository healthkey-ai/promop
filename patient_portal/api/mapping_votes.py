"""Endpoints for public voting on the wearable mapping proposals (#2678).

Two audiences. Curators, signed in, make and revoke the links and read what
came back. Voters have no account: they read the proposals, submit, and
confirm from an emailed link. The rules live in
``omop_core.services.mapping_votes``; this module is who may call what.

The public views authenticate nobody (``authentication_classes = []``), so a
visitor's stale session cookie is never read, no CSRF check applies, and no
session is created. They are on ``/api/v1/`` only.
"""
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from omop_core.models import MappingVoteShare, SourceCodeConceptMapping
from omop_core.services import mapping_votes as votes

NOT_AVAILABLE = {'detail': 'This link is not available.'}
CONFIRMATION_INVALID = {'detail': 'This confirmation link is no longer valid.'}
_READ_DENIED = {'detail': 'Organization admin access required.'}
_WRITE_DENIED = {'detail': 'Mapping curator access required.'}


def _gates():
    # views.py imports half the project; reading its two gates at call time
    # keeps this module importable on its own and the rule in one place.
    from patient_portal.api.views import _can_manage_mappings, _can_view_mappings
    return _can_view_mappings, _can_manage_mappings


def _sees_emails(user):
    """A voter's address is personal data: for those who write mappings."""
    return _gates()[1](user)


# ── Curators ─────────────────────────────────────────────────────────────

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def vote_share_list(request):
    """The links to the public voting page.

    GET  /api/v1/code-mappings/vote-shares/
    POST /api/v1/code-mappings/vote-shares/  {"title", "note"?, "expires_at"?}

    Whoever reads the mapping pages sees that shares exist and how many
    people voted; the link itself is returned only to those who may write
    mappings, who are also the only ones who may make one.
    """
    can_view, can_manage = _gates()
    if not can_view(request.user):
        return Response(_READ_DENIED, status=status.HTTP_403_FORBIDDEN)
    writer = can_manage(request.user)
    if request.method == 'GET':
        response = Response({'results': [
            votes.serialize_share(share, with_link=writer) for share in votes.shares()
        ]})
        response['Cache-Control'] = 'no-store'
        return response
    if not writer:
        return Response(_WRITE_DENIED, status=status.HTTP_403_FORBIDDEN)
    if not isinstance(request.data, dict):
        return Response({'detail': 'A JSON object is required.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        share = votes.create_share(
            request.user, request.data.get('title'), request.data.get('note'),
            request.data.get('expires_at'),
        )
    except votes.SubmissionError as error:
        return Response({'detail': str(error)}, status=status.HTTP_400_BAD_REQUEST)
    response = Response(votes.serialize_share(share, with_link=True), status=status.HTTP_201_CREATED)
    response['Cache-Control'] = 'no-store'
    return response


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def vote_share_revoke(request, share_id):
    """POST /api/v1/code-mappings/vote-shares/<id>/revoke/

    The link stops opening at once. Votes already confirmed are kept; a
    submission still waiting for its confirmation is dropped.
    """
    can_view, can_manage = _gates()
    if not can_view(request.user):
        return Response(_READ_DENIED, status=status.HTTP_403_FORBIDDEN)
    if not can_manage(request.user):
        return Response(_WRITE_DENIED, status=status.HTTP_403_FORBIDDEN)
    share = votes.shares().filter(pk=share_id).first()
    if share is None:
        return Response({'detail': 'Share not found.'}, status=status.HTTP_404_NOT_FOUND)
    votes.revoke_share(share)
    response = Response(votes.serialize_share(share, with_link=True))
    response['Cache-Control'] = 'no-store'
    return response


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def vote_share_votes_export(request, share_id):
    """GET /api/v1/code-mappings/vote-shares/<id>/votes-export/

    Every confirmed vote of one share as a CSV file. It carries the voters'
    addresses, so it is for those who may write mappings.
    """
    can_view, can_manage = _gates()
    if not can_view(request.user):
        return Response(_READ_DENIED, status=status.HTTP_403_FORBIDDEN)
    if not can_manage(request.user):
        return Response(_WRITE_DENIED, status=status.HTTP_403_FORBIDDEN)
    share = MappingVoteShare.objects.filter(pk=share_id).first()
    if share is None:
        return Response({'detail': 'Share not found.'}, status=status.HTTP_404_NOT_FOUND)
    response = HttpResponse(votes.share_votes_csv(share), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="mapping-votes-share-{share.pk}.csv"'
    response['Cache-Control'] = 'no-store'
    return response


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def code_mapping_votes(request, mapping_id):
    """GET /api/v1/code-mappings/<id>/votes/

    The confirmed votes on one mapping and the current tally. A read-only
    reader gets the choices and comments without the voters' addresses.
    """
    can_view, _ = _gates()
    if not can_view(request.user):
        return Response(_READ_DENIED, status=status.HTTP_403_FORBIDDEN)
    mapping = SourceCodeConceptMapping.objects.filter(pk=mapping_id).only(
        'id', 'target_concept_id').first()
    if mapping is None:
        return Response({'detail': 'Mapping not found.'}, status=status.HTTP_404_NOT_FOUND)
    response = Response(votes.mapping_votes(mapping, include_emails=_sees_emails(request.user)))
    response['Cache-Control'] = 'no-store'
    return response


# ── The public ───────────────────────────────────────────────────────────

class _PublicVoteView(APIView):
    """No account, no session, no cookie; throttled by client address.

    ``ScopedRateThrottle`` keys an anonymous request on DRF's ``get_ident``,
    the same client address every other anonymous endpoint here is limited
    by. ``as_view`` already exempts the view from CSRF; the decorator on the
    subclasses says so where a reader looks for it.
    """
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        # The address of these pages carries a token: keep it out of the
        # Referer of anything the page links to, and out of search indexes.
        response['Referrer-Policy'] = 'no-referrer'
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response


def _not_available():
    return Response(NOT_AVAILABLE, status=status.HTTP_404_NOT_FOUND)


class PublicVotePageView(_PublicVoteView):
    """GET /api/v1/public/mapping-votes/<token>/ -- the proposals to vote on."""
    throttle_scope = 'mapping_vote_read'

    def get(self, request, token):
        share = votes.active_share(token)
        if share is None:
            return _not_available()
        return Response(votes.public_payload(share))


@method_decorator(csrf_exempt, name='dispatch')
class PublicVoteSubmitView(_PublicVoteView):
    """POST /api/v1/public/mapping-votes/<token>/submit/

    ``{"email": "...", "votes": [{"mapping": id, "choice": "yes"|"no"|"",
    "comment": "..."}]}``. Stores nothing as a vote: the submission waits for
    the emailed confirmation. The answer is the same whether or not the
    address has voted before, and whether or not an email was sent.
    """
    throttle_scope = 'mapping_vote_submit'

    def post(self, request, token):
        share = votes.active_share(token)
        if share is None:
            return _not_available()
        too_large = Response(
            {'detail': 'This submission is too large.'},
            status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        try:
            declared = int(request.META.get('CONTENT_LENGTH') or 0)
        except (TypeError, ValueError):
            declared = 0
        # The declared length first, so an oversized body is refused unread;
        # then the bytes themselves, for a request that declared none.
        if declared > votes.MAX_BODY_BYTES or len(request.body) > votes.MAX_BODY_BYTES:
            return too_large
        data = request.data
        if not isinstance(data, dict):
            return Response({'detail': 'A JSON object is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            votes.submit(share, data.get('email'), data.get('votes'))
        except votes.SubmissionError as error:
            return Response({'detail': str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'detail': votes.SUBMITTED_DETAIL}, status=status.HTTP_202_ACCEPTED)


@method_decorator(csrf_exempt, name='dispatch')
class PublicVoteConfirmView(_PublicVoteView):
    """/api/v1/public/mapping-votes/confirm/<confirm_token>/

    GET shows what the submission holds and changes nothing, so a mail
    scanner that follows the emailed link confirms no vote. POST records the
    votes, once.
    """
    throttle_scope = 'mapping_vote_confirm'

    def get(self, request, confirm_token):
        summary = votes.pending_summary(confirm_token)
        if summary is None:
            return Response(CONFIRMATION_INVALID, status=status.HTTP_404_NOT_FOUND)
        return Response(summary)

    def post(self, request, confirm_token):
        result = votes.confirm(confirm_token)
        if result is None:
            return Response(CONFIRMATION_INVALID, status=status.HTTP_404_NOT_FOUND)
        return Response(result)
