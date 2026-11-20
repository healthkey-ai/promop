"""Curator-facing hospital context for an exact incoming FHIR code system.

Facility evidence is observed per source code, not a registry that assigns an
OID to one hospital. Keep the raw Coding.system as the mapping identity.
"""

from omop_core.services.source_vocabularies import hospital_vendor


TRUSTED_LEVELS = frozenset({'site', 'brand', 'tenant'})


def source_system_context(mapping):
    """Return a compact display hint without promoting an alias to identity."""
    if mapping is None:
        return {'kind': 'none', 'label': '', 'names': [], 'name_count': 0}

    if mapping.organization_id:
        return {
            'kind': 'verified', 'label': mapping.organization.name,
            'names': [], 'name_count': 0,
        }

    if not hospital_vendor(mapping.source_vocabulary_id):
        return {'kind': 'none', 'label': '', 'names': [], 'name_count': 0}

    metadata = mapping.source_metadata or {}
    facilities = metadata.get('facilities', []) if isinstance(metadata, dict) else []
    names = []
    seen = set()
    for facility in facilities if isinstance(facilities, list) else []:
        if not isinstance(facility, dict):
            continue
        name = str(facility.get('name') or '').strip()
        if (facility.get('confidence') != 'high'
                or facility.get('level') not in TRUSTED_LEVELS
                or not name or name.casefold() in seen):
            continue
        seen.add(name.casefold())
        names.append(name)

    if not names:
        return {'kind': 'none', 'label': '', 'names': [], 'name_count': 0}
    if len(names) == 1:
        return {'kind': 'observed', 'label': names[0], 'names': names, 'name_count': 1}
    return {
        'kind': 'multiple', 'label': 'Multiple observed facilities',
        'names': names[:4], 'name_count': len(names),
    }
