"""Merge a reference-only option export into a saved manifest, without DB writes."""
import hashlib
import copy
import json
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.utils import timezone

from omop_core.services.field_inventory import (
    apply_live_coverage, coverage, render_report, validate_live_export, validate_manifest, source_revision,
)


def merge_reference_options(manifest, payload):
    validate_manifest(manifest)
    manifest = copy.deepcopy(manifest)
    previous_ids = {r['id'] for r in manifest['rows']}
    bindings = manifest['cancerbot_bindings']
    rows, _ = validate_live_export(payload, [b['option_list'] for b in bindings])
    previous = {r['id']: r for r in manifest['rows'] if r['source'] == 'cancerbot_live'}
    for i, row in enumerate(rows):
        if row['id'] in previous:
            # Preserve earlier reconciliation/candidate evidence for stable source identities.
            saved = copy.deepcopy(previous[row['id']])
            if saved['source_label'] != row['source_label']:
                saved.setdefault('validation_flags', []).append('source_label_changed')
                saved['disposition'] = 'needs_review'
            saved['source_label'] = row['source_label']
            saved['search_evidence'] = [e for e in saved['search_evidence'] if e.get('method') != 'live_reference_export'] + row['search_evidence']
            rows[i] = saved
    replaced = set(payload['options'])
    current_ids = {r['id'] for r in rows}
    retained = []
    for row in manifest['rows']:
        if row['id'] in current_ids:
            continue
        if row['source'] == 'cancerbot_live' and row['option_list'] in replaced:
            # Missing membership is evidence, not proof of clinical retirement.
            row['source_presence'] = 'absent_from_latest_export'
            row['last_membership_checked_at'] = payload['exported_at']
        retained.append(row)
    for row in rows:
        row['source_presence'] = 'present'
        row['last_membership_checked_at'] = payload['exported_at']
    manifest['rows'] = sorted(retained + rows, key=lambda r: (r['source'], r['option_list'], r['id']))
    missing = apply_live_coverage(bindings, payload, rows)
    pending = [b['option_list'] for b in bindings if b['coverage'] == 'staging_catalog_available_context_pending']
    manifest['therapy_source_coverage']['context_pending_lists'] = pending
    source = manifest['totals']['source_coverage']['cancerbot_public_lists']
    source.update(
        coverage='source_membership_accounted_for' if not missing and not pending else 'partial',
        by_provider=dict(sorted(Counter(b['coverage'] for b in bindings).items())),
        missing_live_lists=missing,
        live_metadata={key: value for key, value in payload.items() if key != 'options'},
    )
    if 'implementation_contracts' in manifest:
        from omop_core.services.field_inventory_contracts import implementation_contracts
        crosswalk = manifest.get('destination_crosswalk', {})
        reviewed_revision = (manifest.get('source_revisions', {}).get('cancerbot') or {}).get('revision')
        source_review_required = (payload['source_revision'] != reviewed_revision
                                  or crosswalk.get('status') == 'source_revision_requires_review')
        if source_review_required:
            # New source checkout definitions must be reviewed before their
            # catalog entries inherit the old routing decisions.
            crosswalk['status'] = 'source_revision_requires_review'
            for row in manifest['rows']:
                if row['source'] in {'cancerbot_live', 'cancerbot_static', 'cancerbot_source'}:
                    row.pop('destination_route_keys', None)
            manifest['totals']['source_coverage']['cancerbot_destination_routes'] = {'coverage': 'source_revision_requires_review'}
            crosswalk = {}
        else:
            by_list = {}
            for row in manifest['rows']:
                if row['source'] in {'cancerbot_live', 'cancerbot_static'}:
                    by_list.setdefault(row['option_list'], []).append(row)
            for route in crosswalk.get('bindings', []):
                related = by_list.get(route['option_list'], [])
                route['source_row_ids'] = sorted(set(route.get('source_row_ids', [])) | {r['id'] for r in related})
                for row in related:
                    row['destination_route_keys'] = [route['option_list']]
        manifest['implementation_contracts'] = implementation_contracts(
            manifest['rows'], crosswalk, bindings, manifest.get('frontend', {}), manifest.get('representation_decisions', []),
            preserve_dispositions_for=previous_ids)
        if source_review_required:
            manifest['implementation_contracts']['source_review_required'] = True
    manifest['totals'] = coverage(manifest['rows'], manifest['totals']['source_coverage'])
    manifest['limitations'] = [line for line in manifest['limitations'] if not line.startswith('CancerBot:')]
    manifest['limitations'].insert(0,
        f'CancerBot: {len(missing)} public lists lack reference coverage; {len(pending)} planned lists lack source eligibility coverage. '
        'Live reference rows use checked-in provider semantics, not a verified deployed application revision. '
        'Source-to-destination relationships and clinical mappings still require reconciliation.')
    manifest['complete'] = False
    validate_manifest(manifest)
    return manifest


class Command(BaseCommand):
    help = 'Merge a reference-only CancerBot option export into the saved field inventory without connecting to a database.'

    def add_arguments(self, parser):
        parser.add_argument('--inventory', type=Path, required=True)
        parser.add_argument('--cancerbot-export', type=Path, required=True)
        parser.add_argument('--output', type=Path, required=True)
        parser.add_argument('--report', type=Path, required=True)

    def handle(self, **options):
        manifest = json.loads(options['inventory'].read_text())
        raw = options['cancerbot_export'].read_bytes()
        try:
            manifest = merge_reference_options(manifest, json.loads(raw))
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        manifest['cancerbot_reference_export_sha256'] = hashlib.sha256(raw).hexdigest()
        manifest['reference_reconciliation'] = {
            'reconciled_at': timezone.now().isoformat(),
            'tool_source': source_revision(Path(settings.BASE_DIR), [
                'omop_core/services/field_inventory.py',
                'omop_core/services/field_inventory_contracts.py',
                'omop_core/services/cancerbot_reference_options.py',
                'omop_core/management/commands/export_cancerbot_reference_options.py',
                'omop_core/management/commands/import_field_inventory_reference_options.py',
            ]),
        }
        options['output'].write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str) + '\n')
        options['report'].write_text(render_report(manifest))
        self.stdout.write(json.dumps(manifest['totals']['source_coverage']['cancerbot_public_lists']['by_provider']))
