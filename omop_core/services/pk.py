"""PostgreSQL sequence-based PK generation for OMOP tables."""
from django.db import connection

_SEQ_NAME_TEMPLATE = '{table}_{pk_field}_seq'


def _seq_name(model, pk_field):
    return _SEQ_NAME_TEMPLATE.format(
        table=model._meta.db_table, pk_field=pk_field,
    )


def next_pk(model, pk_field):
    """Return the next PK value from the table's sequence."""
    seq = _seq_name(model, pk_field)
    with connection.cursor() as cur:
        cur.execute("SELECT nextval(%s)", [seq])
        return cur.fetchone()[0]


def next_pk_batch(model, pk_field, count):
    """Return a list of *count* consecutive PK values from the table's sequence."""
    if count <= 0:
        return []
    seq = _seq_name(model, pk_field)
    with connection.cursor() as cur:
        cur.execute(
            "SELECT nextval(%s) FROM generate_series(1, %s)",
            [seq, count],
        )
        return [row[0] for row in cur.fetchall()]
