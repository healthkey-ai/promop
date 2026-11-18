"""Endpoints for public voting on the wearable mapping proposals (#2678).

Two audiences. Mapping administrators, signed in, make, revoke and delete the
links and read who voted; everyone who reads the mapping pages sees the
tallies. Voters have no account: they read the proposals, submit, and confirm
from an emailed link. The rules live in ``omop_core.services.mapping_votes``;
this module is who may call what.

The public views authenticate nobody (``authentication_classes = []``), so a
visitor's stale session cookie is never read, no CSRF check applies, and no
session is created. They are on ``/api/v1/`` only, answer JSON only, and take
their token in the request body: a token in a path or a query string would be
in every access log and in the path an error is reported under.
"""
from django.conf import settings
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from omop_core.models import MappingVoteShare, SourceCodeConceptMapping
from omop_core.services import mapping_votes as votes
from omop_core.services import wearable_mapping_export as export

from .permissions import is_machine_request

NOT_AVAILABLE = {'detail': 'This link is not available.'}
CONFIRMATION_INVALID = {'detail': 'This confirmation link is no longer valid.'}
_READ_DENIED = {'detail': 'Organization admin access required.'}
_ADMIN_DENIED = {'detail': 'Mapping administrator access required.'}


def _gates():
    # views.py imports half the project; reading its two gates at call time
    # keeps this module importable on its own and the rule in one place.
    from patient_portal.api.views import _can_approve_mappings, _can_view_mappings
    return _can_view_mappings, _can_approve_mappings


def _reader(request):
    """Whether the caller may read the mapping pages, as a person.

    A machine credential reads the mapping table (#2340) and nothing here:
    what people thought of a proposal, and who they are, is not reference
    data for a warehouse.
    """
    return not is_machine_request(request) and _gates()[0](request.user)


def _administrator(request):
    """Whether the caller may hand out voting links and read who voted.

    Staff and org admins, the people who approve mappings. Writing mappings
    is not enough: an analyst grant can be a self-signup at a public demo
    org, and a share sends mail from this deployment and gathers strangers'
    addresses.
    """
    return _reader(request) and _gates()[1](request.user)


def _no_store(response):
    response['Cache-Control'] = 'no-store'
    return response


