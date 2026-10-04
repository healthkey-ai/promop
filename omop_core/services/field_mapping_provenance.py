"""Who or what supplied a FieldConceptMapping (#1719).

Mirrors Code Mapping's provenance: a machine proposal names its engine and
version, a curator's unapproved edit is ``curator``, and approval stamps the
approver -- the person who authorised the mapping in force.
"""

PROVENANCE_MAX = 100
CURATOR_PROVENANCE = 'curator'
# Legacy value from before engines were versioned; never written any more.
LEGACY_SYSTEM_PROVENANCE = 'system_generated'

# Bump when the field-suggestion rules change, so a proposal says which rules
# produced it.
FIELD_SUGGESTION_VERSION = 'v1'
FIELD_SUGGESTION_PROVENANCE = f'field-suggest {FIELD_SUGGESTION_VERSION}'


def suggestion_provenance(mode):
    """A proposal from the field-suggestion engine, e.g. ``field-suggest v1 (reviewed)``."""
    return f'{FIELD_SUGGESTION_PROVENANCE} ({mode})'[:PROVENANCE_MAX]


def genomics_catalog_provenance(version):
    return f'genomics-catalog v{version}'


def user_provenance(user):
    """The approver, as Code Mapping records one: email, else name, else id."""
    label = (getattr(user, 'email', '') or getattr(user, 'name', '')
             or (str(user.pk) if getattr(user, 'pk', None) is not None else ''))
    return label[:PROVENANCE_MAX] or CURATOR_PROVENANCE
