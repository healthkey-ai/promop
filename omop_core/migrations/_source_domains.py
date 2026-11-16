"""The domain-by-table map 0192 backfilled, frozen for the migration (#2446).

`omop_core.services.source_vocabularies` is live code: a future edit must not
change what a historical migration writes, and importing the service during a
fresh migrate couples the graph to today's module imports. The mapping is
small enough to keep here.
"""

DOMAIN_BY_TABLE = {
    'drug_exposure': 'Drug',
    'procedure': 'Procedure',
    'condition': 'Condition',
    'observation': 'Observation',
    'measurement': 'Measurement',
}
