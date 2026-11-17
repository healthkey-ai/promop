"""Public voting on the wearable mapping proposals (#2678).

A mapping administrator shares a link. Whoever holds it reads the proposals
without an account, marks Yes, No or a comment on any of them, and gives an
email address. That submission is not a vote: it is stored as pending and a
confirmation link is emailed. Only the confirmation behind that link writes
vote rows, so an address somebody else typed never votes.

A vote never changes a mapping. Curators read the tallies and approve or
reject as before.

Everything a voter can reach here is reference data: the proposals are the
global wearable mappings with status ``proposed``, a metric key and a loaded
destination concept, read live. No patient data is reachable from a share.

Both tokens are credentials. They travel in a URL fragment and a request
body, never in a path or a query string, so no access log, audit row or error
report holds one.
"""
import csv
import datetime
import hashlib
import hmac
import io
import ipaddress
import logging
import re
import secrets
import zlib

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage, get_connection
from django.core.validators import validate_email
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.crypto import salted_hmac

from omop_core.models import (
    MappingVote, MappingVoteEmail, MappingVoteShare, MappingVoteSubmission,
    SourceCodeConceptMapping,
)
from omop_core.services import wearable_mapping_export as export
from omop_core.services.disease_field_sets import csv_cell

logger = logging.getLogger(__name__)

#: How long a submission waits for its confirmation.
PENDING_LIFETIME = datetime.timedelta(hours=48)
#: The window of the limits on confirmation emails.
EMAIL_WINDOW = datetime.timedelta(hours=24)
#: The furthest ahead a share may be set to expire.
MAX_SHARE_LIFETIME = datetime.timedelta(days=2 * 366)
TITLE_MAX_LENGTH = 200
NOTE_MAX_LENGTH = 2000
COMMENT_MAX_LENGTH = 1000
EMAIL_MAX_LENGTH = 254
#: A request body larger than this is refused before it is parsed.
MAX_BODY_BYTES = 200 * 1024
CHOICES = (MappingVote.YES, MappingVote.NO, '')

SUBMITTED_DETAIL = (
    'Check your email to confirm your votes. They are not recorded until you confirm.'
)
UNAVAILABLE_DETAIL = 'Voting is temporarily unavailable. Please try again later.'
CSV_COLUMNS = (
    'Share', 'Category', 'Metric key', 'Source', 'Source code', 'Description',
    'Voted on destination', 'Voted on concept', 'OMOP concept ID', 'Vote', 'Comment',
    'Voter email', 'Confirmed at', 'Stale',
)

#: What both tokens look like. Checked before any query, so nothing a
#: stranger types as a token reaches the database unless it could be one.
TOKEN_SHAPE = re.compile(r'[A-Za-z0-9_-]{20,128}')

_EMAIL_SALT = 'omop_core.mapping_votes.email'
# C0 and C1 controls other than the line feed, the line and paragraph
# separators, and the bidirectional overrides and isolates that can reorder
# how a line reads.
_CONTROL = re.compile(r'[\x00-\x09\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]')
_LINK = re.compile(r'https?://|www\.', re.IGNORECASE)
_LOCAL_BASE = re.compile(r'https?://(localhost|127\.0\.0\.1|\[::1\])(:|/|$)')
# Backends that write a message somewhere on this machine instead of sending
# it. The in-memory one is what a test run uses, and prints nothing.
_BACKENDS_THAT_PRINT = frozenset({
    'django.core.mail.backends.console.EmailBackend',
    'django.core.mail.backends.dummy.EmailBackend',
    'django.core.mail.backends.filebased.EmailBackend',
})
_MAIL_BACKENDS_THAT_DELIVER_NOTHING = _BACKENDS_THAT_PRINT | {
    'django.core.mail.backends.locmem.EmailBackend',
}


class SubmissionError(ValueError):
    """The request is refused; the message is safe to show whoever sent it."""


