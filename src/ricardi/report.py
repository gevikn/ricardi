"""Read the audit log back.

    python -m ricardi.report state/audit.jsonl

The log is one JSON object per decision, which is the right shape to write and
the wrong shape to read: the interesting parts are the refusals, and they are
scattered among everything that went fine. This counts them.

It exists mainly for the week after setting Ricardi up on two machines. The same
policy on Windows and Linux should produce the same verdicts, and the place that
difference shows up is here, in which rules fired and what got refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


def read(path: Path, include_rotated: bool = True) -> list[dict]:
    """Every decision, oldest first, across rotated generations."""
    files = [path]
    if include_rotated:
        # audit.jsonl.1 is newer than .2, so read the oldest first.
        rotated = sorted(
            path.parent.glob(path.name + ".*"),
            key=lambda p: int(p.suffix.lstrip(".") or 0),
            reverse=True,
        )
        files = rotated + [path]

    entries: list[dict] = []
    for f in files:
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                # A torn line from a crash mid-write is one lost decision, not
                # a reason to refuse to read the rest.
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def within(entries: list[dict], hours: float) -> list[dict]:
    if hours <= 0:
        return entries
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    kept = []
    for e in entries:
        try:
            when = datetime.fromisoformat(str(e.get("at", "")))
        except ValueError:
            continue
        if when >= cutoff:
            kept.append(e)
    return kept


def _table(title: str, counts: Counter, width: int = 34) -> list[str]:
    if not counts:
        return []
    out = [title]
    for name, n in counts.most_common():
        label = (name or "—")[:width]
        out.append(f"  {label:<{width}} {n:>6}")
    return out + [""]


def summarise(entries: list[dict]) -> str:
    if not entries:
        return "No decisions recorded."

    allowed = [e for e in entries if e.get("allow")]
    refused = [e for e in entries if not e.get("allow")]

    lines = [
        f"{len(entries)} decisions   {len(allowed)} allowed   {len(refused)} refused",
    ]
    first, last = entries[0].get("at", "?"), entries[-1].get("at", "?")
    lines += [f"from {first} to {last}", ""]

    lines += _table("By action", Counter(e.get("action", "?") for e in entries))
    lines += _table("Tools used", Counter(e["tool"] for e in allowed if e.get("tool")))

    if refused:
        lines += _table("Why refused", Counter(e.get("rule") or "default deny" for e in refused))
        lines += _table("Who was refused", Counter(e.get("who", "?") for e in refused))
        # The actual refusals, which is what someone opens this to read.
        lines.append("Refused calls")
        seen = Counter(e.get("what") or e.get("action", "?") for e in refused)
        for what, n in seen.most_common(15):
            suffix = f"  (x{n})" if n > 1 else ""
            lines.append(f"  {what}{suffix}")
        if len(seen) > 15:
            lines.append(f"  … and {len(seen) - 15} more")
        lines.append("")

    people = Counter(e.get("who", "?") for e in entries)
    lines += _table("Everyone", people)

    errors = [e for e in entries if e.get("error")]
    if errors:
        lines.append(f"{len(errors)} decisions could not be made; the policy itself failed.")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ricardi.report",
        description="Summarise what Ricardi decided.",
    )
    parser.add_argument("path", nargs="?", default="state/audit.jsonl")
    parser.add_argument(
        "--hours", type=float, default=0,
        help="only the last N hours (default: everything)",
    )
    parser.add_argument(
        "--no-rotated", action="store_true",
        help="read only the live file, not the rotated generations",
    )
    args = parser.parse_args(argv)

    path = Path(args.path).expanduser()
    if not path.exists() and not list(path.parent.glob(path.name + ".*")):
        print(f"No audit log at {path}.", file=sys.stderr)
        return 2

    entries = within(read(path, include_rotated=not args.no_rotated), args.hours)
    print(summarise(entries), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
