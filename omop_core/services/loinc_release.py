"""Fetch LOINC from loinc.org, and answer whether our copy is out of date.

LOINC class metadata used to arrive by hand: someone downloaded a release zip,
unzipped `Loinc.csv`, paired it with a `LoincClass.csv` from a sibling
`hk-labs` checkout, copied both into a GCS bucket and ran a Cloud Run job. It
was in no deployment path, so a fresh instance got nothing, and the last run
predated migration 0249 -- leaving `property` and `scale_type` empty on 112,371
rows with nothing able to say so. See #1624.

Both files come from one archive, which is the thing that makes an API-only
source possible:

* ``LoincTable/Loinc.csv`` -- ``LoincCodeClass``: class, example units,
  property, scale type.
* ``AccessoryFiles/PartFile/Part.csv`` filtered to ``PartTypeName == 'CLASS'``
  -- ``LoincClass``. LOINC models CLASS as a Part, which is why the archive has
  no file named after classes. Verified on 2.83: 476 CLASS parts covering all
  440 distinct CLASS values used by the 112,405 codes, so nothing is skipped.

The version check is cheap and the download is not -- ``GET /Loinc`` is a small
JSON document, the archive is ~92MB, and the download endpoint appears rate
limited (a repeat request minutes later answered 401 with credentials that
still worked on ``GET /Loinc``). So the version is always checked first, the
archive is fetched only on a mismatch, and a failed fetch leaves a usable
existing release alone rather than failing whatever asked.
"""
import logging

import requests

from django.conf import settings

logger = logging.getLogger(__name__)

API_BASE = 'https://loinc.regenstrief.org/api/v1'
VERSION_URL = f'{API_BASE}/Loinc'

#: Members we read, by their path inside the release archive.
LOINC_TABLE_MEMBER = 'LoincTable/Loinc.csv'
PART_FILE_MEMBER = 'AccessoryFiles/PartFile/Part.csv'

#: `GET /Loinc` is small; the archive is ~92MB over a rate-limited endpoint.
VERSION_TIMEOUT = 30
DOWNLOAD_TIMEOUT = (30, 600)

#: Advisory-lock key for the load. Arbitrary but fixed; scoped to this loader.
_LOCK_KEY = 0x10C1_0AD1


class LoincCredentialsMissing(RuntimeError):
    """No LOINC_USER / LOINC_PASSWORD configured."""


class LoincReleaseUnavailable(RuntimeError):
    """The release API could not be reached or refused us."""


def credentials():
    """Return ``(user, password)``, or raise if either is absent.

    LOINC is licensed, so there is no anonymous fallback to degrade to. The
    distinction that matters to a caller is "not configured" against "the API
    said no" -- the first is a deployment that was never finished, the second
    can be transient.
    """
    user = getattr(settings, 'LOINC_USER', '') or ''
    password = getattr(settings, 'LOINC_PASSWORD', '') or ''
    if not user or not password:
        raise LoincCredentialsMissing(
            'LOINC_USER and LOINC_PASSWORD must be set to reach the LOINC '
            'release API. LOINC is licensed; there is no anonymous access.'
        )
    return user, password