class Unavailable(Exception):
    """Voting cannot take a submission right now: a limit on outgoing mail
    that concerns no particular address has been reached."""


def _plain(value, label):
    """*value* as text without control characters.

    Text that is not valid Unicode (a lone surrogate from a JSON escape) is
    refused here: the database and the mail backend both refuse it later, as
    an error rather than an answer.
    """
    if value is None:
        return ''
    if not isinstance(value, str):
        raise SubmissionError(f'{label} must be text.')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise SubmissionError(f'{label} contains characters that are not valid text.')
    return _CONTROL.sub('', value.replace('\r\n', '\n').replace('\r', '\n')).strip()


def valid_token(token):
    return isinstance(token, str) and TOKEN_SHAPE.fullmatch(token) is not None


# ── Shares ───────────────────────────────────────────────────────────────

def _base_url():
    return settings.APP_BASE_URL.rstrip('/')


def share_url(share):
    """The public link. The token is the URL fragment, which a browser keeps
    to itself: it is in no request line, Referer or server log."""
    return f'{_base_url()}/vote#{share.token}'


def confirmation_url(token):
    return f'{_base_url()}/vote/confirm#{token}'


def email_configured():
    """Whether a confirmation email can reach anybody. Without a real mail
    backend voters are told to check a mailbox nothing will arrive in."""
    return settings.EMAIL_BACKEND not in _MAIL_BACKENDS_THAT_DELIVER_NOTHING


def base_url_configured():
    """Whether the links point somewhere a voter can open."""
    base = _base_url().lower()
    return bool(base) and _LOCAL_BASE.match(base) is None


def is_active(share, now=None):
    now = now or timezone.now()
    return share.revoked_at is None and (share.expires_at is None or share.expires_at > now)


def active_share(token, now=None):
    """The share a public token opens, or None.

    Unknown, malformed, revoked and expired are one answer: the caller cannot
    tell them apart, and neither can the visitor.
    """
    if not valid_token(token):
        return None
    share = MappingVoteShare.objects.filter(token=token).first()
    return share if share is not None and is_active(share, now) else None


def clean_title(value):
    """A share's title: one line, and no link.

    The title is the one piece of curator text in the confirmation email, so
    a link in it would be a link of the writer's choosing sent from this
    deployment's address to every voter.
    """
    text = ' '.join(_plain(value, 'Title').split())
    if not text:
        raise SubmissionError('Title is required.')
    if len(text) > TITLE_MAX_LENGTH:
        raise SubmissionError(f'Title is at most {TITLE_MAX_LENGTH} characters.')
    if _LINK.search(text):
        raise SubmissionError('A title cannot contain a link.')
    return text


def clean_note(value):
    text = _plain(value, 'Note')
    if len(text) > NOTE_MAX_LENGTH:
        raise SubmissionError(f'Note is at most {NOTE_MAX_LENGTH} characters.')
    return text


def parse_expiry(value, now=None):
    """``None``, or the instant a share stops opening, in UTC.

    A bare date means through the end of that day, UTC. The instant must be
    in the future, since a share born expired is a link that never worked,
    and within two years, which also keeps it inside what the database can
    hold whatever offset it was written with.
    """
    if value in (None, ''):
        return None
    if not isinstance(value, str):
        raise SubmissionError('Expiry must be a date.')
    now = now or timezone.now()
    try:
        if len(value) == 10:
            day = datetime.date.fromisoformat(value)
            expires = datetime.datetime.combine(
                day, datetime.time(23, 59, 59), tzinfo=datetime.timezone.utc)
        else:
            expires = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
            if timezone.is_naive(expires):
                raise ValueError('offset required')
            expires = expires.astimezone(datetime.timezone.utc)
    except (ValueError, OverflowError):
        raise SubmissionError('Expiry must be a date such as 2027-01-31.')
    if expires <= now:
        raise SubmissionError('Expiry must be in the future.')
    if expires > now + MAX_SHARE_LIFETIME:
        raise SubmissionError('Expiry must be within two years.')
    return expires


