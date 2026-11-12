"""Guards shared by the opt-in audit/repair command pairs (#2490).

``audit_pre_existing_conditions`` / ``repair_pre_existing_conditions`` (#1988) and
``audit_legacy_pregnancy_tobacco`` / ``repair_legacy_pregnancy_tobacco`` (#2432)
carried identical copies of these helpers. The write target guard is
safety-critical, so it lives here once: a fix to it reaches every command.

This module is not under ``commands/``, so Django does not expose it as one.
"""
import hashlib
import ipaddress
import json
import os
from datetime import datetime
from urllib.parse import unquote, urlsplit

from django.core.management.base import CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection

_TRUE_TEXT = {'true', 't', '1', 'yes', 'y', 'on'}
_FALSE_TEXT = {'false', 'f', '0', 'no', 'n', 'off'}

LOOPBACK_HOSTS = ('localhost', '127.0.0.1', '::1')


# --- audit side -------------------------------------------------------------

def as_bool(text):
    """Interpret a ``RecordRevision`` textual value, or None when it is blank."""
    if text is None:
        return None
    normalized = str(text).strip().casefold()
    if normalized in _TRUE_TEXT:
        return True
    if normalized in _FALSE_TEXT:
        return False
    return None


def approval_digest(payload):
    """Authenticate the reviewable document, not just its underlying data state."""
    stable = {key: value for key, value in payload.items()
              if key not in ('generated_at', 'approval_sha256')}
    return hashlib.sha256(json.dumps(
        stable, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def make_transaction_read_only():
    """Ask Postgres to reject writes for this transaction, as a second guard.

    Inside a caller's transaction (pytest, tests) ``SET TRANSACTION READ ONLY``
    is illegal because statements already ran, so callers invoke this only when
    they own the transaction; the guard is best-effort by design.
    """
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')


def refuse_existing_manifest(path):
    """Fail early, before the audit reads anything, when ``path`` already exists."""
    if path and os.path.exists(path):
        raise CommandError(
            f'{path} already exists; refusing to overwrite an audit '
            'manifest (choose a new path)')


def write_manifest(path, payload):
    """Write an audit manifest owner-only (0600), never over an existing file.

    The manifest holds person ids and their answers, like the repair command's
    backup. O_EXCL also closes the race left by ``refuse_existing_manifest``.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise CommandError(
            f'{path} already exists; refusing to overwrite an audit '
            'manifest.') from exc
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write('\n')


# --- repair side ------------------------------------------------------------

class BackupEncoder(DjangoJSONEncoder):
    def default(self, value):
        # DjangoJSONEncoder truncates datetimes to milliseconds. Repair backups
        # must retain the original timestamps, including microseconds.
        if isinstance(value, datetime):
            return value.isoformat()
        return super().default(value)


def canonical(value):
    return json.dumps(value, cls=BackupEncoder, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_json(path):
    try:
        with open(path, encoding='utf-8') as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError('Expected an object')
        return data
    except (OSError, ValueError) as exc:
        raise CommandError('Cannot read the manifest/backup as a JSON object.') from exc


def write_private(path, payload):
    # Exclusive create, restrictive permissions, fsync BEFORE committing DB data.
    # A leftover file after a failed transaction is not evidence of a commit: the
    # rollback additionally verifies the durable DB ledger for every entry.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(canonical(payload) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        parent_fd = os.open(os.path.dirname(os.path.abspath(path)),
                            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise CommandError('Cannot exclusively create and persist the private backup.') from exc


def _identity(url):
    parts = urlsplit(url)
    return (parts.hostname or '', str(parts.port or 5432), unquote(parts.path.lstrip('/')))


def target_guard(environment):
    """Refuse an apply unless the connection is an explicit local or staging target.

    Production (``PROD_DATABASE_URL``) is refused first, whatever ``environment``
    says. ``local`` needs a loopback host *and* a verified loopback peer;
    ``staging`` needs an exact match with ``STAGING_DATABASE_URL``.
    """
    cfg = connection.settings_dict
    target = (str(cfg.get('HOST') or ''), str(cfg.get('PORT') or '5432'), str(cfg['NAME']))

    prod = os.environ.get('PROD_DATABASE_URL')
    if prod and _identity(prod) == target:
        raise CommandError('Production writes are forbidden.')
    if (not target[0] or cfg.get('OPTIONS', {}).get('service') or os.environ.get('PGSERVICE')):
        raise CommandError('Apply requires explicit HOST configuration without connection services.')
    if cfg.get('OPTIONS', {}).get('hostaddr') or os.environ.get('PGHOSTADDR'):
        raise CommandError('Host-address overrides are forbidden for apply.')
    connection.ensure_connection()
    info = connection.connection.info
    effective = (info.host, str(info.port), info.dbname)
    if effective != target:
        raise CommandError('Effective connection does not match the configured target.')
    if environment == 'local':
        if target[0] not in LOOPBACK_HOSTS:
            raise CommandError('Local apply requires an explicitly configured loopback database.')
        try:
            local_peer = ipaddress.ip_address(info.hostaddr).is_loopback
        except (AttributeError, ValueError):
            local_peer = False
        if not local_peer:
            raise CommandError('Local apply requires a verified loopback peer address.')
    else:
        staging = os.environ.get('STAGING_DATABASE_URL')
        if not staging or _identity(staging) != target:
            raise CommandError('Staging target must exactly match STAGING_DATABASE_URL.')


LOCK_BATCH = 500


def lock_rows(model, pks):
    """Lock exactly the rows a repair may write, for the rest of the transaction.

    The repairs used to take ``SHARE ROW EXCLUSIVE`` on the whole
    ``patient_record`` table, which blocked every patient write for the entire
    per-row loop (#2450 review). Locking only the rows the audit selected keeps
    the all-or-nothing guarantee for them and leaves the rest of the table
    writable. Ids are sorted and batched so two overlapping runs cannot deadlock.
    """
    if connection.vendor != 'postgresql':
        raise CommandError('Apply requires PostgreSQL.')
    ids = sorted({int(pk) for pk in pks})
    if not ids:
        return
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '5s'")
    for start in range(0, len(ids), LOCK_BATCH):
        batch = ids[start:start + LOCK_BATCH]
        # select_for_update locks on read; the values are not needed.
        list(model.objects.select_for_update().filter(pk__in=batch)
             .order_by('pk').values_list('pk', flat=True))


def receipt(operation, run, database, expected, entries):
    payload = {'receipt_version': 1, 'operation': operation, 'run_id': run,
               'database': database, 'input_sha256': expected, 'entries': entries}
    payload['backup_sha256'] = digest(payload)
    return payload
