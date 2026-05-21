from __future__ import annotations

from importlib import import_module

from django.conf import settings

from .base import TokenProvider

_providers: list[TokenProvider] | None = None


def get_providers() -> list[TokenProvider]:
    """Instantiate and cache the configured PARTNER_AUTH_PROVIDERS."""
    global _providers
    if _providers is not None:
        return _providers

    dotted_paths: list[str] = getattr(settings, "PARTNER_AUTH_PROVIDERS", [])
    result: list[TokenProvider] = []

    for path in dotted_paths:
        module_path, _, class_name = path.rpartition(".")
        module = import_module(module_path)
        cls = getattr(module, class_name)
        if not (isinstance(cls, type) and issubclass(cls, TokenProvider)):
            raise TypeError(f"{path} is not a TokenProvider subclass")
        result.append(cls())

    _providers = result
    return _providers
