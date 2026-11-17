"""Public voting on the wearable mapping proposals (#2678).

A curator shares a link. Whoever holds it reads the proposals without an
account, marks Yes, No or a comment on any of them, and gives an email
address. That submission is not a vote: it is stored as pending and a
confirmation link is emailed. Only the POST behind that link writes vote
rows, so an address somebody else typed never votes.

A vote never changes a mapping. Curators read the tallies and approve or
reject as before.

Everything a voter can reach here is reference data: the proposals are the
global wearable mappings with status ``proposed`` and a loaded destination
concept, read live. No patient data is reachable from a share.
"""
import csv
import datetime
import hashlib
import hmac
import io
import logging
import re
import secrets
import zlib

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import connection, transaction
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
#: The window of the per-address limit on confirmation emails.
EMAIL_WINDOW = datetime.timedelta(hours=24)
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
CSV_COLUMNS = (
    'Share', 'Category', 'Metric key', 'Source', 'Source code', 'Description',
    'Voted on destination', 'Voted on concept', 'OMOP concept ID', 'Vote', 'Comment',
    'Voter email', 'Confirmed at', 'Stale',
)

_EMAIL_SALT = 'omop_core.mapping_votes.email'
# C0 and C1 controls other than the line feed, and the bidirectional
# overrides and isolates that can reorder how a line reads.
_CONTROL = re.compile(r'[\x00-\x09\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]')


class SubmissionError(ValueError):
    """The submission is refused; the message is safe to show the visitor."""


# ── Shares ───────────────────────────────────────────────────────────────

def share_url(share):
    return f'{settings.APP_BASE_URL.rstrip("/")}/vote/{share.token}'


def confirmation_url(token):
    return f'{settings.APP_BASE_URL.rstrip("/")}/vote/confirm/{token}'


def is_active(share, now=None):
    now = now or timezone.now()
    return share.revoked_at is None and (share.expires_at is None or share.expires_at > now)


def active_share(token, now=None):
    """The share a public token opens, or None.

    Unknown, revoked and expired are one answer: the caller cannot tell them
    apart, and neither can the visitor.
    """
    if not isinstance(token, str) or not token or len(token) > 64:
        return None
    share = MappingVoteShare.objects.filter(token=token).first()
    return share if share is not None and is_active(share, now) else None


def clean_text(value, limit, label, *, required=False):
    """Curator text for a share: stripped, without control characters."""
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise SubmissionError(f'{label} must be text.')
    text = _CONTROL.sub('', value.replace('\r\n', '\n').replace('\r', '\n')).strip()
    if required and not text:
        raise SubmissionError(f'{label} is required.')
    if len(text) > limit:
        raise SubmissionError(f'{label} is at most {limit} characters.')
    return text


def parse_expiry(value, now=None):
    """``None``, or the instant a share stops opening.

    A bare date means through the end of that day, UTC. The instant must be
    in the future: a share that is born expired is a link that never worked.
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
    except ValueError:
        raise SubmissionError('Expiry must be a date such as 2027-01-31.')
    if expires <= now:
        raise SubmissionError('Expiry must be in the future.')
    return expires


def create_share(user, title, note='', expires_at=None):
    return MappingVoteShare.objects.create(
        token=secrets.token_urlsafe(32),
        title=clean_text(title, TITLE_MAX_LENGTH, 'Title', required=True).replace('\n', ' '),
        note=clean_text(note, NOTE_MAX_LENGTH, 'Note'),
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


def shares():
    """Every share, newest first, with how many addresses confirmed a vote."""
    return MappingVoteShare.objects.annotate(
        voters=Count('votes__voter_email', distinct=True),
        vote_count=Count('votes'),
    ).order_by('-created_at', '-id')


def serialize_share(share, *, with_link, now=None):
    """A share for the curator's dialog. The link is for those who may hand
    it out; a read-only reader sees that the share exists."""
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
    name a destination."""
    return export.tab_mappings().filter(
        status='proposed', organization__isnull=True, target_concept_id__gt=0,
    )


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

def normalize_email(value):
    if not isinstance(value, str):
        raise SubmissionError('Enter a valid email address.')
    email = value.strip().lower()
    if not email or len(email) > EMAIL_MAX_LENGTH:
        raise SubmissionError('Enter a valid email address.')
    try:
        validate_email(email)
    except ValidationError:
        raise SubmissionError('Enter a valid email address.')
    return email


def clean_comment(value):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise SubmissionError('A comment must be text.')
    text = _CONTROL.sub('', value.replace('\r\n', '\n').replace('\r', '\n')).strip()
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
        if choice not in CHOICES:
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
    return salted_hmac(_EMAIL_SALT, email, algorithm='sha256').hexdigest()


