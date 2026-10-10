"""Data-source plug-ins. Every module in this package is loaded, so adding a source is adding one file.

See base.py for the contract and link.py for the simplest example.
"""
import importlib
import pkgutil

from .base import (REGISTRY, Field, Source, SourceError, catalog, clean_settings, fetch_source, flatten,
                   public_settings, register, try_connection)

for _module in pkgutil.iter_modules(__path__):
    if _module.name != "base":
        importlib.import_module(f"{__name__}.{_module.name}")


def type_of(kind: str, settings: dict) -> str:
    """What a source is, for display: "PostgreSQL", "MongoDB", "File"... (a Database source is told by its URL)."""
    from .database import type_label
    if kind == "database":
        return type_label(settings.get("url", ""))
    return REGISTRY[kind].label if kind in REGISTRY else kind

__all__ = ["REGISTRY", "Field", "Source", "SourceError", "catalog", "clean_settings", "fetch_source", "flatten",
           "public_settings", "register", "try_connection", "type_of"]
