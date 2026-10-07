"""Attach lines of therapy written before #1739 to their person's primary cancer.

Migration ``omop_oncology.0007`` does this on deploy; run this after loading the
Episode vocabulary on a deployment that migrated without it. Idempotent.
"""
from django.core.management.base import BaseCommand

from omop_core.models import Person
from omop_core.services.disease_episodes import attach_unparented_lines
from omop_core.services.mappings import CONCEPT_DISEASE_FIRST_OCCURRENCE
from omop_oncology.models import Episode


class Command(BaseCommand):
    help = 'Attach unparented lines of therapy to a Disease Episode for each person\'s primary cancer.'

    def handle(self, *args, **options):
        person_ids = (
            Episode.objects.exclude(episode_concept_id=CONCEPT_DISEASE_FIRST_OCCURRENCE)
            .filter(episode_parent_id__isnull=True)
            .values_list('person_id', flat=True).distinct()
        )
        moved = people = 0
        for person in Person.objects.filter(person_id__in=list(person_ids)):
            count = attach_unparented_lines(person)
            if count:
                moved += count
                people += 1
        self.stdout.write(f'Attached {moved} line(s) for {people} person(s).')
