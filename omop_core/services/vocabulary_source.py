"""Where the Athena vocabulary and LOINC release archives come from (#2288).

Two sources, tried in order:

1. **Git LFS** -- a public GitHub repository that holds both ZIPs as LFS objects
   and a ``manifest.json`` naming each file with its SHA-256 and size. Resolving
   a release is one small HTTPS request for the manifest; the archive itself is
   fetched from GitHub's LFS media endpoint only when the installed vocabulary
   differs. No Google account, quota or ``gdown`` is involved.
2. **Google Drive** -- the governed folder the project used before, through
   ``gdown``. It is the fallback when the LFS manifest or download fails, and
   the only path when no LFS URL is configured.

A *selection* is a dict describing the chosen release. Every kind carries
``kind``, ``name``, ``identity``, ``loinc_archive_name`` and the download
functions below know how to fetch it. ``identity`` is what the deploy-time
freshness check compares against the last successful sync, so a source
switch never changes it for the same artifact except through the SHA-256,
which the sync command checks separately.
"""

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from django.core.management.base import CommandError

LFS_ENV = 'ATHENA_VOCABULARY_LFS_URL'
GDRIVE_ENV = 'ATHENA_VOCABULARY_GDRIVE_URL'
DEFAULT_LFS_URL = 'https://github.com/healthkey-ai/omop-vocabularies'
MANIFEST_NAME = 'manifest.json'
LOINC_ZIP_RE = re.compile(r'(?i)loinc.*\.zip$')
_LOINC_RE = LOINC_ZIP_RE
_CHUNK = 1024 * 1024
MANIFEST_TIMEOUT = 30
DOWNLOAD_TIMEOUT = (30, 300)
# GitHub's media endpoint dropped a 1 GB stream partway on the first staging
# load (#2288): "Connection broken: IncompleteRead". Resume with a Range
# request before giving the archive up to the Drive fallback.
DOWNLOAD_ATTEMPTS = 4
# The identity column is CharField(500) and holds two names beside two hashes.
MAX_FILE_NAME = 150
LOINC_LOCAL_NAME = 'loinc-release.zip'


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(_CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()


class LfsSourceError(Exception):
    """The LFS source could not answer; the caller may fall back to Drive."""


# --- URL handling ---------------------------------------------------------

def parse_lfs_url(url):
    """Return ``(owner, repo, ref)`` for a GitHub repository URL.

    Accepts ``https://github.com/<owner>/<repo>``, the same with ``.git``, or
    ``.../tree/<ref>`` to pin a branch or tag. The ref defaults to ``main``.
    """
    parsed = urlparse(str(url).strip())
    if parsed.netloc.lower() not in ('github.com', 'www.github.com'):
        raise LfsSourceError(f'Not a GitHub repository URL: {url}')
    parts = [part for part in parsed.path.split('/') if part]
    if len(parts) < 2:
        raise LfsSourceError(f'GitHub URL needs an owner and repository: {url}')
    owner, repo = parts[0], parts[1]
    if repo.endswith('.git'):
        repo = repo[:-4]
    ref = 'main'
    if len(parts) >= 4 and parts[2] == 'tree':
        ref = '/'.join(parts[3:])
    return owner, repo, ref


def is_lfs_url(url):
    try:
        parse_lfs_url(url)
    except LfsSourceError:
        return False
    return True


def manifest_url(url):
    owner, repo, ref = parse_lfs_url(url)
    return f'https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{MANIFEST_NAME}'


def media_url(url, filename):
    """The URL that serves an LFS object's content rather than its pointer."""
    owner, repo, ref = parse_lfs_url(url)
    # The Athena bundle's name carries braces; a release could carry a space.
    return (
        f'https://media.githubusercontent.com/media/{owner}/{repo}/{ref}/'
        f'{quote(filename, safe="")}'
    )


# --- configuration --------------------------------------------------------

def configured_sources(lfs=None, gdrive=None):
    """Resolve the ``(lfs_url, gdrive_url)`` pair a command should use.

    Explicit arguments win; otherwise the environment supplies each one. The
    Drive URL has no built-in default here -- commands that want the historic
    folder pass ``DEFAULT_GDRIVE_URL`` themselves -- so a deployment that sets
    only the LFS URL never silently reaches for Google.
    """
    lfs = (lfs or os.environ.get(LFS_ENV) or '').strip() or None
    gdrive = (gdrive or os.environ.get(GDRIVE_ENV) or '').strip() or None
    return lfs, gdrive


# --- LFS ------------------------------------------------------------------

def _manifest_entry(manifest, key):
    entry = manifest.get(key)
    if not isinstance(entry, dict):
        return None
    name = entry.get('file')
    sha256 = str(entry.get('sha256') or '').lower()
    if not isinstance(name, str) or not name or not re.fullmatch(r'[0-9a-f]{64}', sha256):
        raise LfsSourceError(f'manifest entry {key!r} needs "file" and a SHA-256')
    # The name reaches a local path and the identity column; the manifest is
    # public data, so it is trusted for nothing.
    if (
        '/' in name or '\\' in name or '\x00' in name or name.startswith('.')
        or len(name) > MAX_FILE_NAME
    ):
        raise LfsSourceError(f'manifest entry {key!r} has an unsafe file name: {name!r}')
    return {'name': name, 'sha256': sha256, 'size': entry.get('size')}


def fetch_manifest(url):
    target = manifest_url(url)
    try:
        # raw.githubusercontent.com caches branch refs for minutes; a deploy
        # right after a publish must not read an old manifest against a new
        # object (the SHA mismatch would send it to Drive).
        response = requests.get(
            target, timeout=MANIFEST_TIMEOUT, headers={'Cache-Control': 'no-cache'},
        )
        response.raise_for_status()
        manifest = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise LfsSourceError(f'LFS manifest unavailable at {target}: {exc}') from exc
    if not isinstance(manifest, dict):
        raise LfsSourceError(f'LFS manifest at {target} is not a JSON object')
    return manifest


def resolve_lfs_vocabulary(url, log=lambda _msg: None):
    """Describe the release the LFS repository currently publishes.

    Reads only the manifest. The Athena entry is required; LOINC is optional,
    as in the Drive folder. The identity carries both SHA-256s, so a new LOINC
    release with the same Athena bundle invalidates the deploy-time check, as
    the Drive identity does with file ids.
    """
    manifest = fetch_manifest(url)
    athena = _manifest_entry(manifest, 'athena')
    if not athena:
        raise LfsSourceError(f'LFS manifest at {manifest_url(url)} has no "athena" entry')
    loinc = _manifest_entry(manifest, 'loinc')
    if loinc and (not LOINC_ZIP_RE.match(loinc['name']) or loinc['name'] == athena['name']):
        raise LfsSourceError(f'manifest "loinc" entry does not name a LOINC release: {loinc["name"]!r}')
    loinc_suffix = f'+loinc:{loinc["sha256"]}:{loinc["name"]}' if loinc else ''
    log(f'  Resolved {athena["name"]} from {url} (sha256:{athena["sha256"][:12]}...)')
    return {
        'kind': 'lfs',
        'source_url': url,
        'id': athena['sha256'],
        'name': athena['name'],
        'sha256': athena['sha256'],
        'size': athena.get('size'),
        'download_url': media_url(url, athena['name']),
        'identity': f'lfs-file:{athena["sha256"]}:{athena["name"]}{loinc_suffix}',
        'loinc_archive_id': loinc['sha256'] if loinc else None,
        'loinc_archive_name': loinc['name'] if loinc else None,
        'loinc_download_url': media_url(url, loinc['name']) if loinc else None,
    }


def _discard(path):
    """Remove a partial download; the disk that failed the write may refuse this too."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _received(partial):
    return partial.stat().st_size if partial.exists() else 0


def _stream(url, partial, log):
    """Fetch ``url`` into ``partial``, continuing from the bytes already there.

    A resume asks for a Range. A server that answers 200 instead of 206 sent
    the whole object again, so the file is restarted rather than appended.
    What arrived before a broken stream stays on disk for the next attempt.
    """
    received = _received(partial)
    headers = {'Range': f'bytes={received}-'} if received else {}
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT, headers=headers) as response:
        response.raise_for_status()
        resumed = bool(received) and response.status_code == 206
        if received and not resumed:
            log('  The server did not resume; restarting the download.')
        with open(partial, 'ab' if resumed else 'wb') as out:
            for chunk in response.iter_content(chunk_size=_CHUNK):
                if chunk:
                    out.write(chunk)


def download_lfs_file(url, dest, sha256=None, log=lambda _msg: None):
    """Stream one LFS object to ``dest`` and verify its SHA-256 when given.

    A broken stream is resumed from where it stopped, up to
    ``DOWNLOAD_ATTEMPTS`` tries; the SHA-256 is taken over the finished file.
    """
    dest = Path(dest)
    partial = dest.with_name(dest.name + '.part')
    log(f'  Downloading {dest.name} from {url}...')
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _discard(partial)
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                _stream(url, partial, log)
                break
            except requests.RequestException as exc:
                if attempt == DOWNLOAD_ATTEMPTS:
                    raise
                log(
                    f'  WARNING: download interrupted after {_received(partial):,} bytes ({exc}); '
                    f'resuming (attempt {attempt + 1} of {DOWNLOAD_ATTEMPTS})...'
                )
                time.sleep(min(2 ** attempt, 15))
    except (requests.RequestException, OSError, ValueError) as exc:
        # OSError: the scratch disk, not GitHub -- but the caller's fallback
        # decision is the same, and the receipt should say which file failed.
        # ValueError: a path open() refuses.
        _discard(partial)
        raise LfsSourceError(f'LFS download failed for {url}: {exc}') from exc
    actual = sha256_file(partial)
    if sha256 and actual != sha256:
        _discard(partial)
        raise LfsSourceError(
            f'LFS download of {dest.name} does not match the manifest: '
            f'expected sha256 {sha256}, got {actual}'
        )
    shutil.move(str(partial), str(dest))
    return dest


# --- Google Drive (delegates to the existing gdown helpers) ---------------

def _gdrive():
    from omop_core.management.commands import load_athena_vocabularies as loader
    return loader


def resolve_gdrive_vocabulary(url, log=lambda _msg: None):
    selection = _gdrive()._resolve_gdrive_vocabulary(url, log)
    selection.setdefault('kind', 'gdrive')
    selection.setdefault('source_url', url)
    return selection


# --- the order ------------------------------------------------------------

def resolve_vocabulary_source(lfs=None, gdrive=None, log=lambda _msg: None):
    """Pick the release to load: LFS first, Drive when LFS cannot answer.

    Returns a selection. An LFS selection remembers the Drive URL as
    ``fallback_gdrive_url`` so a download that fails later can still fall
    back. Raises ``CommandError`` when neither source yields a release.
    """
    if not lfs and not gdrive:
        raise CommandError(
            f'No vocabulary source: set {LFS_ENV} or {GDRIVE_ENV}, '
            'or pass --lfs / --gdrive.'
        )
    lfs_error = None
    if lfs:
        try:
            selection = resolve_lfs_vocabulary(lfs, log)
            selection['fallback_gdrive_url'] = gdrive
            return selection
        except LfsSourceError as exc:
            lfs_error = exc
            if not gdrive:
                raise CommandError(str(exc)) from exc
            log(f'  WARNING: {exc}; falling back to Google Drive.')
    try:
        selection = resolve_gdrive_vocabulary(gdrive, log)
    except CommandError as drive_exc:
        # The receipt keeps only the exception text, so both failures go in it.
        if lfs_error is not None:
            raise CommandError(f'{drive_exc} (LFS failed first: {lfs_error})') from drive_exc
        raise
    if lfs_error is not None:
        selection['fallback_from'] = str(lfs_error)
    return selection


def download_vocabulary_archive(selection, download_dir, log=lambda _msg: None):
    """Fetch the Athena ZIP for ``selection``; an LFS failure falls back to Drive.

    When the fallback is taken the selection is updated **in place** -- kind,
    identity, LOINC names -- so a caller that captured the identity before the
    download must re-read it afterwards.
    """
    download_dir = Path(download_dir)
    if selection.get('kind') == 'lfs':
        try:
            return download_lfs_file(
                selection['download_url'], download_dir / 'athena-vocabulary.zip',
                selection.get('sha256'), log,
            )
        except LfsSourceError as exc:
            gdrive = selection.get('fallback_gdrive_url')
            if not gdrive:
                raise CommandError(str(exc)) from exc
            log(f'  WARNING: {exc}; falling back to Google Drive.')
            try:
                replacement = resolve_gdrive_vocabulary(gdrive, log)
            except CommandError as drive_exc:
                raise CommandError(f'{drive_exc} (LFS failed first: {exc})') from drive_exc
            replacement['fallback_from'] = str(exc)
            selection.clear()
            selection.update(replacement)
    loader = _gdrive()
    return loader._download_gdrive_vocabulary(
        selection['source_url'], download_dir, log, selection=selection,
    )


def download_loinc_archive(selection, download_dir, log=lambda _msg: None):
    """Fetch the LOINC release ZIP the selection names, or ``None`` without one.

    LOINC is optional on both sources, so a failed download is a warning: the
    axes simply are not loaded, as before.
    """
    if not selection.get('loinc_archive_id'):
        return None
    download_dir = Path(download_dir)
    if selection.get('kind') == 'lfs':
        try:
            # A fixed local name: the manifest's name is for the identity and
            # the log, never for where the bytes land beside the Athena ZIP.
            return download_lfs_file(
                selection['loinc_download_url'],
                download_dir / LOINC_LOCAL_NAME,
                selection['loinc_archive_id'], log,
            )
        except LfsSourceError as exc:
            log(f'  WARNING: {exc}; LOINC axes will not be loaded.')
            return None
    return _gdrive()._download_gdrive_loinc_archive(
        selection['loinc_archive_id'], selection.get('loinc_archive_name'),
        download_dir, log,
    )


def build_manifest(directory, athena=None, loinc=None, released=None):
    """Write the manifest the LFS repository publishes, hashing the files.

    Used by the publishing instructions in that repository's README; kept here
    so the format has exactly one definition.
    """
    directory = Path(directory)
    zips = sorted(p for p in directory.iterdir() if p.suffix.lower() == '.zip')
    athena = Path(athena) if athena else next(
        (p for p in zips if not _LOINC_RE.match(p.name)), None,
    )
    loinc = Path(loinc) if loinc else next(
        (p for p in zips if _LOINC_RE.match(p.name)), None,
    )
    if athena is None:
        raise CommandError(f'No Athena vocabulary ZIP in {directory}')

    def entry(path):
        return {'file': path.name, 'sha256': sha256_file(path), 'size': path.stat().st_size}

    manifest = {'athena': entry(athena)}
    if loinc is not None:
        manifest['loinc'] = entry(loinc)
        version = re.search(r'(?i)loinc[_-]?(\d+(?:\.\d+)*)', loinc.name)
        if version:
            manifest['loinc']['version'] = version.group(1)
    if released:
        manifest['released'] = str(released)
    target = directory / MANIFEST_NAME
    target.write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest
