"""One review row per label, instead of one per vendor code.

A vendor-local code is opaque and the same display text arrives under thousands
of them: in a 721,620-code HealthTree extract ``albumin`` appeared under 2,557
codes, ``glucose`` under 2,032. A per-code queue asks the curator the same
question 2,557 times, and -- worse -- scatters the volume so the queue cannot
be ordered usefully. Albumin's largest single row sits at rank #393 carrying
1.6% of its real weight, while ``hourly rounding bundle`` leads the page
because it happens to have one dominant code.

Grouping on :mod:`omop_core.services.source_labels`'s key fixes both. The first
100 entries of a grouped EPIC tab cover 77,356 vendor codes and 5,633,802
records, against 100 codes today.

**Every entry is a group, including the ones with a single member.** A row whose
label is absent, blank, punctuation-only or stored at the column limit has
``source_label_norm IS NULL`` and belongs to no group -- but SQL groups NULLs
together, which would offer one entry standing for 1,929 unrelated codes,
mappable in a single click. Each such row therefore gets a synthetic key of its
own, ``:<pk>``. The ``:`` is deliberate: it is outside the ``[a-z0-9%#]`` set a
real key is built from, so a synthetic key can never collide with a label.

The review queue can order groups by summed occurrence or by their displayed
description. Ordering happens here, before pagination, so pages remain stable.
"""
from django.db.models import Case, CharField, Count, F, Max, Min, Q, Sum, Value, When

from django.db.models.functions import Cast, Coalesce, Concat, Lower

#: Separates a synthetic single-row key from a real label. Outside the
#: character set source_labels.normalise keeps, so the two cannot collide.
SYNTHETIC_PREFIX = ':'

# Group rows do not have most of a source-code row's sortable fields. These
# are the two group-level values the review table can honestly order by.
GROUP_ORDER_FIELDS = {
    'occurrence_count': 'seen',
    'source_code_description': 'description_sort',
}


def _group_key():
    return Case(
        When(source_label_norm__isnull=True, then=Concat(
            Value(SYNTHETIC_PREFIX), Cast('id', CharField()))),
        default=F('source_label_norm'),
        output_field=CharField(),
    )


def _grouped(queryset):
    return (
        queryset.order_by()
        .annotate(group_key=_group_key())
        .values('group_key')
        .annotate(
            summed_seen=Sum('occurrence_count'),
            supplied_group_seen=Max('source_group_occurrence_count'),
            members=Count('pk'),
            label=Max('source_label_norm'),
            # Any member's spelling will do as a heading: they differ only by
            # the case and punctuation normalisation removed. Min keeps it
            # deterministic across requests.
            description=Min('source_code_description'),
            # NULL is a distinct answer here -- "no destination" against "this
            # concept" is exactly the disagreement the caller must see -- and
            # Count(distinct) skips NULLs, so fold them onto 0 first.
            destinations=Count(Coalesce('target_concept_id', Value(0)), distinct=True),
            destination=Max('target_concept_id'),
            # Only meaningful when the group is unanimous, which `destinations`
            # decides; Max picks the single value in that case.
            destination_name=Max('target_concept__concept_name'),
            destination_code=Max('target_concept__concept_code'),
            destination_vocabulary=Max('target_concept__vocabulary_id'),
            destination_domain=Max('target_concept__domain_id'),
            # The curated unit follows the destination's rule: "no unit"
            # against "this unit" is a disagreement, so NULL folds onto 0.
            # Only the id is aggregated: its code and name are read for the
            # page's own groups afterwards, not joined across the section.
            destination_units=Count(Coalesce('destination_unit_concept_id', Value(0)), distinct=True),
            destination_unit=Max('destination_unit_concept_id'),
            statuses=Count('status', distinct=True),
            # Not named `status`: an annotation shadows the column of the same
            # name, so Q(status='proposed') below would resolve against
            # MAX(status) and Postgres refuses an aggregate inside a FILTER.
            only_status=Max('status'),
            suggested_actions=Count('suggested_action', distinct=True),
            suggested_action=Max('suggested_action'),
            # What a group write may touch: an approved or rejected member is
            # somebody's decision, not a gap.
            proposed=Count('pk', filter=Q(status='proposed')),
            # Compared against `members` in Python: Postgres refuses an
            # aggregate inside a FILTER, so a Case(When(members=1, ...)) here
            # will not compile.
            max_id=Max('id'),
            # The most LOINC axis variants any member's units imply: a group
            # holding one such code needs the same blue flag the row has.
            axis_variant_count=Max('axis_variant_count'),
        )
        .annotate(
            seen=Coalesce('supplied_group_seen', 'summed_seen', Value(0)),
            description_sort=Lower('description'),
        )
    )


