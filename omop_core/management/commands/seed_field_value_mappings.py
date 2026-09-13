from django.core.management.base import BaseCommand
from django.db import transaction

from omop_core import models
from omop_core.services.field_values import ValueResolver, lock_scope, save_mapping, standard_target, validate_choice
from omop_core.services.field_value_seeds import RELEASE, QUESTIONS, LOOKUPS, answer_candidates


class Command(BaseCommand):
    help = 'Idempotently propose verified field/answer candidates and unresolved catalog choices. Dry-run by default; preserves existing reviews.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, **options):
        counts = {'fields_created': 0, 'choices_created': 0, 'proposals_created': 0, 'existing_reviews_preserved': 0, 'unresolved_candidates': 0}
        with transaction.atomic():
            valid_fields = {f.name for f in models.PatientRecord._meta.concrete_fields}
            for field, vocab, code, expected_id, kind, unit in QUESTIONS:
                if field not in valid_fields:
                    continue
                target = models.Concept.objects.filter(vocabulary_id=vocab, concept_code=code).first()
                if not standard_target(target, {'Measurement', 'Observation'}):
                    target = None
                    counts['unresolved_candidates'] += 1
                _, created = models.FieldConceptMapping.objects.get_or_create(field_name=field, defaults={
                    'concept': target, 'vocabulary_id': vocab, 'concept_code': code,
                    'source_value': code, 'value_kind': kind, 'unit': unit,
                    'omop_table': target.domain_id.lower() if target else '', 'status': 'proposed',
                    'provenance': 'system_generated',
                    'notes': f'{RELEASE}. Exact-code candidate (observed ID {expected_id}); review disease, assay and staging context. Not automatically approved.',
                })
                counts['fields_created'] += int(created)
            resolver = ValueResolver()
            def choice_for(field, context, value, label=None, aliases=()):
                lock_scope(field, context)
                existing = resolver.resolve(field, value, context)
                if not existing and field in ('tumor_stage', 'nodes_stage', 'distant_metastasis_stage'):
                    # Existing TNM options retain their full explanatory labels.
                    matching = [c for c in resolver.choices.get((field, context), [])
                        if c.display.split(':', 1)[0].strip().casefold() == str(value).casefold()]
                    existing = matching[0] if len(matching) == 1 else None
                if existing:
                    return existing
                choice = models.FieldChoice(field_name=field, context_key=context,
                    display=label or str(value), canonical_value=value, aliases=list(aliases))
                validate_choice(choice)
                choice.save()
                resolver.choices.setdefault((field, context), []).append(choice)
                counts['choices_created'] += 1
                return choice
            # Catalog rows remain available even when no equivalent is known.
            for model_name, field in LOOKUPS.items():
                if field not in valid_fields and not field.startswith('genetic_mutations.'):
                    continue
                for row in getattr(models, model_name).objects.all():
                    existing = resolver.resolve(field, row.title) or resolver.resolve(field, row.code)
                    choice = existing or choice_for(field, '', row.title, aliases=[row.code])
                    if not getattr(choice, 'value_mapping', None):
                        save_mapping(choice, {'notes': f'{model_name}:{row.code}. Source catalog option; not a clinical approval.', 'outcome': 'needs_review'})
                        counts['proposals_created'] += 1
            for field, context, value, vocab, code in answer_candidates():
                choice = choice_for(field, context, value)
                previous = models.FieldValueConceptMapping.objects.filter(choice=choice).first()
                # Only upgrade our empty migration/seed proposals, never a curator's decision.
                if previous and (previous.reviewer_id or previous.status != 'proposed' or previous.target_concept_id
                                 or previous.outcome not in ('needs_review',)):
                    counts['existing_reviews_preserved'] += 1
                    continue
                target = models.Concept.objects.filter(vocabulary_id=vocab, concept_code=code).first()
                if not standard_target(target, {'Meas Value'}):
                    target = None
                    counts['unresolved_candidates'] += 1
                notes = f'{RELEASE}. Exact candidate {vocab}:{code}; confirm source label, context and vocabulary provenance before approval.'
                if previous and previous.notes == notes and previous.target_concept_id == (target.pk if target else None):
                    continue
                save_mapping(choice, {'target_concept': target, 'role': 'answer', 'status': 'proposed',
                    'outcome': 'mapped' if target else 'needs_review', 'notes': notes, 'vocabulary_release': RELEASE})
                counts['proposals_created'] += 1
            if not options['apply']:
                transaction.set_rollback(True)
        self.stdout.write(('APPLIED ' if options['apply'] else 'DRY RUN ') + str(counts))