def create_share(user, title, note='', expires_at=None):
    return MappingVoteShare.objects.create(
        token=secrets.token_urlsafe(32),
        title=clean_title(title),
        note=clean_note(note),
        expires_at=parse_expiry(expires_at),
        created_by=user,
    )


def revoke_share(share, now=None):
    """Stop the link opening. Confirmed votes stay; pending ones can no longer
    be confirmed, so they are dropped."""
    if share.revoked_at is None:
        share.revoked_at = now or timezone.now()
        share.save(update_fields=['revoked_at'])
    MappingVoteSubmission.objects.filter(share=share).delete()
    return share


def delete_share(share):
    """Erase a share with everything it gathered: its confirmed votes, the
    voters' addresses on them, and anything still pending.

    This is how a voter's request to be removed is met, and how junk votes
    go. The records of emails already sent stay, without the share: deleting
    a share must not buy a fresh allowance of outgoing mail.
    """
    share.delete()


def shares():
    """Every share, newest first, with how many addresses confirmed a vote."""
    return MappingVoteShare.objects.annotate(
        voters=Count('votes__voter_email', distinct=True),
        vote_count=Count('votes'),
    ).order_by('-created_at', '-id')


def serialize_share(share, *, with_link, now=None):
    """A share for the Share dialog. The link is for those who may hand it
    out; everyone else who reads mappings sees that the share exists."""
    data = {
        'id': share.pk,
        'title': share.title,
        'note': share.note,
        'created_at': share.created_at.isoformat(),
        'expires_at': share.expires_at.isoformat() if share.expires_at else None,
        'revoked_at': share.revoked_at.isoformat() if share.revoked_at else None,
        'active': is_active(share, now),
        'voters': getattr(share, 'voters', 0),
        'votes': getattr(share, 'vote_count', 0),
    }
    if with_link:
        data['url'] = share_url(share)
    return data


# ── The proposals ────────────────────────────────────────────────────────

def votable_mappings():
    """The mappings a share puts to the vote: global wearable proposals that
    name a destination and carry a metric key.

    The metric key is what says somebody described the row. Ingest queues a
    code under a wearable vocabulary from whatever text a producer sent, and
    Suggest can then give it a destination; without this such a row would be
    published to strangers without a curator ever having looked at it.
    """
    return export.tab_mappings().filter(
        status='proposed', organization__isnull=True, target_concept_id__gt=0,
    ).exclude(metric_key='')


def votable_rows(ids=None):
    """Those mappings as export rows, in the export's order. One query.

    A destination whose concept is not loaded here has no code or name to
    show a voter, so it is not put to the vote.
    """
    mappings = votable_mappings()
    if ids is not None:
        mappings = mappings.filter(pk__in=ids)
    return [row for row in export.rows_from(mappings) if row.destination_code]


def _proposal(row):
    """One proposal as the public pages show it. Reference data only."""
    return {
        'id': row.pk,
        'metric_key': row.metric_key,
        'measure_class': row.measure_class,
        'source': row.source,
        'source_code': row.source_code,
        'description': row.description,
        'destination_vocabulary': row.destination_vocabulary,
        'destination_code': row.destination_code,
        'destination_name': row.destination_name,
        'unit': row.destination_unit,
    }


def public_payload(share):
    """What the public voting page shows for *share*. One query."""
    rows = votable_rows()
    return {
        'title': share.title,
        'note': share.note,
        'expires_at': share.expires_at.isoformat() if share.expires_at else None,
        'comment_max_length': COMMENT_MAX_LENGTH,
        'groups': [
            {'category': category, 'proposals': [_proposal(row) for row in members]}
            for category, members in export.sections(rows)
        ],
    }


# ── Submitting ───────────────────────────────────────────────────────────

_BAD_EMAIL = 'Enter a valid email address.'


