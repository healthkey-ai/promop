"""Close the Open Wearables day totals nothing more is expected for (#2810).

A sync closes its own connection's idle days as it goes. A phone that never
syncs again leaves its last days open, with the keys of the records counted;
this drops those keys. Safe to run at any time and as often as wanted: a
closed day keeps its total. Nothing schedules it.
"""
from django.core.management.base import BaseCommand

from omop_core.services.open_wearables import close_idle_days


class Command(BaseCommand):
    help = "Drop the record keys of Open Wearables days that no longer receive data."

    def handle(self, *args, **options):
        closed = close_idle_days()
        self.stdout.write(f'Closed {closed} day total(s).')
