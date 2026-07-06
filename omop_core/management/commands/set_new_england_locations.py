"""
Management command to assign random New England city/state/zip to all patients.

Fields updated: city, region (state abbreviation), postal_code, country

Usage:
    python manage.py set_new_england_locations
    python manage.py set_new_england_locations --dry-run
    python manage.py set_new_england_locations --overwrite   # re-randomise already-set records
"""

import random
from django.core.management.base import BaseCommand
from omop_core.models import PatientRecord


# (city, state_abbr, zip)
NEW_ENGLAND_LOCATIONS = [
    # Maine
    ("Portland",        "ME", "04101"),
    ("Portland",        "ME", "04102"),
    ("Portland",        "ME", "04103"),
    ("Lewiston",        "ME", "04240"),
    ("Bangor",          "ME", "04401"),
    ("South Portland",  "ME", "04106"),
    ("Auburn",          "ME", "04210"),
    ("Biddeford",       "ME", "04005"),
    ("Sanford",         "ME", "04073"),
    ("Augusta",         "ME", "04330"),
    ("Saco",            "ME", "04072"),
    ("Westbrook",       "ME", "04092"),
    ("Waterville",      "ME", "04901"),
    ("Presque Isle",    "ME", "04769"),
    ("Bar Harbor",      "ME", "04609"),
    # New Hampshire
    ("Manchester",      "NH", "03101"),
    ("Manchester",      "NH", "03102"),
    ("Manchester",      "NH", "03103"),
    ("Nashua",          "NH", "03060"),
    ("Nashua",          "NH", "03062"),
    ("Concord",         "NH", "03301"),
    ("Derry",           "NH", "03038"),
    ("Dover",           "NH", "03820"),
    ("Rochester",       "NH", "03867"),
    ("Salem",           "NH", "03079"),
    ("Portsmouth",      "NH", "03801"),
    ("Keene",           "NH", "03431"),
    ("Laconia",         "NH", "03246"),
    ("Durham",          "NH", "03824"),
    ("Hanover",         "NH", "03755"),
    # Vermont
    ("Burlington",      "VT", "05401"),
    ("Burlington",      "VT", "05403"),
    ("Essex Junction",  "VT", "05452"),
    ("Rutland",         "VT", "05701"),
    ("Barre",           "VT", "05641"),
    ("Montpelier",      "VT", "05601"),
    ("South Burlington","VT", "05403"),
    ("St. Johnsbury",   "VT", "05819"),
    ("Brattleboro",     "VT", "05301"),
    ("Middlebury",      "VT", "05753"),
    ("Stowe",           "VT", "05672"),
    ("Woodstock",       "VT", "05091"),
    # Massachusetts
    ("Boston",          "MA", "02101"),
    ("Boston",          "MA", "02108"),
    ("Boston",          "MA", "02115"),
    ("Boston",          "MA", "02118"),
    ("Boston",          "MA", "02127"),
    ("Boston",          "MA", "02130"),
    ("Worcester",       "MA", "01601"),
    ("Worcester",       "MA", "01602"),
    ("Springfield",     "MA", "01101"),
    ("Springfield",     "MA", "01103"),
    ("Lowell",          "MA", "01850"),
    ("Cambridge",       "MA", "02138"),
    ("Cambridge",       "MA", "02139"),
    ("Cambridge",       "MA", "02141"),
    ("New Bedford",     "MA", "02740"),
    ("Brockton",        "MA", "02301"),
    ("Quincy",          "MA", "02169"),
    ("Lynn",            "MA", "01901"),
    ("Fall River",      "MA", "02720"),
    ("Newton",          "MA", "02458"),
    ("Somerville",      "MA", "02143"),
    ("Lawrence",        "MA", "01840"),
    ("Framingham",      "MA", "01701"),
    ("Haverhill",       "MA", "01830"),
    ("Waltham",         "MA", "02451"),
    ("Malden",          "MA", "02148"),
    ("Medford",         "MA", "02155"),
    ("Taunton",         "MA", "02780"),
    ("Chicopee",        "MA", "01013"),
    ("Weymouth",        "MA", "02188"),
    ("Revere",          "MA", "02151"),
    ("Peabody",         "MA", "01960"),
    ("Pittsfield",      "MA", "01201"),
    ("Northampton",     "MA", "01060"),
    ("Amherst",         "MA", "01002"),
    ("Gloucester",      "MA", "01930"),
    ("Salem",           "MA", "01970"),
    ("Lexington",       "MA", "02420"),
    ("Concord",         "MA", "01742"),
    ("Provincetown",    "MA", "02657"),
    ("Nantucket",       "MA", "02554"),
    # Rhode Island
    ("Providence",      "RI", "02901"),
    ("Providence",      "RI", "02903"),
    ("Providence",      "RI", "02906"),
    ("Providence",      "RI", "02908"),
    ("Cranston",        "RI", "02910"),
    ("Warwick",         "RI", "02886"),
    ("Warwick",         "RI", "02888"),
    ("Pawtucket",       "RI", "02860"),
    ("East Providence", "RI", "02914"),
    ("Woonsocket",      "RI", "02895"),
    ("Newport",         "RI", "02840"),
    ("Central Falls",   "RI", "02863"),
    ("Westerly",        "RI", "02891"),
    ("Bristol",         "RI", "02809"),
    # Connecticut
    ("Bridgeport",      "CT", "06601"),
    ("Bridgeport",      "CT", "06604"),
    ("New Haven",       "CT", "06510"),
    ("New Haven",       "CT", "06511"),
    ("New Haven",       "CT", "06515"),
    ("Hartford",        "CT", "06101"),
    ("Hartford",        "CT", "06103"),
    ("Hartford",        "CT", "06106"),
    ("Stamford",        "CT", "06901"),
    ("Stamford",        "CT", "06902"),
    ("Waterbury",       "CT", "06701"),
    ("Norwalk",         "CT", "06850"),
    ("Danbury",         "CT", "06810"),
    ("New Britain",     "CT", "06050"),
    ("Greenwich",       "CT", "06830"),
    ("West Hartford",   "CT", "06107"),
    ("Middletown",      "CT", "06457"),
    ("Meriden",         "CT", "06450"),
    ("Milford",         "CT", "06460"),
    ("Norwich",         "CT", "06360"),
    ("New London",      "CT", "06320"),
    ("Mystic",          "CT", "06355"),
    ("Storrs",          "CT", "06268"),
]


class Command(BaseCommand):
    help = "Set random New England city/state/zip on all patient records"

    def add_arguments(self, parser):
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='Re-randomise patients that already have a city/state/zip set',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print assignments without saving',
        )

    def handle(self, *args, **options):
        overwrite = options['overwrite']
        dry_run = options['dry_run']

        from django.db.models import Q
        if overwrite:
            qs = PatientRecord.objects.all()
        else:
            qs = PatientRecord.objects.filter(
                Q(city__isnull=True) | Q(city='')
            )

        total = qs.count()
        self.stdout.write(f"Patients to update: {total}")

        updated = 0
        for patient in qs:
            city, state, zipcode = random.choice(NEW_ENGLAND_LOCATIONS)

            if dry_run:
                self.stdout.write(
                    f"  [dry-run] Person {patient.person_id}: "
                    f"{city}, {state} {zipcode}"
                )
            else:
                patient.city = city
                patient.region = state
                patient.postal_code = zipcode
                patient.country = "United States"
                patient.save(update_fields=['city', 'region', 'postal_code', 'country'])

            updated += 1

        action = "Would update" if dry_run else "Updated"
        self.stdout.write(self.style.SUCCESS(f"\n{action} {updated} patient(s)."))