def current_release():
    """The release loinc.org is publishing right now.

    Returns the API's own payload, which already carries the download URL and
    an MD5 -- so nothing here constructs a URL or guesses a filename.
    """
    try:
        response = requests.get(VERSION_URL, auth=credentials(), timeout=VERSION_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except LoincCredentialsMissing:
        raise
    except requests.RequestException as exc:
        raise LoincReleaseUnavailable(f'Could not reach {VERSION_URL}: {exc}') from exc
    except ValueError as exc:
        raise LoincReleaseUnavailable(f'{VERSION_URL} did not return JSON: {exc}') from exc

    version = (payload.get('version') or '').strip()
    download_url = (payload.get('downloadUrl') or '').strip()
    if not version or not download_url:
        raise LoincReleaseUnavailable(
            f'{VERSION_URL} returned no version or downloadUrl: {payload!r}'
        )
    return {
        'version': version,
        'download_url': download_url,
        'md5': (payload.get('downloadMD5Hash') or '').strip(),
        'release_date': (payload.get('releaseDate') or '').strip()[:10] or None,
        'loinc_count': payload.get('numberOfLoincs'),
    }


def loaded_version():
    """The release our tables were built from, or None if never loaded."""
    from omop_core.models import LoincRelease

    row = LoincRelease.objects.order_by('-loaded_at').first()
    return row.release_version if row else None


def is_out_of_date():
    """``(stale, published)`` -- whether a load is needed, and what is current.

    Raises rather than guessing when the API cannot answer: a caller deciding
    whether to spend a 92MB download needs to know the difference between "up
    to date" and "could not tell".
    """
    published = current_release()
    return published['version'] != loaded_version(), published


def stream_archive(download_url):
    """Yield chunks of the release archive.

    Streamed rather than saved: the archive is ~92MB and only two of its
    members are wanted, so `stream_unzip` reads it as it arrives and nothing
    lands on disk.
    """
    try:
        response = requests.get(
            download_url, auth=credentials(), stream=True, timeout=DOWNLOAD_TIMEOUT)
        response.raise_for_status()
    except LoincCredentialsMissing:
        raise
    except requests.RequestException as exc:
        raise LoincReleaseUnavailable(f'Could not download {download_url}: {exc}') from exc
    return response.iter_content(chunk_size=1024 * 1024)


def load_from_archive(chunks, stdout=None):
    """Load both LOINC tables from one streamed archive.

    Returns ``(class_count, code_count)``.

    Order is not assumed. ``Part.csv`` lives under ``AccessoryFiles/`` and
    ``Loinc.csv`` under ``LoincTable/``, and ``stream_unzip`` yields members in
    whatever order the archive stores them -- but ``LoincCodeClass`` rows are
    dropped when their class is unknown, so classes have to be in place first.
    Both are buffered, which is the cost of not assuming order: the 476 class
    rows are trivial, the 112k code rows are roughly 120MB of dicts and model
    instances at peak. A real archive happens to store ``AccessoryFiles/``
    before ``LoincTable/``, so the code rows could stream once classes are
    known -- but relying on that makes the loader silently wrong on an archive
    laid out the other way, which is the case ``test_classes_load_even_when_
    the_part_file_comes_last`` covers.
    """
    from stream_unzip import stream_unzip

    from omop_core.models import LoincClass, LoincCodeClass

    def say(message, level=logging.INFO):
        # Also logged: the Celery task passes no stdout, and that is the only
        # automated path. A load that leaves no record is the failure this
        # whole change exists to stop.
        logger.log(level, message.strip())
        if stdout is not None:
            stdout.write(message)

    classes, code_rows = None, None
    for name, _size, member_chunks in stream_unzip(chunks):
        member = name.decode('utf-8', 'replace')
        if member.endswith(PART_FILE_MEMBER) and classes is None:
            classes = _read_class_parts(member_chunks)
            say(f'  Read {len(classes)} CLASS parts from {PART_FILE_MEMBER}.')
        elif member.endswith(LOINC_TABLE_MEMBER) and code_rows is None:
            # Buffered rather than streamed into the database, because the
            # classes they depend on may still be later in the archive.
            code_rows = list(_read_loinc_rows(member_chunks))
            say(f'  Read {len(code_rows)} codes from {LOINC_TABLE_MEMBER}.')
        else:
            for _ in member_chunks:
                pass
        if classes is not None and code_rows is not None:
            break

    if classes is None:
        raise LoincReleaseUnavailable(f'Release archive has no {PART_FILE_MEMBER}')
    if code_rows is None:
        raise LoincReleaseUnavailable(f'Release archive has no {LOINC_TABLE_MEMBER}')

    LoincClass.objects.bulk_create(
        [LoincClass(code=code, display_name=name) for code, name in classes.items()],
        update_conflicts=True, unique_fields=['code'], update_fields=['display_name'],
        batch_size=2000,
    )
    # A code whose class is unknown would be dropped, so this must not happen
    # -- on 2.83 all 440 classes in use are present. Reported rather than
    # silently skipped, because silence is how the stale load went unnoticed.
    unknown = {row['loinc_class_id'] for row in code_rows} - set(classes)
    if unknown:
        dropped = sum(1 for row in code_rows if row['loinc_class_id'] in unknown)
        say(f'  WARNING: {dropped} codes reference {len(unknown)} classes absent '
            f'from {PART_FILE_MEMBER}: {sorted(unknown)[:5]}', logging.WARNING)
        code_rows = [row for row in code_rows if row['loinc_class_id'] not in unknown]

    LoincCodeClass.objects.bulk_create(
        [LoincCodeClass(**row) for row in code_rows],
        update_conflicts=True, unique_fields=['loinc_num'],
        update_fields=['loinc_class_id', 'example_units', 'property', 'scale_type'],
        batch_size=2000,
    )
    return len(classes), len(code_rows)


def _decoded_csv(member_chunks):
    """Yield decoded text lines from a streamed zip member."""
    import codecs
    import csv as csv_module
    import io
    import sys

    csv_module.field_size_limit(sys.maxsize)
    decoder = codecs.getincrementaldecoder('utf-8-sig')(errors='replace')
    buffer = ''
    for chunk in member_chunks:
        buffer += decoder.decode(chunk)
        lines = buffer.split('\n')
        buffer = lines.pop()
        for line in lines:
            yield line + '\n'
    tail = buffer + decoder.decode(b'', True)
    if tail:
        yield tail


def _read_class_parts(member_chunks):
    """``{CLASS code: display name}`` from the Part file.

    LOINC models CLASS as a Part, so the class table is ``Part.csv`` filtered
    to ``PartTypeName == 'CLASS'``. Non-ACTIVE parts are kept: a retired class
    can still be referenced by existing codes, and dropping it would take those
    codes with it.
    """
    import csv

    classes = {}
    for row in csv.DictReader(_decoded_csv(member_chunks)):
        if (row.get('PartTypeName') or '').strip() != 'CLASS':
            continue
        code = (row.get('PartName') or '').strip()
        display = (row.get('PartDisplayName') or '').strip()
        if code:
            classes[code] = display or code
    return classes


def _read_loinc_rows(member_chunks):
    """``LoincCodeClass`` field dicts from ``Loinc.csv``."""
    import csv

    for row in csv.DictReader(_decoded_csv(member_chunks)):
        loinc_num = (row.get('LOINC_NUM') or '').strip()
        loinc_class = (row.get('CLASS') or '').strip()
        if not loinc_num or not loinc_class:
            continue
        yield {
            'loinc_num': loinc_num,
            'loinc_class_id': loinc_class,
            'example_units': (row.get('EXAMPLE_UNITS') or '').strip(),
            'property': (row.get('PROPERTY') or '').strip(),
            'scale_type': (row.get('SCALE_TYP') or '').strip(),
        }


def _try_lock():
    """Take the loader lock, or report that someone else holds it.

    Session-level and re-entrant within one connection, which is why this is
    a guard against other *processes* rather than against re-entry here.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s)', [_LOCK_KEY])
        return bool(cursor.fetchone()[0])


def _unlock():
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_unlock(%s)', [_LOCK_KEY])


def sync_release(force=False, stdout=None):
    """Load the current release if ours is stale. Returns the version, or None.

    This is the whole job: check, and only then spend the download. Returning
    None for "already current" is what keeps a boot-time check cheap.
    """
    from django.db import transaction

    from omop_core.models import LoincRelease

    def say(message):
        logger.info(message.strip())
        if stdout is not None:
            stdout.write(message)

    stale, published = is_out_of_date()
    if not stale and not force:
        say(f'LOINC {published["version"]} already loaded; nothing to do.')
        return None

    # One loader at a time. prepare-deployment.sh runs on every instance boot,
    # so scaling to three instances -- or restarting while a worker is mid-load
    # -- otherwise means concurrent 92MB downloads and concurrent
    # bulk_create(update_conflicts=True) over the same 112k rows, which can
    # deadlock on the ON CONFLICT updates. A session-level advisory lock is
    # enough: the loser exits rather than queueing behind the winner, because
    # by the time the winner finishes there is nothing left to do.
    if not _try_lock():
        say('Another LOINC load holds the lock; skipping this one.')
        return None

    try:
        say(f'Loading LOINC {published["version"]} (have {loaded_version() or "nothing"})...')
        chunks = stream_archive(published['download_url'])
    # One transaction over the load and the version row. Recording the version
    # separately would let the tables update while the row failed to write --
    # after which every check reports stale and re-spends the download, with
    # no way to notice.
        with transaction.atomic():
            classes, codes = load_from_archive(chunks, stdout=stdout)
            LoincRelease.objects.update_or_create(
                release_version=published['version'],
                defaults={
                    'release_url': published['download_url'],
                    'archive_md5': published['md5'],
                    'release_date': published['release_date'] or None,
                    'loinc_count': published['loinc_count'],
                },
            )
        say(f'Loaded LOINC {published["version"]}: {classes} classes, {codes} codes.')

        # Only clears this process. Other gunicorn workers self-heal because
        # concept_unit_info refuses to cache an empty mapping (#1624).
        from omop_core.services.concept_unit_info import (
            get_loinc_to_unit, get_loinc_example_units)
        get_loinc_to_unit.cache_clear()
        get_loinc_example_units.cache_clear()
        return published['version']
    finally:
        _unlock()


def dispatch_release_sync(allow_inline=False):
    """Queue a release load if ours is stale. Returns whether one was started.

    **Never raises.** Every caller is a deployment step, and a stale LOINC
    table degrades ``property_for()`` and unit checks -- it is not a reason to
    refuse to serve. An exception escaping here reaches
    ``check_loinc_release``, whose non-zero exit fails
    ``scripts/prepare-deployment.sh`` under ``set -euo pipefail``, turning a
    degraded table into a web service that will not boot. Only
    ``sync_loinc_release``, run deliberately, surfaces errors.

    ``allow_inline`` is off by default and the boot path leaves it off. Without
    a broker the inline branch would fetch ~92MB and load 112k rows *before*
    gunicorn binds its port, and Render kills an instance that does not bind in
    time. Production is exactly that shape: ``render.yaml`` leaves
    ``CELERY_BROKER_URL`` dashboard-managed (``sync: false``) on the web
    service, so a deployment that has not pasted the Redis URL in has no broker
    -- the trap CLAUDE.md already documents for the Suggest inline ceiling.
    Staging has both a broker and a pre-deploy hook and would not have shown
    it.
    """
    try:
        stale, published = is_out_of_date()
    except LoincCredentialsMissing:
        if loaded_version() is None:
            logger.error(
                'LOINC_USER/LOINC_PASSWORD are not set and no LOINC release is '
                'loaded: LoincCodeClass will be empty, so property_for() falls '
                'back to parsing concept names and unit checks are degraded.'
            )
        else:
            logger.warning(
                'LOINC_USER/LOINC_PASSWORD are not set; keeping the loaded '
                'release %s. It will not be refreshed.', loaded_version())
        return False
    except LoincReleaseUnavailable as exc:
        logger.warning('Could not check the LOINC release version: %s', exc)
        return False
    except Exception:
        logger.exception('Unexpected error checking the LOINC release version.')
        return False

    if not stale:
        logger.info('LOINC %s is current.', published['version'])
        return False

    if getattr(settings, 'CELERY_BROKER_URL', ''):
        try:
            from omop_core.tasks import sync_loinc_release_task

            sync_loinc_release_task.delay()
        except Exception:
            # An unreachable broker raises kombu.exceptions.OperationalError.
            logger.exception('Could not queue the LOINC %s load.', published['version'])
            return False
        logger.info('Queued a LOINC %s load on Celery.', published['version'])
        return True

    if not allow_inline:
        logger.warning(
            'LOINC %s is available but CELERY_BROKER_URL is not set, so there '
            'is nowhere to run the load. Refusing to fetch ~92MB in this '
            'process. Configure a broker, or run: manage.py sync_loinc_release',
            published['version'])
        return False

    try:
        sync_release()
    except Exception:
        logger.exception('LOINC %s load failed.', published['version'])
        return False
    return True
