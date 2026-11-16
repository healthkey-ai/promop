"""Export the #1311 priority-field coverage from the reference-only inventory.

The classification lives in `field_inventory_priority.priority_field_coverage`,
which the inventory export already writes into `manifest.json`. This command only
extracts that section into a focused, reviewable artifact and fails when the
committed evidence is stale; it never re-classifies or changes a recipe.
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from omop_core.services.field_inventory_priority import (
    priority_json, priority_markdown, priority_ready,
)

JSON_NAME = 'genomics-priority-coverage.json'
MARKDOWN_NAME = 'genomics-priority-coverage.md'


class Command(BaseCommand):
    help = ('Extract the per-disease priority gene/marker coverage from the field-mapping '
            'inventory; missing, unapproved or incompatible recipes fail.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--manifest', default=None,
            help='Inventory manifest (default: docs/field-mapping-inventory/manifest.json).')
        parser.add_argument('--output-dir', default=None,
                            help='Artifact directory (default: the manifest directory).')
        parser.add_argument('--write', action='store_true', help='Write the JSON and Markdown artifacts.')
        parser.add_argument('--check', action='store_true', help='Fail if the checked-in artifacts are stale.')

    def handle(self, *args, **options):
        if options['write'] and options['check']:
            raise CommandError('Pass either --write or --check, not both.')
        manifest_path = Path(options['manifest']) if options['manifest'] else (
            Path(settings.BASE_DIR) / 'docs' / 'field-mapping-inventory' / 'manifest.json')
        if not manifest_path.is_file():
            raise CommandError(f'Inventory manifest not found: {manifest_path}')
        manifest = json.loads(manifest_path.read_text())
        coverage = manifest.get('priority_field_coverage')
        if not isinstance(coverage, dict) or not coverage.get('fields'):
            raise CommandError(
                f'{manifest_path} has no priority_field_coverage section; regenerate the inventory first.')
        base = Path(options['output_dir']) if options['output_dir'] else manifest_path.parent
        json_path, markdown_path = base / JSON_NAME, base / MARKDOWN_NAME
        json_text, markdown = priority_json(coverage), priority_markdown(coverage)

        if options['check']:
            stale = [
                path for path, text in ((json_path, json_text), (markdown_path, markdown))
                if not path.exists() or path.read_text() != text
            ]
            if stale:
                raise CommandError(
                    'Genomics priority coverage artifacts are stale: '
                    + ', '.join(str(path) for path in stale)
                    + '. Run `manage.py export_genomics_priority_coverage --write`.')
            self.stdout.write(self.style.SUCCESS(
                f'Coverage artifacts are current: {json_path.name}, {markdown_path.name}'))
        elif options['write']:
            base.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json_text)
            markdown_path.write_text(markdown)
            self.stdout.write(f'Wrote {json_path} and {markdown_path}')
        else:
            self.stdout.write(json_text)

        self.stdout.write(
            f"{coverage['total_fields']} priority fields, {coverage['disease_memberships']} "
            'disease memberships: '
            + ', '.join(f'{state}={count}' for state, count in sorted(coverage['counts'].items())))
        if not priority_ready(coverage):
            failing = [record for record in coverage['fields']
                       if 'storage_recipe_incomplete' in record['state']]
            for record in failing:
                self.stderr.write(
                    f"  {record['field_name']}: {', '.join(record.get('problems') or [])}")
            raise CommandError(
                'Genomics priority coverage is incomplete; repair the recipes and regenerate the inventory.')
        self.stdout.write(self.style.SUCCESS(
            'Every priority field has an approved, Measurement/JSON/multiple storage recipe.'))
