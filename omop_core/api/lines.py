"""Structured per-line therapy history, shared by the in-place API and an embedding host.

Both the standalone patient-record payload and a host's federated projection need
the same list: line number, regimen and concept id with its provenance, component
and class ids, dates, outcome, intent, discontinuation reason, the line's own
administrations, and whether its composition is known. Keeping one builder stops
the two surfaces from drifting (the reason concepts and regimens were moved here).
"""
from omop_core.models import DrugExposure
from omop_oncology.models import EpisodeEvent, TherapyLineExposure


def lines_of_therapy(obj, concept_cache=None):
    """Structured per-line therapy history assembled from the flat
    first/second/later_* read-model fields (promop#249).

    Each entry: line number, regimen text + HemOnc concept_id (with its
    `regimen_source` and `release_id` from `therapy_ids_provenance`, falling
    back to `regimen_source='inferred'` for a resolved concept_id until
    provenance is recorded), component concept_ids, dates (ISO strings),
    outcome, intent, and discontinuation reason. 3L+ lines are emitted
    one-per `later_therapies` entry (the authoritative per-line list, which
    includes lines whose regimen did not resolve to a concept_id), each
    naming its own regimen and carrying its own concept_id (may be null) and
    dates; the `component_ids`/outcome remain the aggregate `later_*` values
    (not per-later-line), flagged with `later_aggregate: true`. Older records
    derived before `later_therapies` carried per-line concept_ids fall back
    to one entry per resolved `later_therapy_ids` id.
    """
    prov = obj.therapy_ids_provenance if isinstance(obj.therapy_ids_provenance, dict) else {}

    def _prov(field, key):
        p = prov.get(field)
        return p.get(key) if isinstance(p, dict) else None

    def _iso(v):
        # DateField values load from the DB as date objects, but an
        # in-memory instance may carry a raw string (Django does not coerce
        # on attribute assignment). Emit ISO either way, never crash.
        return v.isoformat() if hasattr(v, 'isoformat') else (v or None)

    # The lines here mirror the flat fields above, so they are the primary
    # cancer's (#1739); another cancer's line 1 must not replace them.
    from omop_core.services.disease_episodes import primary_lines
    episodes_by_line = {
        e.episode_number: e
        for e in primary_lines(obj.person, obj.disease_slug or '').filter(episode_number__isnull=False)
    }
    episode_ids = [e.episode_id for e in episodes_by_line.values()]
    event_ids_by_episode = {}
    if episode_ids:
        from omop_core.services.mappings import CONCEPT_DRUG_EXPOSURE_FIELD
        for ee in EpisodeEvent.objects.filter(episode_id__in=episode_ids,
                episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD):
            event_ids_by_episode.setdefault(ee.episode_id, []).append(ee.event_id)
    all_event_ids = [
        event_id
        for event_ids in event_ids_by_episode.values()
        for event_id in event_ids
    ]
    drugs_by_id = {
        de.drug_exposure_id: de
        for de in DrugExposure.objects.filter(
            person=obj.person, is_erroneous=False, drug_exposure_id__in=all_event_ids,
        ).select_related(
            'drug_concept',
            'drug_concept__vocabulary',
            'drug_concept__concept_class',
        )
    } if all_event_ids else {}

    # Bulk-fetch maintenance flags so the dialog can show/preserve them.
    maint_exposure_ids = set(
        TherapyLineExposure.objects
        .filter(episode_id__in=episode_ids, is_maintenance=True)
        .values_list('drug_exposure_id', flat=True)
    ) if episode_ids else set()

    def _editable_drugs(line_number):
        episode = episodes_by_line.get(line_number)
        if episode is None:
            return None, [], False
        drugs = []
        for event_id in event_ids_by_episode.get(episode.episode_id, []):
            de = drugs_by_id.get(event_id)
            if de is None or de.drug_concept is None:
                continue
            concept = de.drug_concept
            drugs.append({
                'drug_exposure_id': de.pk,
                'start_date': _iso(de.drug_exposure_start_date),
                'end_date': _iso(de.drug_exposure_end_date),
                'concept_id': concept.concept_id,
                'concept_name': concept.concept_name,
                'concept_code': concept.concept_code,
                'vocabulary_id': concept.vocabulary_id,
                'concept_class_id': concept.concept_class_id,
                'standard_concept': concept.standard_concept,
                'source_value': de.drug_source_value,
                'maintenance': de.pk in maint_exposure_ids,
            })
        # An asserted line may have no recorded drug at all (#1924). Say so
        # explicitly, so a consumer does not read "no drugs listed" as "no
        # drugs were given".
        return episode.episode_id, drugs, bool(event_ids_by_episode.get(episode.episode_id))

    def _line(n, regimen, cid, prov_field, comp, start, end,
              outcome, intent, disc, progression_date=None,
              later_aggregate=False, origin_override=None, comp_class=None):
        # Prefer a per-line origin (later_therapies carries one per 3L+ line);
        # else the field-level `therapy_ids_provenance` origin.
        origin = origin_override or _prov(prov_field, 'origin')
        # Provenance is populated by the derivation pipeline for refreshed
        # rows. For a row derived before provenance existed, fall back to
        # 'inferred' for a resolved regimen rather than a misleading `null` —
        # never 'asserted', so a consumer may trust 'asserted' but must
        # verify 'inferred'.
        if not cid:
            # No resolved regimen concept_id → regimen_source is meaningless;
            # a null-id line must NOT inherit an aggregate 'asserted'/'inferred'
            # (e.g. an unresolved 3L+ line under an all-asserted later set).
            origin = None
        elif origin is None:
            origin = 'inferred'
        episode_id, editable_drugs, composition_known = _editable_drugs(n)
        entry = {
            'line': n,
            'episode_id': episode_id,
            'regimen': regimen,
            'regimen_concept_id': cid,
            'regimen_source': origin,
            'release_id': _prov(prov_field, 'release_id'),
            'drugs': editable_drugs,
            'composition_known': composition_known,
            'component_ids': comp or [],
            # Therapy-class ("type") concept_ids for the line (ADR 0002),
            # derived from component_ids; parity with the flat
            # *_therapy_type_ids fields. For 3L+ lines this is the shared
            # later-line aggregate (like component_ids), flagged by
            # later_aggregate — an individual later line may show classes
            # from a sibling later line.
            'type_ids': comp_class or [],
            # ISO strings on the wire, consistent with the flat *_date fields
            # (DRF DateField); avoids raw date objects leaking to consumers
            # that json.dumps the payload themselves.
            'start_date': _iso(start),
            'end_date': _iso(end),
            'outcome': outcome,
            'intent': intent,
            'discontinuation_reason': disc,
            'progression_date': _iso(progression_date),
        }
        if later_aggregate:
            entry['later_aggregate'] = True
        return entry

    lines = []
    if obj.first_line_therapy or obj.first_line_therapy_id:
        lines.append(_line(
            1, obj.first_line_therapy, obj.first_line_therapy_id,
            'first_line_therapy_id', obj.first_line_component_ids,
            obj.first_line_start_date or obj.first_line_date, obj.first_line_end_date,
            obj.first_line_outcome, obj.first_line_intent,
            obj.first_line_discontinuation_reason,
            progression_date=obj.first_line_progression_date,
            comp_class=obj.first_line_therapy_type_ids))
    if obj.second_line_therapy or obj.second_line_therapy_id:
        lines.append(_line(
            2, obj.second_line_therapy, obj.second_line_therapy_id,
            'second_line_therapy_id', obj.second_line_component_ids,
            obj.second_line_start_date or obj.second_line_date, obj.second_line_end_date,
            obj.second_line_outcome, obj.second_line_intent,
            obj.second_line_discontinuation_reason,
            progression_date=obj.second_line_progression_date,
            comp_class=obj.second_line_therapy_type_ids))

    # 3L+ lines: iterate the authoritative per-line `later_therapies` list,
    # which includes lines whose regimen did not resolve to a concept_id, so
    # no later line is dropped. Each entry names its own regimen and carries
    # its own line number, concept_id, and dates; component_ids/outcome
    # remain the aggregate `later_*` values (flagged later_aggregate).
    later_therapies = [lt for lt in (obj.later_therapies or []) if isinstance(lt, dict)]
    # Only the new shape carries a per-line `concept_id` key. Records derived
    # before this change have `later_therapies` entries without that key and
    # with resolved ids in the separate `later_therapy_ids` list.
    has_per_line_ids = any('concept_id' in lt for lt in later_therapies)
    if later_therapies:
        if has_per_line_ids:
            # New shape: line number and id are authoritative per entry.
            aligned_ids = [lt.get('concept_id') for lt in later_therapies]
            line_nums = [lt.get('lineNumber') for lt in later_therapies]
        else:
            # Legacy shape: no per-line id/line. Preserve the per-line
            # entries (count, names, dates); align resolved ids positionally
            # only when every later line resolved (equal counts) — otherwise
            # the subset in later_therapy_ids can't be mapped to a line.
            later_ids = obj.later_therapy_ids or []
            aligned_ids = (later_ids if len(later_ids) == len(later_therapies)
                           else [None] * len(later_therapies))
            line_nums = [None] * len(later_therapies)
        for i, lt in enumerate(later_therapies):
            line_no = line_nums[i] if line_nums[i] is not None else (3 + i)
            lines.append(_line(
                line_no, lt.get('therapy') or obj.later_therapy,
                aligned_ids[i], 'later_therapy_ids',
                obj.later_component_ids, lt.get('startDate'), lt.get('endDate'),
                obj.later_outcome, obj.later_intent,
                obj.later_discontinuation_reason,
                progression_date=lt.get('progression_date'),
                later_aggregate=True,
                origin_override=lt.get('origin'),
                comp_class=obj.later_therapy_type_ids))
    else:
        # Oldest rows with no `later_therapies` list at all: emit one entry
        # per resolved id (naming each from the concept cache), else a single
        # aggregate entry from the flat `later_therapy` text.
        later_ids = obj.later_therapy_ids or []
        if later_ids:
            cache = concept_cache or {}
            for i, cid in enumerate(later_ids):
                c = cache.get(cid)
                regimen_name = c.concept_name if c else obj.later_therapy
                lines.append(_line(
                    3 + i, regimen_name, cid, 'later_therapy_ids',
                    obj.later_component_ids, obj.later_start_date or obj.later_date, obj.later_end_date,
                    obj.later_outcome, obj.later_intent,
                    obj.later_discontinuation_reason,
                    later_aggregate=True,
                    comp_class=obj.later_therapy_type_ids))
        elif obj.later_therapy:
            lines.append(_line(
                3, obj.later_therapy, None, 'later_therapy_ids',
                obj.later_component_ids, obj.later_start_date, obj.later_end_date,
                obj.later_outcome, obj.later_intent,
                obj.later_discontinuation_reason,
                later_aggregate=True,
                comp_class=obj.later_therapy_type_ids))
    return lines
