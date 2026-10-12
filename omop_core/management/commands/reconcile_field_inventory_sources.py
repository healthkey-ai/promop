"""Reconcile source definitions against a saved reference-only inventory."""
import hashlib
import json
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from omop_core.services.field_inventory import (
    cancerbot_source, coverage, render_report, source_revision,
    staging_therapy_coverage, validate_manifest,
)


def reconcile_sources(manifest, cancerbot_root):
    """Retain snapshot evidence; this operation makes no database connection."""
    source_path = 'trials/services/value_options.py'
    path = Path(cancerbot_root) / source_path
    expected = (manifest['source_revisions'].get('cancerbot') or {}).get('files', {}).get(source_path)
    if expected != hashlib.sha256(path.read_bytes()).hexdigest():
        raise ValueError('CancerBot source differs from the snapshot; refresh the full inventory first.')
    existing_coverage = manifest['totals']['source_coverage']['cancerbot_public_lists']
    if existing_coverage.get('live_metadata'):
        raise ValueError('Use the full exporter with the original live export to preserve live coverage.')
    source = cancerbot_source(path)
    if {b['option_list'] for b in source['bindings']} != {b['option_list'] for b in manifest['cancerbot_bindings']}:
        raise ValueError('Public list identities differ from the snapshot.')
    # Existing field/reference/candidate rows retain their evidence and decisions.
    # Deterministic public-list expansions have a distinct source namespace.
    rows = [r for r in manifest['rows'] if r['source'] != 'cancerbot_static']
    rows += [r for r in source['rows'] if r['source'] == 'cancerbot_static']
    for row in rows:
        if row['source'] == 'cancerbot_source' and row['option_list'].split(':')[0] in {
            'registers', 'trial_purposes', 'trial_types',
        }:
            row.update(disposition='not_applicable',
                       reason='Trial search metadata; outside patient clinical field/value mapping scope.')
    manifest['rows'] = sorted(rows, key=lambda r: (r['source'], r['option_list'], r['id']))
    manifest['cancerbot_bindings'] = source['bindings']
    manifest['therapy_source_coverage'] = staging_therapy_coverage(manifest['reference_tables'], source['bindings'])
    missing = [b['option_list'] for b in source['bindings'] if b['coverage'] == 'requires_live_export']
    existing_coverage.update(by_provider=dict(sorted(Counter(b['coverage'] for b in source['bindings']).items())),
                             missing_live_lists=missing)
    manifest['totals'] = coverage(manifest['rows'], manifest['totals']['source_coverage'])
    manifest['limitations'] = [line for line in manifest['limitations'] if not line.startswith('CancerBot:')]
    manifest['limitations'].insert(0,
        f'CancerBot: {len(missing)} public lists still require live reference coverage. Deterministic source lists '
        'and trial-search exclusions are reconciled; planned picker context remains pending.')
    limitation = 'New static public-list rows retain source definitions without inheriting clinical approvals or candidate evidence; their destination and semantic search remain pending.'
    if limitation not in manifest['limitations']:
        manifest['limitations'].append(limitation)
    manifest['complete'] = False
    validate_manifest(manifest)
    return manifest


class Command(BaseCommand):
    help = 'Reconcile deterministic CancerBot source providers in a saved inventory, without database access.'

    def add_arguments(self, parser):
        parser.add_argument('--inventory', type=Path, required=True)
        parser.add_argument('--cancerbot-root', type=Path, required=True)
        parser.add_argument('--output', type=Path, required=True)
        parser.add_argument('--report', type=Path, required=True)

    def handle(self, **options):
        raw = options['inventory'].read_bytes()
        manifest = json.loads(raw)
        validate_manifest(manifest)
        try:
            manifest = reconcile_sources(manifest, options['cancerbot_root'])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        manifest['source_reconciliation'] = {
            'reconciled_at': timezone.now().isoformat(),
            'input_manifest_sha256': hashlib.sha256(raw).hexdigest(),
            'reference_snapshot_unchanged': True,
            'tool_source': source_revision(Path(settings.BASE_DIR), [
                'omop_core/services/cancerbot_static_options.py',
                'omop_core/services/field_inventory.py',
                'omop_core/management/commands/reconcile_field_inventory_sources.py',
            ]),
        }
        options['output'].write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str) + '\n')
        options['report'].write_text(render_report(manifest))
        self.stdout.write(json.dumps(manifest['totals']['source_coverage']['cancerbot_public_lists']['by_provider']))