def normalize_email(value):
    """The address a vote is recorded under and the link is sent to:
    lowercased, and one that mail can be delivered to.

    Beyond Django's validator this refuses what it lets through but no
    confirmation could reach: an IP-literal domain, ``localhost``, a domain of
    one label, and control characters in the local part (legal inside a
    quoted string, and of no use but to confuse whatever reads the address).
    """
    if not isinstance(value, str):
        raise SubmissionError(_BAD_EMAIL)
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise SubmissionError(_BAD_EMAIL)
    email = value.strip().lower()
    if not email or len(email) > EMAIL_MAX_LENGTH or '\n' in email or _CONTROL.search(email):
        raise SubmissionError(_BAD_EMAIL)
    try:
        validate_email(email)
    except ValidationError:
        raise SubmissionError(_BAD_EMAIL)
    domain = email.rsplit('@', 1)[1]
    if domain.startswith('[') or '.' not in domain.strip('.') or domain.endswith('.localhost'):
        raise SubmissionError(_BAD_EMAIL)
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return email
    raise SubmissionError(_BAD_EMAIL)


def canonical_email(email):
    """*email* as the limits count it: without a ``+tag`` on the local part.

    For counting only. ``a+1@x.org`` and ``a+2@x.org`` reach one inbox, so
    they share one allowance of pending submissions and confirmation emails;
    each still votes, and is emailed, under the address as typed.
    """
    local, _, domain = email.rpartition('@')
    return f'{local.split("+", 1)[0] or local}@{domain}'


def clean_comment(value):
    text = _plain(value, 'A comment')
    if len(text) > COMMENT_MAX_LENGTH:
        raise SubmissionError(f'A comment is at most {COMMENT_MAX_LENGTH} characters.')
    return text


def clean_votes(votes):
    """The submitted list as ``[{mapping, concept, choice, comment}]``.

    Raises ``SubmissionError`` for anything that is not a vote on a proposal
    this page offers. An id that is not on the page is refused without saying
    which: the answer must not tell a mapping that exists from one that does
    not. An entry with neither a choice nor a comment says nothing and is
    dropped; a submission of only those is refused.
    """
    if not isinstance(votes, list) or not votes:
        raise SubmissionError('Choose Yes or No, or leave a comment, on at least one proposal.')
    limit = votable_mappings().count()
    if len(votes) > max(limit, 1):
        raise SubmissionError('There are more votes here than proposals on the page.')
    cleaned, seen = [], set()
    for entry in votes:
        if not isinstance(entry, dict):
            raise SubmissionError('Each vote must name a proposal.')
        mapping_id = entry.get('mapping')
        if (isinstance(mapping_id, bool) or not isinstance(mapping_id, int)
                or not 0 < mapping_id < 2 ** 63 or mapping_id in seen):
            raise SubmissionError('Each vote must name a proposal, once.')
        seen.add(mapping_id)
        choice = entry.get('choice') or ''
        if not isinstance(choice, str) or choice not in CHOICES:
            raise SubmissionError('A vote is Yes or No.')
        comment = clean_comment(entry.get('comment'))
        if choice or comment:
            cleaned.append({'mapping': mapping_id, 'choice': choice, 'comment': comment})
    if not cleaned:
        raise SubmissionError('Choose Yes or No, or leave a comment, on at least one proposal.')
    concepts = {row.pk: row.concept_id for row in votable_rows(seen)}
    if seen - set(concepts):
        raise SubmissionError(
            'Some proposals on this page have changed. Reload the page and vote again.')
    for vote in cleaned:
        vote['concept'] = concepts[vote['mapping']]
    return cleaned


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _email_key(email):
    return salted_hmac(_EMAIL_SALT, canonical_email(email), algorithm='sha256').hexdigest()


def _lock(namespace, key=''):
    """Hold a named lock until the transaction ends.

    Advisory locks keyed on two 32-bit hashes: a collision makes two unrelated
    callers wait for each other for a few milliseconds and nothing else.
    """
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)', [
            zlib.crc32(namespace.encode()) - 2 ** 31,
            zlib.crc32(str(key).encode()) - 2 ** 31,
        ])


