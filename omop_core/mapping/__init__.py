"""The Mapping bounded context.

This package owns mapping use cases while the existing ``omop_core`` models
remain the system of record.  It intentionally has no Django app config,
models, or migrations: code, field, and therapy mappings have distinct
schemas and lifecycles, but belong behind one coherent application boundary.

Use the focused modules (``code_resolution``, ``suggestions``, ``field``, and
``therapy``) rather than importing from the legacy ``omop_core.services``
paths.  Those paths are compatibility shims for existing integrations.
"""