def _lock_email(email):
    """Serialize submissions from one address for the rest of the transaction,
    so two requests at once cannot both pass the limits."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)', [
            zlib.crc32(b'mapping_vote_email') - 2 ** 31,
            zlib.crc32(email.encode()) - 2 ** 31,
        ])


def purge_expired(now=None):
    """Delete submissions past their life and email records past the limit's
    window. Returns how many submissions went."""
    now = now or timezone.now()
    deleted, _ = MappingVoteSubmission.objects.filter(expires_at__lte=now).delete()
    MappingVoteEmail.objects.filter(sent_at__lte=now - EMAIL_WINDOW).delete()
    return deleted


def submit(share, email, votes, now=None):
    """Hold *votes* from *email* as pending and email the confirmation link.

    Returns whether an email was sent. The caller answers the visitor the
    same either way: over a limit nothing is stored and nothing is sent, and
    saying so would tell a stranger how often an address has been used.
    """
    now = now or timezone.now()
    email = normalize_email(email)
    cleaned = clean_votes(votes)
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        _lock_email(email)
        purge_expired(now)
        pending = MappingVoteSubmission.objects.filter(share=share, email=email).count()
        key = _email_key(email)
        sent = MappingVoteEmail.objects.filter(email_key=key, sent_at__gt=now - EMAIL_WINDOW).count()
        # Every address together: the per-address limits do nothing against
        # a sender who types a new address each time, and the per-client
        # throttle is only as good as the client address the proxy reports.
        total = MappingVoteEmail.objects.filter(sent_at__gt=now - EMAIL_WINDOW).count()
        if (pending >= settings.MAPPING_VOTE_MAX_PENDING_PER_EMAIL
                or sent >= settings.MAPPING_VOTE_MAX_EMAILS_PER_DAY
                or total >= settings.MAPPING_VOTE_MAX_EMAILS_PER_DAY_TOTAL):
            logger.info('Vote submission on share %s held back by a limit.', share.pk)
            return False
        submission = MappingVoteSubmission.objects.create(
            share=share, email=email, payload=cleaned, token_hash=_token_hash(token),
            created_at=now, expires_at=now + PENDING_LIFETIME,
        )
        # Counted when attempted: a send that fails still spent the address's turn.
        MappingVoteEmail.objects.create(email_key=key, sent_at=now)
    if _send_confirmation(share, email, cleaned, token):
        return True
    # The link never left, so nothing can confirm this row.
    submission.delete()
    return False


def _count(number, noun):
    return f'{number} {noun}' if number == 1 else f'{number} {noun}s'


def confirmation_email(share, votes, token):
    """``(subject, body)`` of the confirmation email.

    Fixed wording, the curator's title, two numbers and the link. Nothing the
    visitor typed is in it: a comment here would let anyone make this
    deployment send their text to an address of their choosing.
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


def _send_confirmation(share, email, votes, token):
    """Send the link; return whether the backend accepted it. Never raises."""
    subject, body = confirmation_email(share, votes, token)
    # No preview in the log, unlike the account emails: the body carries the
    # token, and a developer machine's console mail backend prints it anyway.
    try:
        return send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email]) == 1
    except Exception:  # noqa: BLE001 - the visitor gets the same answer either way
        logger.exception('Failed to send a vote confirmation email for share %s', share.pk)
        return False


# ── Confirming ───────────────────────────────────────────────────────────

def _pending(token, now, *, lock=False):
    """The live submission a confirmation token names, or None.

    Unknown, used, expired, and a share since revoked or expired are one
    answer. Only the token's SHA-256 is stored, so it is found by its hash
    and that hash is compared in constant time.
    """
    if not isinstance(token, str) or not token or len(token) > 128:
        return None
    digest = _token_hash(token)
    submissions = MappingVoteSubmission.objects.select_related('share')
    if lock:
        submissions = submissions.select_for_update(of=('self',))
    submission = submissions.filter(token_hash=digest).first()
    if submission is None or not hmac.compare_digest(submission.token_hash, digest):
        return None
    if submission.expires_at <= now or not is_active(submission.share, now):
        return None
    return submission


def _payload_votes(submission):
    return [vote for vote in submission.payload if isinstance(vote, dict)]


def pending_summary(token, now=None):
    """What the confirmation page shows before the voter confirms, or None.

    Reading it changes nothing: a mail scanner that follows the link confirms
    no vote.
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
    """
    now = now or timezone.now()
    with transaction.atomic():
        submission = _pending(token, now, lock=True)
        if submission is None:
            return None
        votes = _payload_votes(submission)
        concepts = {row.pk: row.concept_id for row in votable_rows([vote['mapping'] for vote in votes])}
        recorded = skipped = 0
        for vote in votes:
            if concepts.get(vote['mapping']) != vote['concept']:
                skipped += 1
                continue
            MappingVote.objects.update_or_create(
                share=submission.share, voter_email=submission.email, mapping_id=vote['mapping'],
                defaults={
                    'choice': vote['choice'], 'comment': vote['comment'],
                    'concept_id': vote['concept'],
                    'created_at': submission.created_at, 'confirmed_at': now,
                },
            )
            recorded += 1
        submission.delete()
    return {'recorded': recorded, 'skipped': skipped}


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