def purge_expired(now=None):
    """Delete submissions past their life and email records past the limits'
    window. Returns how many submissions went."""
    now = now or timezone.now()
    deleted, _ = MappingVoteSubmission.objects.filter(expires_at__lte=now).delete()
    MappingVoteEmail.objects.filter(sent_at__lte=now - EMAIL_WINDOW).delete()
    return deleted


def submit(share, email, votes, now=None):
    """Hold *votes* from *email* as pending and have the confirmation link
    emailed. Returns whether a submission was stored.

    Two limits concern no particular address, so they are said out loud
    (``Unavailable``): how many confirmation emails the deployment, and this
    share, send in a day. Telling a voter to wait for mail that will not come
    helps nobody. The limit that does concern the address, how many emails it
    is sent in a day, is answered like a success by the caller, since saying
    so would tell a stranger how often an address has been used. They are
    checked in that order for the same reason: an address over its own limit
    must not get a different answer from the one everybody else is getting.

    Every count is taken under three locks, always in one order (the address,
    the share, then one for the deployment), so two submissions at the same
    moment cannot both read "one below the limit". The locks last for this
    transaction only; the email is sent after it commits.

    At the limit on pending submissions for one share and address the oldest
    gives way to the new one. Refusing the new one instead would let anybody
    who knows a voter's address lock that voter out for two days with three
    junk submissions.
    """
    now = now or timezone.now()
    email = normalize_email(email)
    cleaned = clean_votes(votes)
    key = _email_key(email)
    with transaction.atomic():
        _lock('mapping_vote_address', key)
        _lock('mapping_vote_share', share.pk)
        _lock('mapping_vote_deployment')
        purge_expired(now)
        emails = MappingVoteEmail.objects.filter(sent_at__gt=now - EMAIL_WINDOW)
        if (emails.count() >= settings.MAPPING_VOTE_MAX_EMAILS_PER_DAY_TOTAL
                or emails.filter(share=share).count() >= settings.MAPPING_VOTE_MAX_EMAILS_PER_SHARE_PER_DAY):
            logger.warning('Vote submissions on share %s are held back by a mail limit.', share.pk)
            raise Unavailable()
        if emails.filter(email_key=key).count() >= settings.MAPPING_VOTE_MAX_EMAILS_PER_DAY:
            logger.info('A vote submission on share %s was held back by an address limit.', share.pk)
            return False
        pending = list(MappingVoteSubmission.objects.filter(
            share=share, email_key=key,
        ).order_by('created_at', 'id').values_list('pk', flat=True))
        room = max(settings.MAPPING_VOTE_MAX_PENDING_PER_EMAIL - 1, 0)
        if len(pending) > room:
            MappingVoteSubmission.objects.filter(pk__in=pending[:len(pending) - room]).delete()
        submission = MappingVoteSubmission.objects.create(
            share=share, email=email, email_key=key, payload=cleaned,
            created_at=now, expires_at=now + PENDING_LIFETIME,
        )
        MappingVoteEmail.objects.create(email_key=key, share=share, submission=submission, sent_at=now)
    from omop_core.services import mapping_vote_jobs
    mapping_vote_jobs.dispatcher().dispatch(submission.pk)
    return True


# ── The confirmation email ───────────────────────────────────────────────

def _count(number, noun):
    return f'{number} {noun}' if number == 1 else f'{number} {noun}s'


