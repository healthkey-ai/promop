"""Validation messages we wrote ourselves, returned to the caller as a 400.

A few services refuse input with an exception whose message is meant for the
person who sent it (``ScopeError``, ``SetError``, ``SubmissionError``, a
``ValueError`` from an override validator, ...). A view used to catch it and
put ``str(exc)`` in the response, which reads like leaking an exception to
anything that cannot tell these from a stack trace. ``public_errors`` names the
exact exception types whose text is a public message, and turns only those
into ``PublicError``, which DRF renders as a 400. Any other exception is
untouched and stays a 500 with nothing exposed.
"""
from rest_framework.exceptions import ValidationError


class PublicError(ValidationError):
    """A 400 whose message is ours to show: ``{field: message}``, or a bare
    message (a list, as a serializer reports it) when no field is given."""

    def __init__(self, message, field=None):
        super().__init__({field: message} if field else message)


class public_errors:
    """``with public_errors(ScopeError, field='diseases'): ...``"""

    def __init__(self, *types, field=None):
        self.types = types
        self.field = field

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None and issubclass(exc_type, self.types):
            raise PublicError(str(exc_value), self.field) from None
        return False
