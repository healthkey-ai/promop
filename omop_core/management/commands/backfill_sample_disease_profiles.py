"""Repair and complete sample-only disease profiles with batched OMOP writes."""
import json
from collections import Counter, defaultdict
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from omop_core.models import FieldConceptMapping, Measurement, Observation, PatientRecord, ProcedureOccurrence
from omop_core.services.flipi import calculate_flipi
from omop_core.services.pk import next_pk_batch
from omop_core.services.sample_disease_profiles import (
    PREFIX, PROFILE_FIELDS, PROFILE_UNITS, missing, profile_values, recover_sample_source_fields,
)
from omop_core.services.sample_patient_stage import SAMPLE_ORG_DISEASES
from omop_core.signals import suppress_patient_record_refresh


class Command(BaseCommand):
    help = 'Preview disease-specific sample profile completion; apply with --confirm.'

    def add_arguments(self, parser):
        parser.add_argument('--org-slugs', default=','.join(SAMPLE_ORG_DISEASES))
        parser.add_argument('--confirm', action='store_true')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--limit', type=int)
        parser.add_argument('--disease', choices=['FL', 'MM', 'BC'], help='Explicit disease for a custom synthetic organization.')
        parser.add_argument('--person-ids', help='Restrict to selected sample person IDs.')
        parser.add_argument('--batch-size', type=int, default=100)

    def handle(self, *args, **options):
        slugs = [s.strip().lower() for s in options['org_slugs'].split(',') if s.strip()]
        if not slugs or (set(slugs) - SAMPLE_ORG_DISEASES.keys() and not options['disease']):
            raise CommandError('Choose only known sample organizations.')
        if options['batch_size'] < 1 or (options['limit'] is not None and options['limit'] < 1):
            raise CommandError('Limits and batch size must be positive.')
        dry = options['dry_run'] or not options['confirm']
        scope = Q()
        for slug in slugs: scope |= Q(organization__slug__iexact=slug)
        if options['person_ids']:
            try: scope &= Q(person_id__in=[int(x) for x in options['person_ids'].split(',')])
            except ValueError: raise CommandError('Person IDs must be integers.') from None
        ids = list(PatientRecord.objects.filter(scope).order_by('pk').values_list('pk', flat=True))
        if options['limit']: ids = ids[:options['limit']]
        mapping_rows = FieldConceptMapping.objects.filter(status='approved', field_name__in=PROFILE_FIELDS).exclude(field_name='cytogenetic_markers', vocabulary_id='SNOMED', concept_code='107675007').select_related('concept')
        mappings = {m.field_name: m for m in mapping_rows if m.omop_table in {'measurement', 'observation'} and m.concept_id}
        fields = {f.name: f for f in PatientRecord._meta.fields}
        counts = Counter()
        self.stdout.write(f'{"Previewing" if dry else "Applying"} disease profiles for {len(ids)} sample patients')
        for offset in range(0, len(ids), options['batch_size']):
            with transaction.atomic(), suppress_patient_record_refresh():
                records = PatientRecord.objects.filter(pk__in=ids[offset:offset+options['batch_size']]).select_related('organization')
                if not dry: records = records.select_for_update(of=('self',))
                records = list(records)
                people = [r.person_id for r in records]
                rows = defaultdict(list)
                # One read per OMOP table per batch. Avoid a remote query per field.
                for model, prefix in [(Measurement, 'measurement'), (Observation, 'observation')]:
                    for row in model.objects.filter(person_id__in=people, is_erroneous=False).order_by('-'+prefix+'_date', '-'+prefix+'_id'):
                        rows[row.person_id].append(row)
                transplants = {}
                for proc in ProcedureOccurrence.objects.filter(person_id__in=people, is_erroneous=False).filter(Q(procedure_source_value='58336002') | Q(procedure_concept__concept_code='58336002')).order_by('-procedure_date', '-pk'):
                    transplants.setdefault(proc.person_id, proc.procedure_date)
                creates = {Measurement: [], Observation: []}
                changed_records, changed_fields = [], set()
                for record in records:
                    disease = options['disease'] or SAMPLE_ORG_DISEASES[record.organization.slug.lower()]
                    if disease == 'FL' and 'mantle' in (record.disease or '').lower():
                        counts['skipped:other disease'] += 1
                        continue
                    prior = {field: getattr(record, field) for field in PROFILE_FIELDS}
                    prior['stage'] = record.stage
                    protected = set(record.user_edited_fields or [])
                    recovered = recover_sample_source_fields(record, rows[record.person_id], disease)
                    if disease == 'MM' and record.person_id in transplants:
                        recovered.setdefault('stem_cell_transplant_history', ['autologous SCT'])
                        recovered.setdefault('sct_date', transplants[record.person_id])
                    for field, value in recovered.items():
                        if field not in protected and (missing(getattr(record, field)) or field == 'stage'):
                            setattr(record, field, value)
                    synthetic = profile_values(record, disease)
                    for field, value in synthetic.items():
                        if field not in protected: setattr(record, field, value)
                    delta = {field: getattr(record, field) for field, value in prior.items() if value != getattr(record, field)}
                    # Recovered standard facts already exist; explicit demo facts make
                    # previously unreadable legacy values durable under a valid local key.
                    for field, value in delta.items():
                        counts[f'{disease}:{field}'] += 1
                        if field == 'stage': continue
                        mapping = mappings.get(field)
                        kind = fields[field].get_internal_type()
                        target = mapping.omop_table if mapping else ('measurement' if kind in {'DecimalField', 'FloatField', 'IntegerField', 'PositiveIntegerField', 'PositiveSmallIntegerField'} else 'observation')
                        model = Measurement if target == 'measurement' else Observation
                        numeric = kind in {'DecimalField', 'FloatField', 'IntegerField', 'PositiveIntegerField', 'PositiveSmallIntegerField', 'BooleanField'}
                        text_value = None if numeric else json.dumps(value) if kind == 'JSONField' else str(value)
                        if text_value is not None and len(text_value) > model._meta.get_field('value_as_string').max_length:
                            raise CommandError(f'{field} exceeds OMOP scalar width; refusing to truncate.')
                        source = (mapping.source_value or mapping.concept.concept_code) if mapping else PREFIX+field
                        creates[model].append(model(**{
                            'person_id': record.person_id, target+'_concept_id': mapping.concept_id if mapping else 0,
                            target+'_type_concept_id': 0, target+'_date': date.today(),
                            target+'_source_value': source, 'value_source_value': PREFIX+field,
                            'qualifier_source_value': 'synthetic demo profile' if field in synthetic else 'recovered demo source',
                            'value_as_number': float(value) if numeric else None, 'value_as_string': text_value,
                            'unit_source_value': PROFILE_UNITS.get(field),
                        }))
                    if disease == 'FL':
                        if record.gelf_criteria_options is not None:
                            status = 'Met' if record.gelf_criteria_options else 'Not Met'
                            if record.gelf_criteria_status != status:
                                record.gelf_criteria_status = status
                                delta['gelf_criteria_status'] = status
                        try: score, risk = calculate_flipi(record.flipi_score_options)
                        except ValueError: score, risk = None, None
                        if record.flipi_score != score or record.flipi_risk_category != risk:
                            record.flipi_score, record.flipi_risk_category = score, risk
                            delta.update(flipi_score=score, flipi_risk_category=risk)
                    if delta:
                        changed_records.append(record)
                        changed_fields.update(delta)
                    counts['processed'] += 1
                if not dry:
                    for model, objects in creates.items():
                        keys = next_pk_batch(model, model._meta.pk.name, len(objects))
                        for obj, key in zip(objects, keys): obj.pk = key
                        model.objects.bulk_create(objects, batch_size=500)
                    if changed_records:
                        PatientRecord.objects.bulk_update(changed_records, sorted(changed_fields), batch_size=100)
                counts['updated'] += len(changed_records)
            self.stdout.write(f'Processed {min(offset+options["batch_size"],len(ids))}/{len(ids)}; updated={counts["updated"]}')
        self.stdout.write(json.dumps(dict(sorted(counts.items())), sort_keys=True))
        self.stdout.write('Preview complete; use --confirm.' if dry else 'Sample disease profiles complete.')