# ── Signed in ────────────────────────────────────────────────────────────

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def vote_share_list(request):
    """The links to the public voting page.

    GET  /api/v1/code-mappings/vote-shares/
    POST /api/v1/code-mappings/vote-shares/  {"title", "note"?, "expires_at"?}

    Whoever reads the mapping pages sees that shares exist and how many
    people voted. The link itself is returned only to a mapping
    administrator, who is also the only one who may make a share. The answer
    says whether voting can work at all here: without a mail backend no voter
    receives a confirmation link.
    """
    if not _reader(request):
        return Response(_READ_DENIED, status=status.HTTP_403_FORBIDDEN)
    administrator = _administrator(request)
    if request.method == 'GET':
        return _no_store(Response({
            'results': [votes.serialize_share(share, with_link=administrator) for share in votes.shares()],
            'can_manage': administrator,
            'email_configured': votes.email_configured(),
            'base_url_configured': votes.base_url_configured(),
        }))
    if not administrator:
        return Response(_ADMIN_DENIED, status=status.HTTP_403_FORBIDDEN)
    if not isinstance(request.data, dict):
        return Response({'detail': 'A JSON object is required.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        share = votes.create_share(
            request.user, request.data.get('title'), request.data.get('note'),
            request.data.get('expires_at'),
        )
    except votes.SubmissionError as error:
        return Response({'detail': str(error)}, status=status.HTTP_400_BAD_REQUEST)
    return _no_store(Response(votes.serialize_share(share, with_link=True), status=status.HTTP_201_CREATED))


def _administered_share(request, share_id):
    """``(share, None)`` for a mapping administrator, else ``(None, refusal)``."""
    if not _reader(request):
        return None, Response(_READ_DENIED, status=status.HTTP_403_FORBIDDEN)
    if not _administrator(request):
        return None, Response(_ADMIN_DENIED, status=status.HTTP_403_FORBIDDEN)
    share = votes.shares().filter(pk=share_id).first()
    if share is None:
        return None, Response({'detail': 'Share not found.'}, status=status.HTTP_404_NOT_FOUND)
    return share, None


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def vote_share_detail(request, share_id):
    """DELETE /api/v1/code-mappings/vote-shares/<id>/

    Erases the share with every vote cast through it, the voters' addresses
    on them, and anything pending. For junk votes, and for a voter who asks
    to be removed. Revoke is the one that keeps the votes.
    """
    share, refusal = _administered_share(request, share_id)
    if refusal is not None:
        return refusal
    votes.delete_share(share)
    return _no_store(Response(status=status.HTTP_204_NO_CONTENT))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def vote_share_revoke(request, share_id):
    """POST /api/v1/code-mappings/vote-shares/<id>/revoke/

    The link stops opening at once. Votes already confirmed are kept; a
    submission still waiting for its confirmation is dropped.
    """
    share, refusal = _administered_share(request, share_id)
    if refusal is not None:
        return refusal
    votes.revoke_share(share)
    return _no_store(Response(votes.serialize_share(share, with_link=True)))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def vote_share_votes_export(request, share_id):
    """GET /api/v1/code-mappings/vote-shares/<id>/votes-export/

    Every confirmed vote of one share as a CSV file. It carries the voters'
    addresses, so it is for mapping administrators.
    """
    share, refusal = _administered_share(request, share_id)
    if refusal is not None:
        return refusal
    response = HttpResponse(votes.share_votes_csv(share), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="mapping-votes-share-{share.pk}.csv"'
    return _no_store(response)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def code_mapping_votes(request, mapping_id):
    """GET /api/v1/code-mappings/<id>/votes/

    The confirmed votes on one mapping and the current tally. A voter's
    address is in the answer only for a mapping administrator; everyone else
    who reads mappings gets the choices and comments.
    """
    if not _reader(request):
        return Response(_READ_DENIED, status=status.HTTP_403_FORBIDDEN)
    mapping = SourceCodeConceptMapping.objects.filter(pk=mapping_id).only(
        'id', 'target_concept_id').first()
    if mapping is None:
        return Response({'detail': 'Mapping not found.'}, status=status.HTTP_404_NOT_FOUND)
    return _no_store(Response(votes.mapping_votes(mapping, include_emails=_administrator(request))))


# ── The public ───────────────────────────────────────────────────────────

def client_address(request):
    """The address the public voting throttles count a client under.

    DRF's own ``get_ident`` keys on the whole ``X-Forwarded-For`` header
    while ``NUM_PROXIES`` is unset, so a client that sends a different header
    each time is a new client each time. Here the deployment says how many
    proxies it sits behind (``MAPPING_VOTE_PROXY_HOPS``) and the entry that
    many from the right is read: the one the outermost trusted proxy wrote,
    which the client cannot reach. With fewer entries than hops the header is
    not what the deployment described, so the socket's peer is used.
    """
    hops = settings.MAPPING_VOTE_PROXY_HOPS
    peer = request.META.get('REMOTE_ADDR') or ''
    if hops < 1:
        return peer
    entries = [entry.strip() for entry in request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')]
    entries = [entry for entry in entries if entry]
    return entries[-hops] if len(entries) >= hops else peer


class PublicVoteThrottle(ScopedRateThrottle):
    """The view's scope, counted per ``client_address``. Never per user: the
    views read no session, so signing in buys no fresh bucket."""

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': client_address(request)}


@method_decorator(csrf_exempt, name='dispatch')
class _PublicVoteView(APIView):
    """No account, no session, no cookie; JSON in and JSON out.

    Every request is a POST whose body carries the token. ``as_view`` already
    exempts the view from CSRF; the decorator says so where a reader looks
    for it. JSON is the only renderer because the browsable API would draw a
    form around whatever the visitor posted, their address included.
    """
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [PublicVoteThrottle]
    parser_classes = [JSONParser]
    renderer_classes = [JSONRenderer]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        response['Referrer-Policy'] = 'no-referrer'
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response

    def body(self, request):
        """The request's JSON object, or ``None`` with the refusal to return."""
        try:
            declared = int(request.META.get('CONTENT_LENGTH') or 0)
        except (TypeError, ValueError):
            declared = 0
        # The declared length first, so an oversized body is refused unread;
        # then the bytes themselves, for a request that declared none.
        if declared > votes.MAX_BODY_BYTES or len(request.body) > votes.MAX_BODY_BYTES:
            return None, Response(
                {'detail': 'This request is too large.'}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        try:
            data = request.data
        except RecursionError:
            # JSON nested deeper than the parser's stack. DRF turns a
            # malformed body into a 400 itself, but not this.
            raise ParseError('Malformed JSON.')
        if not isinstance(data, dict):
            return None, Response({'detail': 'A JSON object is required.'}, status=status.HTTP_400_BAD_REQUEST)
        return data, None


def _not_available():
    return Response(NOT_AVAILABLE, status=status.HTTP_404_NOT_FOUND)


class PublicVotePageView(_PublicVoteView):
    """POST /api/v1/public/mapping-votes/  {"token"} -- the proposals to vote on.

    A read, as a POST: the token must not be in the URL. It also clears
    expired submissions, which nothing else does on a deployment where nobody
    has submitted since.
    """
    throttle_scope = 'mapping_vote_read'

    def post(self, request):
        data, refusal = self.body(request)
        if refusal is not None:
            return refusal
        share = votes.active_share(data.get('token'))
        if share is None:
            return _not_available()
        votes.purge_expired()
        return Response(votes.public_payload(share))


class PublicVoteExportView(_PublicVoteView):
    """POST /api/v1/public/mapping-votes/export/

    ``{"token": "...", "file_format": "csv"|"docx", "destination": "omop"|"cdisc-bc"}``
    -- the wearable mappings as a file, for whoever holds the link. The same
    file the Wearables tab downloads (every global mapping that is not
    rejected, with an OMOP concept or a CDISC Biomedical Concept as the
    target), without its provenance and source units, which the public page
    does not show either. A POST for the reason the page is: the token must
    not be in a URL. The answer is the file, not JSON; a refusal is JSON.
    """
    throttle_scope = 'mapping_vote_export'

    def post(self, request):
        data, refusal = self.body(request)
        if refusal is not None:
            return refusal
        if votes.active_share(data.get('token')) is None:
            return _not_available()
        file_format = data.get('file_format') or export.FORMAT_CSV
        destination = data.get('destination') or export.DEST_OMOP
        # Checked here, with fixed wording: nothing an exception says reaches a stranger.
        if (not isinstance(file_format, str) or not isinstance(destination, str)
                or file_format not in export.CONTENT_TYPES or destination not in export.DESTINATIONS):
            return Response({'detail': 'Unknown file format or destination.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            filename, content_type, content = export.export_file(
                file_format, destination=destination, public=True)
        except export.FormatError:
            # What is left is the Word document's row limit.
            return Response({'detail': 'This file is too large to build. Download the CSV instead.'},
                            status=status.HTTP_400_BAD_REQUEST)
        response = HttpResponse(content, content_type=content_type)
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


class PublicVoteSubmitView(_PublicVoteView):
    """POST /api/v1/public/mapping-votes/submit/

    ``{"token": "...", "email": "...", "votes": [{"mapping": id, "choice":
    "yes"|"no"|"", "comment": "..."}]}``. Stores nothing as a vote: the
    submission waits for the emailed confirmation. The 202 is the same
    whether or not the address has voted before and whether or not it is over
    its own limit. A limit that concerns no address is a 503.
    """
    throttle_scope = 'mapping_vote_submit'

    def post(self, request):
        data, refusal = self.body(request)
        if refusal is not None:
            return refusal
        share = votes.active_share(data.get('token'))
        if share is None:
            return _not_available()
        try:
            votes.submit(share, data.get('email'), data.get('votes'))
        except votes.SubmissionError as error:
            return Response({'detail': str(error)}, status=status.HTTP_400_BAD_REQUEST)
        except votes.Unavailable:
            return Response({'detail': votes.UNAVAILABLE_DETAIL}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'detail': votes.SUBMITTED_DETAIL}, status=status.HTTP_202_ACCEPTED)


class PublicVotePreviewView(_PublicVoteView):
    """POST /api/v1/public/mapping-votes/confirm/preview/  {"token"}

    What the submission holds. It records nothing, however often it is
    called: the confirmation page calls it on load, and so may anything that
    follows the emailed link.
    """
    throttle_scope = 'mapping_vote_confirm'

    def post(self, request):
        data, refusal = self.body(request)
        if refusal is not None:
            return refusal
        summary = votes.pending_summary(data.get('token'))
        if summary is None:
            return Response(CONFIRMATION_INVALID, status=status.HTTP_404_NOT_FOUND)
        return Response(summary)


class PublicVoteConfirmView(_PublicVoteView):
    """POST /api/v1/public/mapping-votes/confirm/  {"token"} -- records the votes, once."""
    throttle_scope = 'mapping_vote_confirm'

    def post(self, request):
        data, refusal = self.body(request)
        if refusal is not None:
            return refusal
        result = votes.confirm(data.get('token'))
        if result is None:
            return Response(CONFIRMATION_INVALID, status=status.HTTP_404_NOT_FOUND)
        return Response(result)