def count_groups(queryset):
    """How many entries a section holds, for pagination."""
    return _grouped(queryset).count()


def group_entries(queryset, page, page_size, order='-occurrence_count'):
    """One ordered page of entries for a section.

    Entries are dicts, not model instances: a group has no single row behind
    it. A one-member group carries ``mapping_id`` so the client can act on it
    the way it acts on a row.
    """
    field = GROUP_ORDER_FIELDS.get(order.lstrip('-'))
    if field is None:
        raise ValueError('Unknown group sort column.')
    ordering = (F(field).desc(nulls_last=True) if order.startswith('-')
                else F(field).asc(nulls_last=True))
    grouped = _grouped(queryset).order_by(ordering, 'group_key')
    start = (page - 1) * page_size
    from omop_core.models import Concept
    from omop_core.services.concept_unit_info import suggested_unit

    page_entries = list(grouped[start:start + page_size])
    # One query for the page's unanimous units, whatever the number of groups.
    # An id whose concept is not loaded has no entry and reads as no unit.
    unit_ids = {entry['destination_unit'] for entry in page_entries
                if entry['destination_units'] == 1 and entry['destination_unit']}
    units = Concept.objects.only('concept_code', 'concept_name').in_bulk(unit_ids) if unit_ids else {}

    def unanimous_unit(entry):
        return units.get(entry['destination_unit']) if entry['destination_units'] == 1 else None

    return [
        {
            'label': entry['label'],
            'sample_mapping_id': entry['max_id'],
            'group_key': entry['group_key'],
            'description': entry['description'],
            'members': entry['members'],
            'seen': entry['seen'],
            'proposed': entry['proposed'],
            # A group speaks with one voice only when every member agrees.
            'destination_concept_id': entry['destination'] if entry['destinations'] == 1 else None,
            'destination_concept_name': entry['destination_name'] if entry['destinations'] == 1 else None,
            'destination_concept_code': entry['destination_code'] if entry['destinations'] == 1 else None,
            'destination_vocabulary_id': entry['destination_vocabulary'] if entry['destinations'] == 1 else None,
            'mixed_destinations': entry['destinations'] > 1,
            # The unit the members' values will be stored in, when they agree.
            'destination_unit_code': getattr(unanimous_unit(entry), 'concept_code', ''),
            'destination_unit_name': getattr(unanimous_unit(entry), 'concept_name', ''),
            'mixed_destination_units': entry['destination_units'] > 1,
            # What a unanimous LOINC Measurement destination is usually
            # reported in; shown only where no unit is curated.
            'suggested_unit': suggested_unit(
                entry['destination_vocabulary'], entry['destination_domain'],
                entry['destination_code'],
            ) if entry['destinations'] == 1 and entry['destination'] is not None else '',
            'status': entry['only_status'] if entry['statuses'] == 1 else None,
            'mixed_statuses': entry['statuses'] > 1,
            'suggested_action': (
                entry['suggested_action'] if entry['suggested_actions'] == 1 else ''
            ),
            'mapping_id': entry['max_id'] if entry['members'] == 1 else None,
            'axis_variant_count': entry['axis_variant_count'] or 0,
        }
        for entry in page_entries
    ]


def group_members(queryset, label):
    """The rows behind one entry: a real label, or ``:<pk>`` for a single row."""
    if label.startswith(SYNTHETIC_PREFIX):
        try:
            pk = int(label[len(SYNTHETIC_PREFIX):])
        except ValueError:
            return queryset.none()
        # A pk past bigint reaches Postgres as an out-of-range comparison and
        # raises DataError -- a 500 for what is only an unknown group.
        if not 0 < pk <= 9223372036854775807:
            return queryset.none()
        return queryset.filter(pk=pk)
    return queryset.filter(source_label_norm=label)
