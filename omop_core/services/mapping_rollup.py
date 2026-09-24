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

Ordering is by summed occurrence -- records retired per curated decision, which
is the question a curator is really answering.
"""
from django.db.models import CharField, Count, Max, Min, Q, Sum, Value
from django.db.models.functions import Cast, Coalesce, Concat

#: Separates a synthetic single-row key from a real label. Outside the
#: character set source_labels.normalise keeps, so the two cannot collide.
SYNTHETIC_PREFIX = ':'


def _group_key():
    return Coalesce(
        'source_label_norm',
        Concat(Value(SYNTHETIC_PREFIX), Cast('id', CharField()),
               output_field=CharField()),
        output_field=CharField(),
    )


def group_entries(queryset, page, page_size):
    """Return ``(entries, total_groups)`` for one section, ordered by Seen.

    ``entries`` are dicts, not model instances: a group has no single row
    behind it. A one-member group carries ``mapping_id`` so the client can
    render it exactly as it renders a row today.
    """
    grouped = (
        queryset.order_by()
        .annotate(group_key=_group_key())
        .values('group_key')
        .annotate(
            seen=Coalesce(Sum('occurrence_count'), Value(0)),
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
            statuses=Count('status', distinct=True),
            # Not named `status`: an annotation shadows the column of the same
            # name, so Q(status='proposed') below would resolve against
            # MAX(status) and Postgres refuses an aggregate inside a FILTER.
            only_status=Max('status'),
            # What a group write may touch: an approved or rejected member is
            # somebody's decision, not a gap.
            proposed=Count('pk', filter=Q(status='proposed')),
            # Compared against `members` in Python: Postgres refuses an
            # aggregate inside a FILTER, so a Case(When(members=1, ...)) here
            # will not compile.
            max_id=Max('id'),
        )
        .order_by('-seen', 'group_key')
    )
    total = grouped.count()
    start = (page - 1) * page_size
    entries = [
        {
            'label': entry['label'],
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
            'status': entry['only_status'] if entry['statuses'] == 1 else None,
            'mixed_statuses': entry['statuses'] > 1,
            'mapping_id': entry['max_id'] if entry['members'] == 1 else None,
        }
        for entry in grouped[start:start + page_size]
    ]
    return entries, total


def group_members(queryset, label):
    """The rows behind one entry: a real label, or ``:<pk>`` for a single row."""
    if label.startswith(SYNTHETIC_PREFIX):
        try:
            return queryset.filter(pk=int(label[len(SYNTHETIC_PREFIX):]))
        except ValueError:
            return queryset.none()
    return queryset.filter(source_label_norm=label)