def confirmation_email(share, votes, token):
    """``(subject, body)`` of the confirmation email.

    Fixed wording, the share's title, two numbers and the link. Nothing the
    visitor typed is in it, and neither is the share's note: either would let
    somebody make this deployment send text of their choosing to an address
    of their choosing. The title is one line and holds no link (``clean_title``).
    """
    choices = sum(1 for vote in votes if vote['choice'])
    comments = sum(1 for vote in votes if vote['comment'])
    hours = int(PENDING_LIFETIME.total_seconds() // 3600)
    body = (
        'Hello,\n\n'
        'Someone, hopefully you, used this email address to vote on proposed '
        'wearable metric mappings:\n\n'
        f'  {share.title}\n\n'
        f'The submission holds {_count(choices, "vote")} and {_count(comments, "comment")}. '
        'Nothing has been recorded yet. To review and confirm them, open:\n\n'
        f'  {confirmation_url(token)}\n\n'
        f'The link works once and expires in {hours} hours. If you did not submit '
        'these votes, ignore this email and nothing will be recorded.\n\n'
        '— The HealthKey team'
    )
    return 'Confirm your votes on the wearable mapping proposals', body


def _deliver(share, email, votes, token):
    """Hand the email to the mail backend; return whether it took it.

    Never raises and logs no address: a mail library's error text names the
    recipient, so only the kind of failure is logged. The timeout is this
    message's own. Both keyword names are passed because each backend reads
    the one it knows and ignores the other (``requests_timeout`` is
    anymail's, ``timeout`` is SMTP's), which leaves every other sender's
    connection as it was.
    """
    if not settings.DEBUG and settings.EMAIL_BACKEND in _BACKENDS_THAT_PRINT:
        # A deployed process with no mail service: a worker that was not
        # given the web service's mail variables, most likely. Its backend
        # would write the message, link and address included, into the
        # process log and deliver it to nobody.
        logger.error(
            'A vote confirmation email for share %s was not sent: this process has no mail '
            'backend (EMAIL_BACKEND is %s). Give it the same mail settings as the web service.',
            share.pk, settings.EMAIL_BACKEND)
        return False
    subject, body = confirmation_email(share, votes, token)
    seconds = settings.MAPPING_VOTE_EMAIL_TIMEOUT
    try:
        backend = get_connection(requests_timeout=seconds, timeout=seconds)
        message = EmailMessage(subject, body, settings.DEFAULT_FROM_EMAIL, [email], connection=backend)
        return message.send() == 1
    except Exception as error:  # noqa: BLE001 - whatever it is, the voter gets no link
        logger.error(
            'A vote confirmation email for share %s could not be sent (%s).',
            share.pk, type(error).__name__)
        return False


def send_confirmation(submission_id):
    """Make the confirmation token for a pending submission and email it.
    Returns whether the email went.

    The token is made here, where the email is written, and never before: it
    is in no task argument, broker message or request. What is stored is its
    hash, committed before the send so the link works the moment it arrives.

    A submission that already has a hash was handled by an earlier delivery
    of the same job, so it is left alone: a job that is delivered twice (a
    worker lost between sending and acknowledging) must not mail a stranger
    twice. The price is that a worker lost between the commit and the send
    leaves a submission nobody can confirm, until it expires.

    When the backend refuses the message the submission is deleted, since no
    link exists to confirm it, and so is its email record: an email that was
    never sent is not counted against anyone's limit.
    """
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        submission = MappingVoteSubmission.objects.select_for_update(of=('self',)).select_related(
            'share').filter(pk=submission_id).first()
        if submission is None or submission.token_hash:
            return False
        submission.token_hash = _token_hash(token)
        submission.save(update_fields=['token_hash'])
    if _deliver(submission.share, submission.email, _payload_votes(submission), token):
        return True
    MappingVoteEmail.objects.filter(submission_id=submission_id).delete()
    MappingVoteSubmission.objects.filter(pk=submission_id).delete()
    return False


# ── Confirming ───────────────────────────────────────────────────────────

def _pending(token, now, *, lock=False):
    """The live submission a confirmation token names, or None.

    Unknown, malformed, used, expired, and a share since revoked or expired
    are one answer. Only the token's SHA-256 is stored, so it is found by its
    hash and that hash is compared in constant time.
    """
    if not valid_token(token):
        return None
    digest = _token_hash(token)
    submissions = MappingVoteSubmission.objects.select_related('share')
    if lock:
        submissions = submissions.select_for_update(of=('self',))
    submission = submissions.filter(token_hash=digest).first()
    if submission is None or not hmac.compare_digest(submission.token_hash or '', digest):
        return None
    if submission.expires_at <= now or not is_active(submission.share, now):
        return None
    return submission


def _payload_votes(submission):
    return [vote for vote in submission.payload if isinstance(vote, dict)]


def pending_summary(token, now=None):
    """What the confirmation page shows before the voter confirms, or None.

    Reading it changes nothing: whatever follows the emailed link, or calls
    this, confirms no vote.
    """
    now = now or timezone.now()
    submission = _pending(token, now)
    if submission is None:
        return None
    votes = _payload_votes(submission)
    rows = {row.pk: row for row in votable_rows([vote['mapping'] for vote in votes])}
    items = []
    for vote in votes:
        row = rows.get(vote['mapping'])
        open_now = row is not None and row.concept_id == vote['concept']
        items.append({
            'proposal': _proposal(row) if open_now else None,
            'choice': vote['choice'],
            'comment': vote['comment'],
        })
    # The export's order for the proposals still open; the closed ones last.
    order = {pk: index for index, pk in enumerate(row.pk for row in sorted(
        rows.values(), key=lambda row: row.sort_key))}
    items.sort(key=lambda item: (
        item['proposal'] is None,
        order.get(item['proposal']['id'], 0) if item['proposal'] else 0,
    ))
    return {
        'title': submission.share.title,
        'email': submission.email,
        'expires_at': submission.expires_at.isoformat(),
        'votes': items,
    }


def confirm(token, now=None):
    """Record the votes a confirmation token holds. ``None`` for a token that
    is not live; otherwise how many were recorded and how many were skipped.

    One transaction: the submission is locked, its votes written and the
    submission deleted, so the token works once even when two requests carry
    it at the same moment. A proposal that was approved, rejected, deleted or
    moved to another destination since the visitor saw it is skipped: the
    vote was for something that is no longer on the table.

    Two submissions from one voter can name the same proposals, and
    confirming both at once used to deadlock, each holding a vote row the
    other wanted. So a voter's confirmations on a share take turns (a lock on
    the share and address, taken before any row), and the votes are written
    as one statement in mapping order.

    The cost does not grow with the number of votes: one read of the
    proposals, one upsert, one delete.
    """
    now = now or timezone.now()
    if not valid_token(token):
        return None
    try:
        return _confirm(token, now)
    except IntegrityError:
        # A mapping was deleted between the read and the write. Nothing was
        # recorded; a second pass no longer finds it and skips it.
        return _confirm(token, now)


def _confirm(token, now):
    with transaction.atomic():
        voter = MappingVoteSubmission.objects.filter(
            token_hash=_token_hash(token)).values_list('share_id', 'email').first()
        if voter is None:
            return None
        _lock('mapping_vote_voter', f'{voter[0]}:{voter[1]}')
        submission = _pending(token, now, lock=True)
        if submission is None:
            return None
        votes = sorted(_payload_votes(submission), key=lambda vote: vote['mapping'])
        concepts = {row.pk: row.concept_id for row in votable_rows([vote['mapping'] for vote in votes])}
        current = [vote for vote in votes if concepts.get(vote['mapping']) == vote['concept']]
        MappingVote.objects.bulk_create(
            [MappingVote(
                share=submission.share, voter_email=submission.email, mapping_id=vote['mapping'],
                choice=vote['choice'], comment=vote['comment'], concept_id=vote['concept'],
                created_at=submission.created_at, confirmed_at=now,
            ) for vote in current],
            update_conflicts=True, unique_fields=['share', 'voter_email', 'mapping'],
            update_fields=['choice', 'comment', 'concept_id', 'created_at', 'confirmed_at'],
        )
        submission.delete()
    return {'recorded': len(current), 'skipped': len(votes) - len(current)}


# ── What curators read ───────────────────────────────────────────────────

def with_vote_counts(mappings):
    """*mappings* annotated with the current tally of each row.

    ``vote_yes``, ``vote_no`` and ``vote_comments`` count confirmed votes cast
    on the destination the row has now; a vote on an earlier destination is
    stale and counts nowhere. Subqueries on the row's own query, so a page of
    any size costs no further query.
    """
    def tally(condition):
        votes = MappingVote.objects.filter(
            condition, mapping=OuterRef('pk'), concept_id=OuterRef('target_concept_id'),
        ).order_by().values('mapping').annotate(n=Count('pk')).values('n')
        return Coalesce(Subquery(votes, output_field=IntegerField()), 0)

    return mappings.annotate(
        vote_yes=tally(Q(choice=MappingVote.YES)),
        vote_no=tally(Q(choice=MappingVote.NO)),
        vote_comments=tally(~Q(comment='')),
    )


def row_tally(mapping):
    """The tally ``with_vote_counts`` put on *mapping*, or None for a row that
    was loaded without it."""
    if not hasattr(mapping, 'vote_yes'):
        return None
    return {'yes': mapping.vote_yes, 'no': mapping.vote_no, 'comments': mapping.vote_comments}


def mapping_votes(mapping, *, include_emails):
    """Every confirmed vote on *mapping*, newest first, and the current tally.

    An email address is personal data: it is in the answer only for a reader
    who may write mappings.
    """
    votes = []
    tally = {'yes': 0, 'no': 0, 'comments': 0}
    for vote in MappingVote.objects.filter(mapping=mapping).select_related('share').order_by(
            '-confirmed_at', '-id'):
        stale = vote.concept_id != mapping.target_concept_id
        if not stale:
            if vote.choice:
                tally[vote.choice] += 1
            if vote.comment:
                tally['comments'] += 1
        entry = {
            'id': vote.pk,
            'choice': vote.choice,
            'comment': vote.comment,
            'confirmed_at': vote.confirmed_at.isoformat(),
            'share_id': vote.share_id,
            'share_title': vote.share.title,
            'stale': stale,
        }
        if include_emails:
            entry['voter_email'] = vote.voter_email
        votes.append(entry)
    return {'tally': tally, 'emails_included': include_emails, 'votes': votes}


def share_votes_csv(share):
    """Every confirmed vote of *share* as CSV text, on the export's
    conventions: a byte order mark, ``\\n`` line ends, and text a spreadsheet
    would run as a formula quoted. A comment and an address are whatever a
    stranger typed."""
    votes = list(MappingVote.objects.filter(share=share).select_related('mapping').order_by('id'))
    rows = {row.pk: row for row in export.rows_from(
        SourceCodeConceptMapping.objects.filter(pk__in={vote.mapping_id for vote in votes}))}
    from omop_core.models import Concept
    voted_on = {
        concept.concept_id: concept for concept in Concept.objects.filter(
            concept_id__in={vote.concept_id for vote in votes})
    }
    votes.sort(key=lambda vote: (rows[vote.mapping_id].sort_key, vote.voter_email, vote.pk))
    buffer = io.StringIO()
    buffer.write('\ufeff')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(CSV_COLUMNS)
    for vote in votes:
        row = rows[vote.mapping_id]
        concept = voted_on.get(vote.concept_id)
        writer.writerow([csv_cell(value) for value in (
            share.title, row.category, row.metric_key, row.source, row.source_code,
            row.description,
            f'{concept.vocabulary_id}:{concept.concept_code}' if concept else '',
            concept.concept_name if concept else '',
            vote.concept_id, vote.choice, vote.comment, vote.voter_email,
            vote.confirmed_at.isoformat(timespec='seconds'),
            'yes' if vote.concept_id != vote.mapping.target_concept_id else 'no',
        )])
    return buffer.getvalue()

