"""A deliberate refusal of a patient-field write, worded for the reader (#2489).

A writer that refuses a bad value raises :class:`WriteRefused` with a sentence
built from the field name and the submitted input. The shared-form PATCH turns
only this into its 400 message. Any other ``ValueError`` from a step of the
write is internal: it is logged without values and the client gets a generic
answer, so an exception's own text never reaches a response (code scanning
`py/stack-trace-exposure`).

It subclasses ``ValueError`` so every caller that already catches
``ValueError`` (the native serializer, the persons endpoint) is unchanged.
"""


class WriteRefused(ValueError):
    """The writer refused the input. The message is safe to show the caller."""
