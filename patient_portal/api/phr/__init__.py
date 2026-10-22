"""Read model for the patient's own Personal Health Record (``/api/v1/phr/``).

Each endpoint returns one section of the record, shaped for display: every
item carries its source (record or patient-reported, facility, date), empty
values are omitted rather than sent as blanks, and nothing here writes.
"""
