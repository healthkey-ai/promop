"""Field-mapping use cases over the existing PRomop field-mapping tables.

Field mappings are deliberately not coerced into source-code mappings.  This
module gives the Mapping component a stable home for their descriptor,
curation-transfer, and value-coercion capabilities while their established
models stay in :mod:`omop_core.models`.
"""

from omop_core.services.field_curation_transfer import (
    DEFAULT_TABLES,
    TABLES,
    TransferStats,
    apply_payload,
    read_payload,
)
from omop_core.services.field_descriptor import get_all_field_descriptors
from omop_core.services.field_write_service import coerce_assertion_value

__all__ = [
    'TransferStats',
    'DEFAULT_TABLES',
    'TABLES',
    'apply_payload',
    'coerce_assertion_value',
    'get_all_field_descriptors',
    'read_payload',
]
