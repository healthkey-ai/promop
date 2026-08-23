"""PostgreSQL sequence-based PK generation for OMOP tables.

Some legacy write paths (patient_portal/api/views.py, lot_inference_service)
assign PKs as ``MAX(id)+1`` with an explicit value, which
does NOT advance the table's sequence. That strands the sequence behind the
table max, so a later ``nextval`` here would hand out an already-used id and
hit a duplicate-key error. To stay robust against those writers, every
allocation first self-heals the sequence to ``>= MAX(pk)`` before drawing.
"""
from django.db import connection

# OMOP CDM reserves concept_id >= 2,000,000,000 for local (site-specific) use.
# All HealthKey-minted concepts must live in this range so they never collide
# with a future Athena vocabulary release.  Issue #425.
LOCAL_CONCEPT_MIN_ID = 2_000_000_000

_SEQ_NAME_TEMPLATE = '{table}_{pk_field}_seq'


def _seq_name(model, pk_field):
    return _SEQ_NAME_TEMPLATE.format(
        table=model._meta.db_table, pk_field=pk_field,
    )


def _reseed_to_max(cur, model, pk_field, seq):
    """Advance ``seq`` so the next value exceeds both its current position and
    ``MAX(pk)`` — making sequence allocation immune to legacy MAX(id)+1 writers
    that bypass the sequence. (Identifiers come from model meta, not user input.)

    For ``concept.concept_id`` an additional floor of ``LOCAL_CONCEPT_MIN_ID``
    is applied so that HealthKey-minted concepts always land in the OMOP-reserved
    local range (>= 2,000,000,000) and never collide with Athena-assigned IDs.
    """
    table = connection.ops.quote_name(model._meta.db_table)
    col = connection.ops.quote_name(pk_field)
    qseq = connection.ops.quote_name(seq)

    # For Concept.concept_id, enforce the local-range floor.
    floor = (
        LOCAL_CONCEPT_MIN_ID
        if model._meta.db_table == 'concept' and pk_field == 'concept_id'
        else 0
    )

    cur.execute(
        f"SELECT setval(%s, GREATEST("
        f"%s, "
        f"(SELECT last_value FROM {qseq}), "
        f"(SELECT COALESCE(MAX({col}), 0) FROM {table})), true)",
        [seq, floor],
    )


def next_pk(model, pk_field):
    """Return the next PK value from the table's sequence."""
    seq = _seq_name(model, pk_field)
    with connection.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '5s'")
        _reseed_to_max(cur, model, pk_field, seq)
        cur.execute("SELECT nextval(%s)", [seq])
        val = cur.fetchone()[0]
        # Reset the timeout so subsequent queries in the same transaction
        # are not subject to the 5-second limit imposed above.
        cur.execute("SET LOCAL statement_timeout = '0'")
        return val


def next_pk_batch(model, pk_field, count):
    """Return a list of *count* consecutive PK values from the table's sequence."""
    if count <= 0:
        return []
    seq = _seq_name(model, pk_field)
    with connection.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '5s'")
        _reseed_to_max(cur, model, pk_field, seq)
        cur.execute(
            "SELECT nextval(%s) FROM generate_series(1, %s)",
            [seq, count],
        )
        rows = cur.fetchall()
        cur.execute("SET LOCAL statement_timeout = '0'")
        return [row[0] for row in rows]
