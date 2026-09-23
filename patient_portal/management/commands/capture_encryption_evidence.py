"""
Capture encryption-at-rest evidence for every store that holds PHI (HKI-SEC-08, #60).

The mechanism is platform-managed storage encryption with provider-held keys; see
docs/soc2/encryption-at-rest.md for the decision. This command records what can be
observed about that from inside a running deployment, so the evidence is produced by
the same code on any host rather than by screenshots:

  * which stores hold PHI, derived from the live settings, not from a list that can
    drift: the database, the file storage behind PatientDocument, and the Celery
    broker/result backend;
  * what each store is, as far as the application can see it, and whether any PHI
    sits somewhere no encryption attestation covers;
  * with ``--render``, the Render resources behind those stores, read through the
    Render API.

Encryption itself is not observable from a database client or an API response. The
output says which statements rest on a provider attestation, and lists the manual
attestations (BAA, HIPAA-enabled workspace) that no API exposes.

Output is JSON on stdout or ``--output``. It never contains hostnames, usernames,
passwords, connection strings or the API key. ``--require-covered`` exits non-zero
when any gap is found, so a scheduled run fails loudly.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.core.files.storage import storages
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

RENDER_API = 'https://api.render.com/v1'

# Render names a datastore's host after its resource id: `dpg-<id>-a` for Postgres
# (internally, or as the first label of the external hostname) and `red-<id>` for
# Key Value (the internal host, or the username of an external URL). The id is not
# a credential; the host is still never emitted.
_RENDER_HOST_ID = {
    'postgres': re.compile(r'^(dpg-[a-z0-9]+)(?:-[a-z])?$'),
    'key_value': re.compile(r'^(red-[a-z0-9]+)$'),
}

RENDER_ATTESTATION = {
    'postgres': {
        'statement': 'Render Postgres databases are encrypted at rest using AES-256 '
                     'data encryption. This applies to both primary and replica '
                     'instances, along with all backups.',
        'source': 'https://render.com/docs/postgresql-creating-connecting',
    },
    'hipaa_workspace': {
        'statement': 'All disks and daily snapshots are encrypted at rest.',
        'source': 'https://render.com/blog/introducing-hipaa-enabled-workspaces',
    },
    'key_custody': 'Provider-managed. Render documents no customer-managed key option.',
}

MANUAL_ATTESTATIONS = [
    'Business Associate Agreement with the hosting provider is signed and current.',
    'The workspace hosting these resources is HIPAA-enabled (not exposed by the Render API).',
    "The provider's current SOC 2 Type II report covers encryption at rest.",
]


def render_resource_id(url, kind):
    """The Render resource id a datastore URL points at, or None."""
    parts = urlsplit(url or '')
    for candidate in ((parts.hostname or '').split('.')[0], parts.username or ''):
        match = _RENDER_HOST_ID[kind].match(candidate)
        if match:
            return match.group(1)
    return None


def database_store():
    db = settings.DATABASES['default']
    store = {
        'store': 'database',
        'holds': 'all OMOP clinical tables, PatientRecord, identities, audit trail',
        'engine': db['ENGINE'].rsplit('.', 1)[-1],
        'render_postgres_id': None,
        'gaps': [],
    }
    if connection.vendor != 'postgresql':
        store['gaps'].append(f'{connection.vendor} is not a supported production database.')
        return store
    with connection.cursor() as cursor:
        cursor.execute('SHOW server_version')
        store['server_version'] = cursor.fetchone()[0]
        cursor.execute(
            'SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid()')
        row = cursor.fetchone()
    store['connection_tls'] = (
        {'ssl': row[0], 'version': row[1], 'cipher': row[2]} if row else None)
    host = db.get('HOST') or ''
    store['render_postgres_id'] = render_resource_id(f'//{host}', 'postgres')
    return store


def file_store():
    from omop_core.models import PatientDocument

    storage = storages['default']
    backend = f'{type(storage).__module__}.{type(storage).__name__}'
    store = {
        'store': 'files',
        'holds': 'PatientDocument uploads (advance directives, reports)',
        'backend': backend,
        'stored_files': PatientDocument.objects.exclude(file='').exclude(file__isnull=True).count(),
        'gaps': [],
    }
    location = getattr(storage, 'location', None)
    if location is not None:
        store['local_path'] = str(location)
        if settings.IS_DEPLOYED:
            store['gaps'].append(
                'Uploaded documents are written to the service filesystem. They are '
                'outside the database, so the database encryption and backup '
                'attestations do not cover them. Unless a persistent disk is mounted '
                'at this path, they are also lost on every deploy (see '
                'render_service.disk).')
    return store


def broker_store():
    broker = settings.CELERY_BROKER_URL
    result = settings.CELERY_RESULT_BACKEND
    urls = {u for u in (broker, result) if u}
    store = {
        'store': 'key_value',
        'holds': 'Celery task messages and results (person ids, derivation errors), '
                 'throttle counters',
        'configured': bool(urls),
        'schemes': sorted({urlsplit(u).scheme for u in urls}),
        'render_key_value_ids': sorted(
            {i for i in (render_resource_id(u, 'key_value') for u in urls) if i}),
        'gaps': [],
    }
    return store


class RenderAPI:
    def __init__(self, api_key, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {api_key}', 'Accept': 'application/json'})

    def get(self, path):
        response = self.session.get(f'{RENDER_API}{path}', timeout=20)
        if response.status_code != 200:
            # A failed read fails the capture: missing access is not a passing control.
            raise CommandError(f'Render API read failed for {path}: HTTP {response.status_code}')
        return response.json()


def _pick(data, *keys):
    return {key: data.get(key) for key in keys}


def render_capture(api, service_id, postgres_id, key_value_ids):
    capture = {'attestation': RENDER_ATTESTATION}
    owner_ids = set()
    if service_id:
        service = api.get(f'/services/{service_id}')
        details = service.get('serviceDetails') or {}
        disk = details.get('disk')
        capture['render_service'] = {
            **_pick(service, 'id', 'name', 'type'),
            'region': details.get('region'),
            'plan': details.get('plan'),
            'disk': _pick(disk, 'id', 'mountPath', 'sizeGB') if disk else None,
        }
        owner_ids.add(service.get('ownerId'))
    if postgres_id:
        pg = api.get(f'/postgres/{postgres_id}')
        capture['postgres'] = {
            **_pick(pg, 'id', 'name', 'plan', 'region', 'version', 'status', 'role',
                    'highAvailabilityEnabled', 'diskSizeGB', 'diskAutoscalingEnabled'),
            'ip_allow_list_entries': len(pg.get('ipAllowList') or []),
            'read_replicas': len(pg.get('readReplicas') or []),
            'recovery': _pick(api.get(f'/postgres/{postgres_id}/recovery'),
                              'recoveryStatus', 'startsAt'),
        }
        owner_ids.add((pg.get('owner') or {}).get('id'))
    capture['key_value'] = []
    for kv_id in key_value_ids:
        kv = api.get(f'/key-value/{kv_id}')
        capture['key_value'].append({
            **_pick(kv, 'id', 'name', 'plan', 'region', 'version', 'status'),
            'persistence_mode': (kv.get('options') or {}).get('persistenceMode'),
            'ip_allow_list_entries': len(kv.get('ipAllowList') or []),
        })
        owner_ids.add((kv.get('owner') or {}).get('id'))
    capture['owners'] = [
        _pick(api.get(f'/owners/{owner_id}'), 'id', 'name', 'type', 'twoFactorAuthEnabled')
        for owner_id in sorted(o for o in owner_ids if o)
    ]
    return capture


def assess(evidence):
    stores = {s['store']: s for s in evidence['stores']}
    render = evidence.get('render')
    files = stores.get('files', {})
    disk = ((render or {}).get('render_service') or {}).get('disk')
    if files.get('local_path') and disk and Path(files['local_path']).is_relative_to(
            disk['mountPath']):
        # A mounted disk keeps the files and, in a HIPAA-enabled workspace, is
        # covered by the disk attestation, so the filesystem gap does not apply.
        files['gaps'] = []
    gaps = [f"{s['store']}: {gap}" for s in evidence['stores'] for gap in s['gaps']]
    if render is None:
        return gaps
    if 'postgres' not in render:
        gaps.append('database: no Render Postgres resource identified; the provider '
                    'attestation cannot be tied to this database.')
    elif render['postgres']['recovery'].get('recoveryStatus') != 'AVAILABLE':
        gaps.append('database: point-in-time recovery is not available.')
    if stores['key_value']['configured'] and not render['key_value']:
        gaps.append('key_value: broker is not a Render Key Value resource; no '
                    'attestation covers it.')
    if len(render['owners']) > 1:
        gaps.append('owners: resources span more than one Render workspace.')
    return gaps


class Command(BaseCommand):
    help = 'Capture encryption-at-rest evidence for the stores that hold PHI.'

    def add_arguments(self, parser):
        parser.add_argument('--output', type=Path,
                            help='Write JSON here instead of stdout.')
        parser.add_argument('--render', action='store_true',
                            help='Read the backing Render resources (needs RENDER_API_KEY).')
        parser.add_argument('--render-service-id', default=os.environ.get('RENDER_SERVICE_ID'),
                            help='Defaults to RENDER_SERVICE_ID, which Render sets in every service.')
        parser.add_argument('--render-postgres-id',
                            help='Override the id derived from the database host.')
        parser.add_argument('--require-covered', action='store_true',
                            help='Exit 1 if any gap is found.')

    def handle(self, *args, **options):
        stores = [database_store(), file_store(), broker_store()]
        evidence = {
            'captured_at': datetime.now(timezone.utc).isoformat(),
            'control': 'HKI-SEC-08 encryption at rest',
            'deployment': {
                'is_deployed': settings.IS_DEPLOYED,
                'platform': 'render' if os.environ.get('RENDER') else None,
                'service_name': os.environ.get('RENDER_SERVICE_NAME'),
                'git_commit': os.environ.get('RENDER_GIT_COMMIT'),
            },
            'stores': stores,
            'manual_attestations_required': MANUAL_ATTESTATIONS,
        }
        if options['render']:
            api_key = os.environ.get('RENDER_API_KEY')
            if not api_key:
                raise CommandError('--render needs RENDER_API_KEY in the environment.')
            by_name = {s['store']: s for s in stores}
            evidence['render'] = render_capture(
                RenderAPI(api_key),
                options['render_service_id'],
                options['render_postgres_id'] or by_name['database']['render_postgres_id'],
                by_name['key_value']['render_key_value_ids'],
            )
        evidence['gaps'] = assess(evidence)

        body = json.dumps(evidence, indent=2, sort_keys=True) + '\n'
        if options['output']:
            options['output'].parent.mkdir(parents=True, exist_ok=True)
            options['output'].write_text(body)
            self.stderr.write(f"Captured encryption evidence to {options['output']}")
        else:
            self.stdout.write(body, ending='')
        for gap in evidence['gaps']:
            self.stderr.write(f'GAP: {gap}')
        if options['require_covered'] and evidence['gaps']:
            raise SystemExit(1)
