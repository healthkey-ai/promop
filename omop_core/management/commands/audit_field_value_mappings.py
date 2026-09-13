"""Reference-only coverage inventory. No patient tables or clinical values."""
import ast
import json
from collections import Counter
from pathlib import Path

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from omop_core import models
from omop_core.services.field_values import standard_target


def source_options(path):
    """Read literal source options without importing or executing CancerBot.

    Dynamic lists are explicitly flagged; a source scan is not a live export.
    """
    tree = ast.parse(Path(path).read_text())
    result = []
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef) or cls.name != 'ValueOptions':
            continue
        for method in cls.body:
            if not isinstance(method, ast.FunctionDef) or method.name.startswith('_'):
                continue
            entries = []
            dynamic = False
            for node in ast.walk(method):
                if not isinstance(node, ast.Return):
                    continue
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    dynamic = True
                    continue
                if isinstance(value, dict):
                    entries.extend({'source_key': key, 'label': label, 'disposition': 'needs_review'} for key, label in value.items())
            result.append({'option_list': method.name, 'source_line': method.lineno,
                'literal_values': entries, 'requires_live_export': dynamic,
                'disposition': 'needs_review'})
    return result


def reference_rows(model):
    # SELECT * permits auditing staging before the optional new schema exists.
    # Table identifiers are from installed Django models, never caller input.
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT * FROM {connection.ops.quote_name(model._meta.db_table)} ORDER BY 1')
        names = [c.name for c in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def audit(cancerbot_source=None, cancerbot_export=None):
    fields = []
    mappings = {row['field_name']: row for row in reference_rows(models.FieldConceptMapping)}
    for field in models.PatientRecord._meta.concrete_fields:
        mapping = mappings.pop(field.name, None)
        fields.append({'field': field.name, 'data_type': field.get_internal_type(),
            'model_choices': list(field.flatchoices), 'mapping': mapping,
            'disposition': 'existing_mapping_requires_validation' if mapping else 'needs_review'})
    fields.extend({'field': name, 'mapping': row, 'disposition': 'existing_mapping_requires_validation'} for name, row in mappings.items())
    choices = reference_rows(models.FieldChoice)
    codes = reference_rows(models.FieldChoiceCode)
    code_refs = {(c['vocabulary_id'], c['code']) for c in codes}
    from django.db.models import Q
    where = Q(pk__in=[])
    for vocab, code in code_refs:
        where |= Q(vocabulary_id=vocab, concept_code=code)
    candidates = {(c.vocabulary_id, c.concept_code): c for c in models.Concept.objects.filter(where)}
    def concept_ref(c):
        return None if c is None else {'id': c.pk, 'vocabulary': c.vocabulary_id, 'code': c.concept_code,
            'name': c.concept_name, 'domain': c.domain_id, 'passes_standard_screen': standard_target(c),
            'semantic_approval': False}
    for choice in choices:
        choice['codes'] = [{**c, 'candidate': concept_ref(candidates.get((c['vocabulary_id'], c['code'])))} for c in codes if c['choice_id'] == choice['id']]
        choice['disposition'] = 'needs_review'
    reference = {}
    for model in apps.get_app_config('omop_core').get_models():
        if issubclass(model, models.VocabularyLookup) or model in (models.TherapyOutcome, models.ToxicityGrade):
            rows = reference_rows(model)
            ids = [r['concept_id'] for r in rows if r.get('concept_id')]
            linked = models.Concept.objects.in_bulk(ids)
            for row in rows:
                row['candidate'] = concept_ref(linked.get(row.get('concept_id')))
                row['disposition'] = 'needs_review'
            reference[model._meta.db_table] = rows
    links = {model._meta.db_table: reference_rows(model) for model in (
        models.DiseaseTherapyRegimen, models.TherapyRegimenComponent, models.TherapyComponentClassLink,
        models.TherapyOutcome.diseases.through,
    )}
    from omop_core.services.genomics_catalog import catalog
    result = {'schema_version': 1, 'generated_at': timezone.now().isoformat(),
        'scope': 'Reference data only. Standard screening is not clinical approval.',
        'fields': fields, 'field_choices': choices, 'reference_catalogs': reference, 'reference_links': links,
        'genomics_catalog': catalog(), 'cancerbot_source_options': source_options(cancerbot_source) if cancerbot_source else [],
        'cancerbot_live_options': json.loads(Path(cancerbot_export).read_text()) if cancerbot_export else None,
        'limitations': ['CancerBot database-driven options require a current reference-only export.'] if not cancerbot_export else [],
    }
    tables = connection.introspection.table_names()
    if models.FieldValueConceptMapping._meta.db_table in tables:
        result['value_mappings'] = reference_rows(models.FieldValueConceptMapping)
    result['totals'] = {'fields': len(fields), 'field_choices': len(choices),
        'reference_options': sum(len(rows) for rows in reference.values()),
        'catalogs': {name: len(rows) for name, rows in reference.items()},
        'field_dispositions': dict(Counter(row['disposition'] for row in fields))}
    result['vocabulary_releases'] = reference_rows(models.VocabularyRelease)
    return result


class Command(BaseCommand):
    help = 'Export exhaustive local field/choice/reference coverage; never read patient values or approve candidates.'

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True)
        parser.add_argument('--cancerbot-source', help='Path to ValueOptions source, parsed without execution.')
        parser.add_argument('--cancerbot-export', help='Reference-only ValueOptions JSON export; never a patient export.')

    def handle(self, **options):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION READ ONLY')
            result = audit(options.get('cancerbot_source'), options.get('cancerbot_export'))
        Path(options['output']).write_text(json.dumps(result, indent=2, default=str) + '\n')
        self.stdout.write(json.dumps(result['totals']))
