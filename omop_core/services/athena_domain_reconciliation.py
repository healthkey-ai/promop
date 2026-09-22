"""Check mapped standard destination domains against Athena's live website."""
from datetime import date

from django.db import transaction
from django.db.models import Q, Subquery

from omop_core.models import Concept, Domain, MappingDestinationCandidate, SourceCodeConceptMapping
from omop_core.services.athena_destinations import (
    ATHENA_URL, BrowserTransport, LookupFailure, active, concept_from_api,
)
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE

STALE_MAPPING_LIMIT = 50


def destination_snapshots(vocabulary, using='default'):
    """Include selected destinations and all candidate choices, once per concept."""
    mappings = SourceCodeConceptMapping.objects.using(using).filter(source_vocabulary_id=vocabulary)
    candidates = MappingDestinationCandidate.objects.using(using).filter(mapping_id__in=Subquery(mappings.values('pk')))
    return list(Concept.objects.using(using).filter(standard_concept='S').filter(
        Q(pk__in=Subquery(mappings.exclude(target_concept_id=None).values('target_concept_id')))
        | Q(pk__in=Subquery(candidates.exclude(target_concept_id=None).values('target_concept_id')))
    ).order_by('pk').values('concept_id', 'vocabulary_id', 'concept_code', 'domain_id', 'source'))


def external(snapshot):
    return not snapshot['source'] and 0 < snapshot['concept_id'] < 2_000_000_000


class AthenaDomainBrowser:
    """Visit concept pages and read the detail responses delivered by the web UI.

    A batch closes its browser before returning, so Playwright's event loop never
    overlaps synchronous Django ORM operations. No direct API fallback is used.
    """
    def __init__(self, *, interval=1, timeout=30, executable=None, storage_state=None):
        self.interval, self.timeout = interval, timeout
        self.executable, self.storage_state = executable, storage_state

    def lookup_many(self, concept_ids):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise LookupFailure('Install requirements-athena-scrape.txt and run playwright install chromium') from exc
        results = {}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.executable)
            try:
                context = browser.new_context(storage_state=self.storage_state)
                page = context.new_page()
                page.set_default_timeout(self.timeout * 1000)
                transport = BrowserTransport(page, self.interval)
                for concept_id in concept_ids:
                    try:
                        results[concept_id] = {'concept': concept_from_api(transport.get('/' + str(concept_id)))}
                    except Exception as exc:
                        # Browser/network/format failures are separate audit outcomes;
                        # none is interpreted as a missing concept or a domain match.
                        results[concept_id] = {'error': f'{type(exc).__name__}: {exc}'[:1000]}
            finally:
                browser.close()
        return results


def reconcile_domain(snapshot, result, *, using='default', apply=False):
    """Domain-only update after exact identity checks and a concurrent-edit check."""
    cid = snapshot['concept_id']
    receipt = dict(concept_id=cid, vocabulary_id=snapshot['vocabulary_id'], concept_code=snapshot['concept_code'],
                   local_domain=snapshot['domain_id'], athena_domain='', outcome='', reason='',
                   stale_mappings='', reference=f'{ATHENA_URL}/search-terms/terms/{cid}')
    def finish(outcome, reason=''):
        receipt.update(outcome=outcome, reason=reason)
        if outcome not in ('updated', 'would_update'):
            # Nothing was written, so nothing was left out of step. Naming rows
            # here would read as a correction having destabilised them.
            receipt['stale_mappings'] = ''
        return receipt
    if not external(snapshot):
        return finish('protected_local', 'Locally authored concept or non-external concept ID')
    if result.get('error') or 'concept' not in result:
        return finish('lookup_failed', result.get('error', 'Athena returned no concept evidence'))
    target = result['concept']
    if any(target.get(key) != snapshot[key] for key in ('concept_id', 'vocabulary_id', 'concept_code')):
        return finish('identity_conflict', 'Athena ID, vocabulary or code differs from the local identity')
    if target.get('standard_concept') != 'S':
        return finish('upstream_nonstandard', 'Athena no longer marks this destination standard; domain left unchanged')
    try:
        if not active(target, date.today()):
            return finish('upstream_inactive', 'Athena destination is not currently valid; domain left unchanged')
    except (ValueError, TypeError, KeyError, OverflowError):
        return finish('lookup_failed', 'Athena validity information is incomplete')
    domain = target.get('domain_id')
    if not isinstance(domain, str) or not domain.strip():
        return finish('lookup_failed', 'Athena domain is missing')
    receipt['athena_domain'] = domain
    if domain == snapshot['domain_id']:
        return finish('unchanged')
    if domain not in DOMAIN_TO_TABLE:
        # A destination in Device, Meas Value or Spec Anatomic Site is a real
        # Athena 'Maps to' target, but nothing downstream can route it: the
        # concept drops out of every per-domain partial index, so Suggest stops
        # returning it, and creating a mapping from it is rejected because the
        # form prefills domain_id from concept.domain_id. Report it instead.
        return finish('unsupported_domain',
                      f'Athena domain {domain!r} is outside the domains this application routes '
                      f'({", ".join(sorted(DOMAIN_TO_TABLE))}); domain left unchanged')
    if not Domain.objects.using(using).filter(pk=domain).exists():
        return finish('missing_domain', 'Official domain is absent from the local Domain reference table')
    # A mapping carries its own domain_id and omop_table, and clean() only checks
    # those two against each other — never against target_concept.domain_id. So a
    # domain correction here silently leaves the mapping routing facts to the old
    # table. Naming the rows is the least this can do; deciding whether to rewrite
    # them (and what to do with facts already written) is a curation question.
    # A blank domain_id is unset, not divergent, so it is not reported.
    diverging = (SourceCodeConceptMapping.objects.using(using)
                 .filter(target_concept_id=cid).exclude(domain_id__in=('', domain)).order_by('pk'))
    stale = list(diverging.values_list('pk', flat=True)[:STALE_MAPPING_LIMIT])
    # Truncation has to be visible: a receipt that silently drops rows reads as
    # a complete remediation list and leaves the rest inconsistent.
    hidden = (diverging.count() - len(stale)) if len(stale) == STALE_MAPPING_LIMIT else 0
    receipt['stale_mappings'] = ' '.join(str(pk) for pk in stale) + (f' +{hidden} more' if hidden else '')
    if not apply:
        return finish('would_update')
    with transaction.atomic(using=using):
        current = Concept.objects.using(using).select_for_update().filter(pk=cid).first()
        if current is None:
            return finish('changed_during_lookup', 'Destination was removed during the lookup')
        if (current.source or current.standard_concept != 'S'
                or any(getattr(current, key) != snapshot[key] for key in ('vocabulary_id', 'concept_code'))):
            return finish('changed_during_lookup', 'Destination identity, provenance or standard status changed')
        if current.domain_id == domain:
            return finish('unchanged', 'Domain already corrected during the lookup')
        if current.domain_id != snapshot['domain_id']:
            return finish('changed_during_lookup', 'Local domain changed during the lookup; rerun to recheck')
        Concept.objects.using(using).filter(pk=cid).update(domain_id=domain)
    return finish('updated')
