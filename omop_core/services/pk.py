"""PostgreSQL sequence-based PK generation for OMOP tables.

Some legacy write paths (patient_portal/api/views.py, lot_inference_service,
omop_write_service) assign PKs as ``MAX(id)+1`` with an explicit value, which
does NOT advance the table's sequence. That strands the sequence behind the
table max, so a later ``nextval`` here would hand out an already-used id and
hit a duplicate-key error. To stay robust against those writers, every
allocation first self-heals the sequence to ``>= MAX(pk)`` before drawing.
"""
from django.db import connection

_SEQ_NAME_TEMPLATE = '{table}_{pk_field}_seq'


def _seq_name(model, pk_field):
    return _SEQ_NAME_TEMPLATE.format(
        table=model._meta.db_table, pk_field=pk_field,
    )


def _reseed_to_max(cur, model, pk_field, seq):
    """Advance ``seq`` so the next value exceeds both its current position and
    ``MAX(pk)`` — making sequence allocation immune to legacy MAX(id)+1 writers
    that bypass the sequence. (Identifiers come from model meta, not user input.)
    """
    table = connection.ops.quote_name(model._meta.db_table)
    col = connection.ops.quote_name(pk_field)
    qseq = connection.ops.quote_name(seq)
    cur.execute(
        f"SELECT setval(%s, GREATEST("
        f"(SELECT last_value FROM {qseq}), "
        f"(SELECT COALESCE(MAX({col}), 0) FROM {table})), true)",
        [seq],
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
