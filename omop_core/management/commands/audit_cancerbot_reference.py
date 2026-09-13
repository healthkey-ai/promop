"""Reproduce source reference evidence without importing CancerBot or its data."""
import ast
import csv
import hashlib
import json
import subprocess
from pathlib import Path

from django.core.management.base import BaseCommand

from .audit_field_value_mappings import source_options


def source_reference(root):
    root = Path(root).expanduser().resolve()
    value_options = root / 'trials/services/value_options.py'
    tree = ast.parse(value_options.read_text())
    bindings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'get_all_options':
            for child in node.body:
                if isinstance(child, ast.Return) and isinstance(child.value, ast.Dict):
                    bindings = [{'source_key': ast.literal_eval(key), 'expression': ast.unparse(value),
                                 'source_line': value.lineno, 'owner_issue': 1223}
                                for key, value in zip(child.value.keys, child.value.values)]

    files = [value_options, *sorted((root / 'trials/services/loaders').glob('*.py'))]
    files += [root / 'trials/services' / name for name in
              ('markers_mapper.py', 'mutations_mapper.py', 'therapies_mapper.py', 'concomitant_medications_mapper.py')]
    evidence = []
    for path in files:
        if not path.is_file():
            continue
        literals = []
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Assign):
                continue
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            if not isinstance(value, (dict, list, tuple)) or not value:
                continue
            # Preserve typed keys, including Boolean false and the empty
            # Unknown sentinel. JSON object keys would erase their types.
            entries = [{'source_value': key, 'source_label': label} for key, label in value.items()] if isinstance(value, dict) else list(value)
            literals.append({'name': ', '.join(ast.unparse(t) for t in node.targets),
                             'source_line': node.lineno, 'entries': entries,
                             'disposition': 'source_evidence_requires_review'})
        evidence.append({'path': str(path.relative_to(root)),
                         'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'literal_assignments': literals})
    crosswalk_path = root / 'docs/omop/mapping/therapy_omop_mapping.csv'
    crosswalk = list(csv.DictReader(crosswalk_path.open())) if crosswalk_path.exists() else []
    commit = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    return {
        'schema_version': 1, 'source_commit': commit.stdout.strip() if commit.returncode == 0 else None,
        'scope': 'Source and checked-in reference crosswalk only; no database or patient reads.',
        'value_options': source_options(value_options), 'api_option_bindings': bindings,
        'source_files': evidence, 'therapy_crosswalk': crosswalk,
        'therapy_crosswalk_sha256': hashlib.sha256(crosswalk_path.read_bytes()).hexdigest() if crosswalk else None,
        'limitations': [
            'Source seeds do not establish the current database-generated options; live parity remains unverified.',
            'Literal assignments include intermediate reference structures, not a deduplicated live option count.',
            'CancerBot therapy decisions are comparison evidence. PRomop owns its existing regimen, component and class tables and mapping tools.',
            'Concept IDs in the crosswalk require vocabulary/code, domain and release verification on PRomop; no approval is imported.',
        ],
    }


class Command(BaseCommand):
    help = 'Read CancerBot option definitions and reference crosswalk without executing its code or changing either database.'

    def add_arguments(self, parser):
        parser.add_argument('--cancerbot-root', required=True)
        parser.add_argument('--output', required=True)

    def handle(self, **options):
        result = source_reference(options['cancerbot_root'])
        Path(options['output']).write_text(json.dumps(result, indent=2) + '\n')
        self.stdout.write(f"Recorded {len(result['api_option_bindings'])} API option bindings and "
                          f"{len(result['therapy_crosswalk'])} therapy crosswalk rows; live parity is unverified.")
