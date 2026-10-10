"""The plug-in contract: every data source, database or not, answers one question - "give me your data as a table".

A plug-in is a class with a `kind`, the `fields` its setup form asks for, a `test()` that proves the connection
works, and a `fetch()` that returns a pandas DataFrame. Decorate it with `@register` and put it in this package;
it then appears in the dashboard's "Add source" menu automatically. Everything after `fetch()` (mapping to the
project's standard format, checks, AI) is shared, so a plug-in never deals with any of it.
"""
from __future__ import annotations

import importlib.util
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, ClassVar, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

import pandas as pd

from ..errors import DataPulseError
from ..io import normalize_frame


class SourceError(DataPulseError):
    """A source that can't be reached or read; the message is shown to the user."""


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    placeholder: str = ""
    secret: bool = False      # stored on the server only; never sent back to the browser
    required: bool = True
    multiline: bool = False
    help: str = ""
    input: str = "text"       # "text", or "file" for an uploaded file (the server stores it and keeps its id)


class Source:
    kind: ClassVar[str]                 # unique id, e.g. "postgres"
    label: ClassVar[str]                # shown in the menu, e.g. "PostgreSQL"
    group: ClassVar[str]                # menu section: "Databases", "Files & links", "Apps"
    description: ClassVar[str] = ""
    fields: ClassVar[Tuple[Field, ...]] = ()
    requires: ClassVar[Tuple[str, ...]] = ()  # Python packages the plug-in imports
    hidden: ClassVar[bool] = False            # still works for saved sources, but not offered in the menu
    has_sample: ClassVar[bool] = False        # the Add source form can fill itself with test data

    def test(self, settings: Dict[str, str]) -> str:
        """Connect and return a short success message, e.g. "Connected. 12 tables."; raise SourceError if not."""
        raise NotImplementedError

    def fetch(self, settings: Dict[str, str], max_rows: int) -> pd.DataFrame:
        """Return the data as a table. Fetch at most max_rows + 1 rows so the caller can tell it was too big."""
        raise NotImplementedError


REGISTRY: Dict[str, Source] = {}


def register(cls):
    if cls.kind in REGISTRY:
        raise ValueError(f"Two data-source plug-ins use the kind '{cls.kind}'")
    REGISTRY[cls.kind] = cls()
    return cls


def max_rows() -> int:
    return int(os.getenv("DATAPULSE_MAX_SOURCE_ROWS", "200000"))


def _missing(plugin: Source) -> List[str]:
    return [p for p in plugin.requires if importlib.util.find_spec(p) is None]


GROUPS = ("Databases", "Files & links", "Apps")


def error_text(e: Exception) -> str:
    """A driver exception as one readable line, without SQLAlchemy's "(psycopg.OperationalError) " prefix."""
    first_line = (str(e).strip().splitlines() or [type(e).__name__])[0]
    return re.sub(r"^\([\w.]+\)\s*", "", first_line)


def catalog() -> List[Dict[str, Any]]:
    """Every plug-in and the form it needs, for the "Add source" menu."""
    plugins = sorted(REGISTRY.values(), key=lambda p: GROUPS.index(p.group) if p.group in GROUPS else len(GROUPS))
    return [{"kind": p.kind, "label": p.label, "group": p.group, "description": p.description,
             "fields": [asdict(f) for f in p.fields], "available": not _missing(p),
             "missing_packages": _missing(p), "hidden": p.hidden, "has_sample": p.has_sample} for p in plugins]


def plugin(kind: str) -> Source:
    if kind not in REGISTRY:
        raise SourceError(f"Unknown source type '{kind}'.")
    found = REGISTRY[kind]
    if _missing(found):
        raise SourceError(f"{found.label} needs the Python package(s) {', '.join(_missing(found))} on the server. "
                          "Install them with: pip install 'datapulse[sources]'")
    return found


def clean_settings(kind: str, settings: Dict[str, Any], previous: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Validate a setup form. A blank secret keeps the saved one, so editing a source never needs the password again."""
    fields = {f.name: f for f in plugin(kind).fields}
    unknown = set(settings) - set(fields)
    if unknown:
        raise SourceError(f"Unknown setting(s) for {REGISTRY[kind].label}: {', '.join(sorted(unknown))}")
    out = {}
    for name, f in fields.items():
        value = str(settings.get(name) or "").strip()
        if not value and f.secret and previous:
            value = previous.get(name, "")
        if f.required and not value:
            raise SourceError(f"{f.label} is required.")
        if value:
            out[name] = value
    return out


def public_settings(kind: str, settings: Dict[str, str]) -> Dict[str, Any]:
    """Settings safe to show in the browser: secrets are replaced by whether one is saved."""
    secret = {f.name for f in REGISTRY[kind].fields if f.secret} if kind in REGISTRY else set(settings)
    return {k: ("" if k in secret else v) for k, v in settings.items()} | {
        "saved_secrets": sorted(k for k in settings if k in secret)}


def _secrets(kind: str, settings: Dict[str, str]) -> List[str]:
    """Secret values, plus the password inside any connection URL, so error messages can be scrubbed."""
    found = []
    for f in REGISTRY[kind].fields:
        value = settings.get(f.name)
        if f.secret and value:
            found.append(value)
            try:
                password = urlsplit(value).password
            except ValueError:
                password = None
            if password:
                found += [password, unquote(password)]
    return sorted(set(found), key=len, reverse=True)


def _scrub(message: str, secrets: List[str]) -> str:
    for s in secrets:
        if len(s) >= 3:
            message = message.replace(s, "•••")
    return re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1•••@", message)  # any other user:password@ in a URL


def _guard(kind: str, settings: Dict[str, str], action):
    try:
        return action()
    except SourceError as e:
        raise SourceError(_scrub(str(e), _secrets(kind, settings))) from None
    except Exception as e:  # drivers raise their own exception types; show their message, never a password
        raise SourceError(f"{REGISTRY[kind].label}: {_scrub(error_text(e), _secrets(kind, settings))}") from None


def try_connection(kind: str, settings: Dict[str, str]) -> str:
    found = plugin(kind)
    return _guard(kind, settings, lambda: found.test(settings))


def fetch_source(kind: str, settings: Dict[str, str], name: str = "source") -> pd.DataFrame:
    found, limit = plugin(kind), max_rows()
    df = _guard(kind, settings, lambda: found.fetch(settings, limit))
    if len(df) > limit:
        raise SourceError(f"{name} returned more than {limit:,} rows. Narrow the query or collection filter "
                          "(DATAPULSE_MAX_SOURCE_ROWS raises the limit).")
    if len(df.columns) == 0:
        raise SourceError(f"{name} returned no columns.")
    return normalize_frame(df.reset_index(drop=True), name)


def flatten(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """Nested documents (MongoDB, JSON APIs) as flat columns: {"address": {"city": "X"}} -> "address.city"."""
    if not records:
        return pd.DataFrame()
    return pd.json_normalize(records, sep=".")
