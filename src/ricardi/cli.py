"""Run Ricardi, against Slack or against the local stub."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ricardi import config, policy as policy_mod, readiness
from ricardi.audit import AuditedPolicy, AuditLog
from ricardi.grants import GrantError, GrantStore, normalise, seed_from_policy
from ricardi.limits import Limits, RateLimited
from ricardi.app import Ricardi
from ricardi.store import SessionIds, ThreadBindings


def _load_env(path: Path) -> None:
    """Read a .env without taking a dependency to do it."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        # Only a matching pair comes off: stripping any mixture would eat a
        # quote that is part of the value.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # Anything already in the environment wins, so a shell export can
        # override the file without editing it.
        os.environ.setdefault(key.strip(), value)


async def _serve(args: argparse.Namespace) -> int:
    settings = config.load(args.agents)
    # Nothing is ready yet, and the state directory outlives the container --
    # so a process that starts and never serves must not leave the last one's
    # marker standing for the deploy to read as its own.
    readiness.clear(settings.state_dir)
    # Loaded before anything connects: a broken policy should stop startup, not
    # surface as a refusal to the first person who asks.
    policy = policy_mod.load(args.policy)
    per_minute, per_hour = policy_mod.load_limits(args.policy)
    limits = Limits(per_minute=per_minute, per_hour=per_hour)

    # Order matters: the rate limiter wraps the rules so it can refuse a turn
    # the rules would allow, and the audit wraps both so its refusals are
    # recorded like any other.
    policy = RateLimited(policy, limits)
    audit_path = Path(args.audit) if args.audit else (settings.state_dir / "audit.jsonl")
    policy = AuditedPolicy(
        policy,
        AuditLog(
            audit_path,
            max_bytes=int(settings.audit_max_mb * 1024 * 1024),
            keep=settings.audit_keep,
        ),
    )

    if args.backend == "fake":
        from ricardi.backends.base import Agent
        from ricardi.backends.fake import FakeBackend

        backend = FakeBackend(
            [
                Agent(name=s.name, description=s.description, cwd=s.cwd)
                for s in settings.agents
            ]
        )
    else:
        from ricardi.backends.sdk import SdkBackend

        backend = SdkBackend(
            settings.agents,
            policy=policy,
            sessions=SessionIds(settings.state_dir / "sessions.json"),
        )

    if args.transport == "slack":
        from ricardi.transports.slack import SlackTransport

        transport = SlackTransport(
            bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
            app_token=os.environ.get("SLACK_APP_TOKEN", ""),
        )
        where = "Slack"
    else:
        from ricardi.transports.local import LocalTransport

        transport = LocalTransport(host=args.host, port=args.port)
        where = f"http://{args.host}:{args.port}"

    state = settings.state_dir / "threads.json" if settings.state_dir else None
    ricardi = Ricardi(
        transport=transport,
        backend=backend,
        bindings=ThreadBindings(state),
        ack_after=settings.ack_after,
        policy=policy,
    )

    await ricardi.start()
    names = [a.name for a in settings.agents]
    # The machine-readable half of the line below. The deploy reads this
    # instead of grepping the container log, which on the box loses its
    # newest lines and so answers "is it up" with a previous boot's word.
    readiness.mark_ready(settings.state_dir, where=where, agents=names)
    # Logged rather than printed, so --log-file catches the one line that says
    # what this process actually is. A banner only on stdout is the line you
    # most want when reading a log weeks later.
    logging.getLogger("ricardi").info(
        "ricardi on %s; agents: %s; %d rules, %d people%s%s; audit %s",
        where, ", ".join(names), len(policy.rules), len(policy.roster),
        f", {per_hour}/hour" if per_hour else "",
        f", {per_minute}/minute" if per_minute else "",
        audit_path,
    )
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await ricardi.close()
    return 0


