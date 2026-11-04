"""Load consent purposes, legal documents and their versioned texts from a JSON file.

The wording a member agrees to is data, so a deployment keeps it in a file of
its own and loads it with this command on each deploy. The load is additive
and idempotent: purposes and documents are created or have their settings
updated; a text version that does not exist yet is added; a version that
already exists is left exactly as it is, published or not, because members
may have agreed to it. A wording change is therefore always a new version.

    manage.py load_consent_texts path/to/consent.json [--publish]

File shape::

    {
      "purposes": [
        {"key": "my_record", "kind": "permission", "answers": "yes_no",
         "stage": "first_profile", "sort_order": 1,
         "texts": [{"version": "1.0", "language": "en", "title": "...",
                    "summary": "...", "body": "...", "published_at": "2026-11-01T00:00:00Z"}]}
      ],
      "documents": [
        {"key": "terms", "name": "Terms of Service",
         "versions": [{"version": "0.4", "language": "en", "title": "...", "body": "...",
                       "requires_reacceptance": true, "published_at": "..."}]}
      ]
    }

``--publish`` publishes every newly added version that has no ``published_at``
of its own, at the time of the load.
"""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from patient_portal.models import ConsentPurpose, ConsentText, LegalDocument, LegalDocumentVersion

PURPOSE_SETTINGS = (
    'kind', 'answers', 'asks_frequency', 'frequency_options', 'stage',
    'gated_by_country', 'enabled_countries', 'sort_order', 'is_active',
)
DOCUMENT_SETTINGS = ('name', 'sort_order', 'is_active')
TEXT_FIELDS = ('title', 'summary', 'body')


def _published_at(item, publish_now):
    raw = item.get('published_at')
    if raw:
        when = parse_datetime(raw)
        if when is None:
            raise CommandError(f'Bad published_at {raw!r}')
        return when
    return timezone.now() if publish_now else None


class Command(BaseCommand):
    help = 'Load consent purposes, documents and texts from a JSON file (additive, idempotent).'

    def add_arguments(self, parser):
        parser.add_argument('path')
        parser.add_argument('--publish', action='store_true', help='Publish new versions that carry no published_at.')

    def handle(self, path, publish, **options):
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        added = kept = 0
        with transaction.atomic():
            for spec in data.get('purposes', []):
                purpose, _ = ConsentPurpose.objects.update_or_create(
                    key=spec['key'], defaults={k: spec[k] for k in PURPOSE_SETTINGS if k in spec},
                )
                for text in spec.get('texts', []):
                    _, created = ConsentText.objects.get_or_create(
                        purpose=purpose, version=text['version'], language=text.get('language', 'en'),
                        defaults={**{k: text.get(k, '') for k in TEXT_FIELDS},
                                  'published_at': _published_at(text, publish)},
                    )
                    added, kept = added + created, kept + (not created)
            for spec in data.get('documents', []):
                document, _ = LegalDocument.objects.update_or_create(
                    key=spec['key'], defaults={k: spec[k] for k in DOCUMENT_SETTINGS if k in spec},
                )
                for text in spec.get('versions', []):
                    _, created = LegalDocumentVersion.objects.get_or_create(
                        document=document, version=text['version'], language=text.get('language', 'en'),
                        defaults={**{k: text.get(k, '') for k in TEXT_FIELDS},
                                  'requires_reacceptance': text.get('requires_reacceptance', True),
                                  'published_at': _published_at(text, publish)},
                    )
                    added, kept = added + created, kept + (not created)
        self.stdout.write(f'{added} text version(s) added, {kept} already present and left unchanged.')
