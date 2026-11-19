"""Check the deployment's record mappings, or apply them to submitted responses.

    python manage.py apply_record_mappings --check
    python manage.py apply_record_mappings --response <uuid>
    python manage.py apply_record_mappings --survey <slug> [--since 2026-11-01]

A response is applied when it is submitted (patient_portal.survey_record_mapping);
this command is for one that was submitted before its mapping existed, or whose
write failed. Applying a response again writes the same values.
"""
import datetime as dt

from django.core.management.base import BaseCommand, CommandError

from patient_portal import survey_record_mapping as srm


class Command(BaseCommand):
    help = 'Check record mappings against their survey versions, or apply them to submitted responses.'

    def add_arguments(self, parser):
        parser.add_argument('--check', action='store_true',
                            help='Load every mapping and check it against its survey versions; write nothing.')
        parser.add_argument('--response', help='Apply the mapping to this submitted response.')
        parser.add_argument('--survey', help="Apply the mapping to this survey's submitted responses.")
        parser.add_argument('--since', type=dt.date.fromisoformat, help='With --survey: submitted on or after (YYYY-MM-DD).')

    def handle(self, *args, **options):
        from prolog_surveys.models import ResponseStatus, SurveyResponse, SurveyVersion

        srm.reset_cache()
        try:
            loaded = srm.mappings()
        except (srm.MappingError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        if options['check']:
            problems = []
            for (slug, version), mapping in sorted(loaded.items()):
                found = SurveyVersion.objects.filter(survey__slug=slug, version=version).first()
                if found is None:
                    problems.append(f'{slug}@{version}: not loaded')
                    continue
                problems += [f'{slug}@{version} {p}' for p in srm.check_against_definition(mapping, found.definition)]
            for problem in problems:
                self.stderr.write(problem)
            if problems:
                raise CommandError(f'{len(problems)} problem(s) in the record mappings')
            self.stdout.write(f'OK: {len(loaded)} mapped survey version(s)')
            return

        responses = SurveyResponse.objects.filter(status=ResponseStatus.SUBMITTED).select_related('survey_version__survey')
        if options['response']:
            responses = responses.filter(pk=options['response'])
        elif options['survey']:
            responses = responses.filter(survey_version__survey__slug=options['survey'])
            if options['since']:
                responses = responses.filter(submitted_at__date__gte=options['since'])
        else:
            raise CommandError('give --check, --response or --survey')
        written = 0
        for response in responses.order_by('submitted_at'):
            applied = srm.apply_response(response)
            if applied is not None:
                written += 1
                self.stdout.write(f'{applied.response_id}: fields {applied.fields}, lines {applied.lines}')
        self.stdout.write(f'{written} response(s) written to the record')
