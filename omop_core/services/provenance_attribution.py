"""Which hospital a clinical row came from, read off its provenance (#1690).

The same local code can mean different things at two hospitals, so rows
attributed to different hospitals are never treated as one event.
"""

from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.expressions import ArraySubquery
from django.db.models import Exists, OuterRef

from omop_core.models import ProvenanceRecord

ATTRIBUTION = 'attribution'

OWN, UNATTRIBUTED, FOREIGN = 'own', 'unattributed', 'foreign'


def _provenance_of(model):
    return ProvenanceRecord.objects.filter(
        content_type=ContentType.objects.get_for_model(model),
        object_id=OuterRef('pk'),
    )


def with_attribution(qs):
    """Annotate each row with the sorted organization ids on its provenance."""
    organizations = (
        _provenance_of(qs.model).exclude(organization_id=None)
        .order_by('organization_id').values('organization_id').distinct()
    )
    return qs.annotate(**{ATTRIBUTION: ArraySubquery(organizations)})


def classify(attribution, organization_id):
    if not attribution:
        return UNATTRIBUTED
    if set(attribution) == {organization_id}:
        return OWN
    return FOREIGN


def same_attribution(pks, attribution_by_pk):
    """The rows that share the first row's attribution, first row included."""
    keep = attribution_by_pk[pks[0]]
    return [pk for pk in pks if attribution_by_pk[pk] == keep]


def exclusively_attributed(qs, organization_id):
    provenance = _provenance_of(qs.model)
    return qs.filter(
        Exists(provenance.filter(organization_id=organization_id)),
    ).exclude(
        Exists(provenance.exclude(organization_id=None).exclude(organization_id=organization_id)),
    )
