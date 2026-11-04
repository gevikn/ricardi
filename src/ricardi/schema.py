"""What each table in a config file may contain.

One mistake in `policy.toml` and `agents.toml` used to be quiet where every
other one was loud: a key nothing reads. Both files are read key by key with
`.get`, so an unknown key is dropped -- and dropping a key is never narrowing.
In a rule it is the sharp end of it, because `Rule.matches` treats a dimension
the rule does not name as matching everything, so `tols = ["Read"]` is a rule
about every tool rather than about two.

These two helpers return the message rather than raising it, so each file keeps
raising its own error type while both say the same thing the same way.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping
from typing import Any


def _hint(key: str, known: Iterable[str]) -> str:
    near = difflib.get_close_matches(key, sorted(known), n=1)
    return f" Did you mean {near[0]!r}?" if near else ""


def unknown_key(where: str, entry: Mapping[str, Any], known: Iterable[str]) -> str | None:
    """The first key in `entry` that nothing reads, as a message. None if all are known."""
    known = frozenset(known)
    for key in entry:
        if key in known:
            continue
        return (
            f"{where} has an unknown key {str(key)!r}.{_hint(str(key), known)} "
            "Nothing reads it, so whatever it was meant to bound is left unbounded."
        )
    return None


def unknown_table(raw: Mapping[str, Any], known: Iterable[str]) -> str | None:
    """The first top-level table that nothing reads, as a message."""
    known = frozenset(known)
    for table in raw:
        if table in known:
            continue
        return f"unknown table {str(table)!r}.{_hint(str(table), known)}"
    return None