def log_handlers(log_file: str) -> list[logging.Handler]:
    """Where the log goes: the console, and a file as well when asked for one.

    Its own function because the file half is the part that can silently not
    happen -- a Windows scheduled task captures nothing, so a process that
    logs only to a console it does not have runs perfectly and leaves nothing
    behind. Worth a test rather than a hope.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if not log_file:
        return handlers
    path = Path(log_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Rotating, because this is the log of something meant to run for months:
    # 10MB and five generations, the same shape as the audit log.
    handlers.append(
        RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5,
                            encoding="utf-8")
    )
    return handlers


def _grants_command(args: argparse.Namespace) -> int:
    """Drive the grant store by hand: the admin's half of the access model."""
    # An explicit --state-dir overrides the same value the settings carry; it
    # is not a second way to find state/. Settings are loaded only when
    # needed, so list/add/revoke work against a store without a catalogue.
    settings = None
    if args.state_dir:
        state_dir = Path(args.state_dir).expanduser()
    else:
        settings = config.load(args.agents)
        state_dir = settings.state_dir
    # Opened with the rest of the failure paths: a corrupt store raises
    # GrantError, which main() reports and turns into a non-zero exit.
    store = GrantStore.open(state_dir / "grants.db")
    try:
        if args.action == "list":
            for g in store.current():
                print(f"{g.person}	{g.name}	{g.verb}	by {g.by}	{g.at}")
        elif args.action == "seed":
            settings = settings or config.load(args.agents)
            roster = policy_mod.load(args.policy)
            n = seed_from_policy(
                store, roster, names=[a.name for a in settings.agents], by=args.by
            )
            print(f"seeded {n} grant(s)")
        else:
            granted = args.action == "add"
            store.add(person=args.person, name=args.name, verb=args.verb,
                      by=args.by, granted=granted)
            word = "granted" if granted else "revoked"
            print(f"{word} {args.verb} on {args.name} for {normalise(args.person)}")
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ricardi", description=__doc__)
    here = Path(__file__).resolve().parents[2]
    parser.add_argument(
        "--agents",
        default=str(here / "agents.toml"),
        help="agent catalogue (default: agents.toml beside the project)",
    )
    parser.add_argument(
        "--policy",
        default=str(here / "policy.toml"),
        help="policy rules (default: policy.toml beside the project)",
    )
    parser.add_argument(
        "--audit",
        default="",
        help="where decisions are recorded (default: state/audit.jsonl)",
    )
    parser.add_argument("--transport", choices=["slack", "local"], default="slack")
    parser.add_argument(
        "--backend",
        choices=["sdk", "fake"],
        default="sdk",
        help="fake answers without Claude, for checking the wiring",
    )
    parser.add_argument("--port", type=int, default=8808, help="local transport port")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="local transport address; 0.0.0.0 to be reachable from outside a "
             "container (the default binds to loopback, which a port mapping "
             "cannot reach)",
    )
    parser.add_argument("--env", default=str(here / ".env"))
    parser.add_argument(
        "--log-file",
        default="",
        help="also write the log here, rotating at 10MB. A Windows scheduled "
             "task captures nothing on its own, so without this it runs "
             "perfectly and leaves nothing to read",
    )
    parser.add_argument("--verbose", action="store_true")
    # Optional: the container launches `ricardi` with flags and no subcommand,
    # and that must keep meaning "serve".
    sub = parser.add_subparsers(dest="command")
    grants = sub.add_parser("grants", help="inspect and change who has been granted what")
    state_help = ("override the state directory (default: derived from --agents, "
                  "as audit.jsonl and sessions.json are)")
    grants.add_argument("--state-dir", default="", help=state_help)
    # Also accepted after the action (`grants list --state-dir X`), which is
    # where people type it. SUPPRESS keeps an action parser's unset default
    # from overwriting a value given before it.
    after = argparse.ArgumentParser(add_help=False)
    after.add_argument("--state-dir", default=argparse.SUPPRESS, help=state_help)
    actions = grants.add_subparsers(dest="action", required=True)
    actions.add_parser("list", parents=[after],
                       help="print the grants in force (not the history)")
    actions.add_parser("seed", parents=[after],
                       help="grant what policy.toml already permits").add_argument(
        "--by", required=True, help="who is recording this")
    for verb_name, text in (("add", "grant one person one verb on one agent"),
                            ("revoke", "supersede a grant; the history is kept")):
        one = actions.add_parser(verb_name, parents=[after], help=text)
        one.add_argument("--person", required=True, help="email")
        one.add_argument("--name", required=True, help="agent name")
        one.add_argument("--verb", required=True)
        one.add_argument("--by", required=True, help="who is recording this")
    args = parser.parse_args(argv)

    logging.basicConfig(
        handlers=log_handlers(args.log_file),
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    _load_env(Path(args.env))

    try:
        if args.command == "grants":
            return _grants_command(args)
        return asyncio.run(_serve(args))
    except (config.ConfigError, policy_mod.PolicyError, GrantError) as exc:
        print(f"ricardi: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
