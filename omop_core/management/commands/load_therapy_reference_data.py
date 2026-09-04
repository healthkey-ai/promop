"""Load therapy reference data from CSVs into the database.

Idempotent — safe to re-run. Uses get_or_create for all rows.

Usage:
    DATABASE_URL="postgresql://postgres@localhost:5432/promop_test" \\
      .venv/bin/python manage.py load_therapy_reference_data
"""
import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import (
    Concept, Disease,
    TherapyRegimen, TherapyComponent, TherapyClass,
    TherapyRegimenComponent, TherapyComponentClassLink,
    TherapyRound, DiseaseTherapyRegimen,
)

import logging

logger = logging.getLogger(__name__)

_THERAPY_ROUNDS = [
    ('first_line_therapy', 'First Line Therapy'),
    ('second_line_therapy', 'Second Line Therapy'),
    ('second_round_therapy', 'Second Round Therapy'),
    ('later_line_therapy', 'Later Line Therapy'),
    ('supportive_therapy', 'Supportive Therapy'),
]


def _concept_or_none(concept_id_str):
    """Return a Concept instance or None."""
    if not concept_id_str or not str(concept_id_str).strip():
        return None
    try:
        cid = int(concept_id_str)
    except (ValueError, TypeError):
        return None
    try:
        return Concept.objects.get(concept_id=cid)
    except Concept.DoesNotExist:
        logger.warning(f'Concept {cid} not found in DB — setting to None')
        return None


class Command(BaseCommand):
    help = 'Load therapy reference data from CSVs into TherapyRegimen/Component/Class tables.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--data-dir', default='data',
            help='Directory containing the CSV files (default: data/)',
        )

    def handle(self, *args, **options):
        data_dir = Path(options['data_dir'])

        tc_path = data_dir / 'therapies_and_components.csv'
        cc_path = data_dir / 'components_and_classes.csv'
        dtr_path = data_dir / 'disease_therapy_rounds.csv'

        for p in [tc_path, cc_path, dtr_path]:
            if not p.exists():
                raise CommandError(f'Missing CSV: {p}. Run generate_therapy_csvs first.')

        # ── Seed TherapyRound ─────────────────────────────────────────────
        for code, title in _THERAPY_ROUNDS:
            TherapyRound.objects.get_or_create(code=code, defaults={'title': title})
        self.stdout.write(f'TherapyRound: {len(_THERAPY_ROUNDS)} seeded')

        # ── Ensure required diseases exist ──────────────────────────────
        Disease.objects.get_or_create(
            code='MCL', defaults={'title': 'Mantle Cell Lymphoma'},
        )

        # ── Load therapies_and_components.csv ─────────────────────────────
        regimen_count = 0
        component_count = 0
        link_count = 0
        updated_count = 0
        with open(tc_path, newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                concept = _concept_or_none(row.get('therapy_concept_id'))
                regimen, created = TherapyRegimen.objects.get_or_create(
                    code=row['therapy_code'],
                    defaults={
                        'title': row['therapy_title'],
                        'concept': concept,
                    },
                )
                if created:
                    regimen_count += 1
                elif concept and regimen.concept_id != concept.concept_id:
                    regimen.concept = concept
                    regimen.title = row['therapy_title']
                    regimen.save(update_fields=['concept', 'title'])
                    updated_count += 1

                concept = _concept_or_none(row.get('component_concept_id'))
                component, created = TherapyComponent.objects.get_or_create(
                    code=row['component_code'],
                    defaults={
                        'title': row['component_title'],
                        'concept': concept,
                    },
                )
                if created:
                    component_count += 1
                elif concept and component.concept_id != concept.concept_id:
                    component.concept = concept
                    component.title = row['component_title']
                    component.save(update_fields=['concept', 'title'])
                    updated_count += 1

                _, created = TherapyRegimenComponent.objects.get_or_create(
                    regimen=regimen,
                    component=component,
                )
                if created:
                    link_count += 1

        self.stdout.write(
            f'therapies_and_components.csv: '
            f'{regimen_count} regimens, {component_count} components, {link_count} links'
            + (f', {updated_count} concept IDs updated' if updated_count else '')
        )

        # ── Load components_and_classes.csv ───────────────────────────────
        class_count = 0
        class_link_count = 0
        with open(cc_path, newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                concept = _concept_or_none(row.get('component_concept_id'))
                component, created = TherapyComponent.objects.get_or_create(
                    code=row['component_code'],
                    defaults={
                        'title': row['component_title'],
                        'concept': concept,
                    },
                )
                if not created and concept and component.concept_id != concept.concept_id:
                    component.concept = concept
                    component.save(update_fields=['concept'])

                concept = _concept_or_none(row.get('class_concept_id'))
                therapy_class, created = TherapyClass.objects.get_or_create(
                    code=row['class_code'],
                    defaults={
                        'title': row['class_title'],
                        'concept': concept,
                    },
                )
                if created:
                    class_count += 1
                elif concept and therapy_class.concept_id != concept.concept_id:
                    therapy_class.concept = concept
                    therapy_class.save(update_fields=['concept'])

                _, created = TherapyComponentClassLink.objects.get_or_create(
                    component=component,
                    therapy_class=therapy_class,
                )
                if created:
                    class_link_count += 1

        self.stdout.write(
            f'components_and_classes.csv: '
            f'{class_count} classes, {class_link_count} component-class links'
        )

        # ── Load disease_therapy_rounds.csv ───────────────────────────────
        dtr_count = 0
        dtr_skipped = 0
        with open(dtr_path, newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    regimen = TherapyRegimen.objects.get(code=row['therapy_code'])
                except TherapyRegimen.DoesNotExist:
                    logger.warning(f'Regimen not found: {row["therapy_code"]}')
                    dtr_skipped += 1
                    continue

                try:
                    disease = Disease.objects.get(code=row['disease'])
                except Disease.DoesNotExist:
                    logger.warning(f'Disease not found: {row["disease"]}')
                    dtr_skipped += 1
                    continue

                round_obj, _ = TherapyRound.objects.get_or_create(
                    code=row['round'],
                    defaults={'title': row['round'].replace('_', ' ').title()},
                )

                _, created = DiseaseTherapyRegimen.objects.get_or_create(
                    disease=disease,
                    round=round_obj,
                    regimen=regimen,
                )
                if created:
                    dtr_count += 1

        self.stdout.write(
            f'disease_therapy_rounds.csv: '
            f'{dtr_count} disease-therapy-round links'
            + (f' ({dtr_skipped} skipped)' if dtr_skipped else '')
        )

        self.stdout.write(self.style.SUCCESS('Done.'))
