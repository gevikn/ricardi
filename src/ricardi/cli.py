"""Run Ricardi, against Slack or against the local stub."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ricardi import checks, config, policy as policy_mod, readiness
from ricardi.audit import AuditedPolicy, AuditLog
from ricardi.checks import (
    unbounded_tool_allows,
    unrostered_owners,
    warn_unbounded_tool_allows,
    warn_unrostered_owners,
)
from ricardi.grants import (
    Grant,
    GrantError,
    GrantStore,
    normalise,
    normalise_name,
    backup_store,
    seed_from_policy,
)
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


def _check_state_dir(args: argparse.Namespace, settings: config.Settings) -> Path:
    """Where the grant store is, with the same override `grants` accepts.

    `--state-dir` is not a second way to find state/: it overrides the value the
    catalogue derives, for a store that lives somewhere else.
    """
    given = getattr(args, "state_dir", "")
    if given:
        return Path(given).expanduser()
    return settings.state_dir or Path("state")


def grants_in_force(state_dir: Path) -> tuple[tuple[Grant, ...] | None, str]:
    """The grants in force, or why they could not be read.

    None rather than an empty tuple when the store will not open, because the
    two must not look alike: an empty store means nobody has been granted
    anything, and an unreadable one means nothing is known. Reporting the
    first for the second sends somebody to re-seed a store that is fine.
    """
    try:
        store = GrantStore.open(state_dir / "grants.db")
    except GrantError as exc:
        return None, str(exc)
    try:
        return store.current(), ""
    except GrantError as exc:
        return None, str(exc)
    finally:
        store.close()


def _print_family(label: str, why: str, found: list[checks.Finding]) -> None:
    print(f"{label} -- {why}")
    if not found:
        # Said out loud: a section that printed nothing reads exactly like a
        # section that never ran, which is the trap this repo keeps meeting.
        print("  nothing to report")
        return
    width = max(len(f.assertion) for f in found)
    for one in found:
        print(f"  {one.assertion.ljust(width)}  {one.text}")


def _check_command(args: argparse.Namespace) -> int:
    """Read the configured world back and say what is so.

    §9's gap was that paranoia is achievable and not verifiable: every item on
    the dial is "you can write it correctly" rather than "it cannot be wrong".
    This is the readout, and `--strict` is what lets a deploy gate on it.

    The two families get different consequences, which is the one decision this
    command turns on. A wholeness finding is a defect -- something configured
    cannot work -- so `--strict` counts it. A posture finding is a dial
    position, and the shipped example deliberately sits off the tightest
    setting, so counting those by default would fail the gate on a correct
    configuration and get it deleted from the deploy within a week.
    `--strict-posture` is for an operator who has decided their dial is at the
    top and wants it held there.
    """
    strict = bool(args.strict)
    strict_posture = bool(args.strict_posture)

    blocked: list[str] = []
    settings = None
    policy = None
    try:
        settings = config.load(args.agents)
    except config.ConfigError as exc:
        blocked.append(f"the agent catalogue: {exc}")
    try:
        policy = policy_mod.load(args.policy)
    except policy_mod.PolicyError as exc:
        blocked.append(f"the policy: {exc}")

    if settings is None or policy is None:
        # Nothing below these two can be asserted, and a report that says no
        # findings because it never reached the files is worse than no report.
        print("not checked:")
        for line in blocked:
            print(f"  {line}")
        print("nothing was checked.")
        return 1 if (strict or strict_posture) else 0

    grants, why = grants_in_force(_check_state_dir(args, settings))
    if why:
        blocked.append(f"the grant store: {why}")

    posture = checks.posture(policy, settings)
    whole = checks.wholeness(
        settings, policy, grants, env=os.environ, home=Path.home()
    )

    _print_family("posture", "is the dial where you think it is", posture)
    _print_family("wholeness", "does the configured world hang together", whole)
    if blocked:
        print("not checked:")
        for line in blocked:
            print(f"  {line}")
    # Counted apart because only one of the three decides an exit code, and a
    # summary that lumps them together is read as though all of them do.
    gating = [one for one in whole if one.gating]
    notes = len(whole) - len(gating)
    print(f"{len(posture)} posture note(s), {len(gating)} wholeness finding(s)"
          + (f" and {notes} wholeness note(s)" if notes else "") + ".")

    if strict and gating:
        return 1
    if strict_posture and posture:
        return 1
    # An assertion that did not run must not read as one that passed, which is
    # the only thing a gate is asking about.
    if blocked and (strict or strict_posture):
        return 1
    return 0


async def _serve(args: argparse.Namespace) -> int:
    settings = config.load(args.agents)
    # Nothing is ready yet, and the state directory outlives the container --
    # so a process that starts and never serves must not leave the last one's
    # marker standing for the deploy to read as its own.
    readiness.clear(settings.state_dir)
    # Loaded before anything connects: a broken policy should stop startup, not
    # surface as a refusal to the first person who asks.
    policy = policy_mod.load(args.policy)
    warn_unrostered_owners(policy)
    warn_unbounded_tool_allows(policy)
    declared_limits = policy_mod.load_limits(args.policy)
    # Unlike threads.json and sessions.json, which shrug off corruption because
    # they must not stop Ricardi answering, a corrupt grant store stops startup:
    # serving with no grants would deny everyone and look exactly like a policy
    # bug. A missing file is fine -- open() creates it empty, and every ask is
    # refused until `ricardi grants seed` has run.
    # Held in a local as well, because `policy` is rebound twice below: without
    # it there is nothing left to close the connection through, and WAL dropped
    # without a checkpoint leaves the next process reading through a stale
    # grants.db-wal.
    grants = GrantStore.open(settings.state_dir / "grants.db")
    policy.grants = grants
    # Splatted, so an unset ceiling keeps the dataclass default rather than
    # being read as "no ceiling": the grant ceilings default on.
    limits = Limits(**declared_limits)

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

    # The credential is the half that dies quietly: Slack stays up and every
    # agent fails, which from a channel is indistinguishable from the box being
    # switched off. Nothing to watch behind the fake backend, which needs no
    # credential to answer.
    watch = None
    if args.backend != "fake" and settings.health_every > 0:
        from ricardi.health import CredentialWatch, sdk_probe

        channel = settings.alert_channel

        async def announce(text: str) -> None:
            # Channel root, not a thread: nobody is reading a thread when the
            # thing that would have replied in it is the thing that is broken.
            await transport.send(channel, "", text)

        if not channel:
            logging.getLogger("ricardi").warning(
                "no alert_channel set in %s, so a dead credential will show up "
                "only in the log. Add one under [ricardi] to hear about it in "
                "Slack instead.",
                args.agents,
            )
        watch = CredentialWatch(
            probe=sdk_probe(),
            announce=announce if channel else None,
            every=settings.health_every,
        )

    state = settings.state_dir / "threads.json" if settings.state_dir else None
    ricardi = Ricardi(
        transport=transport,
        backend=backend,
        bindings=ThreadBindings(state),
        ack_after=settings.ack_after,
        policy=policy,
        grants=grants,
        health=watch,
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
        "ricardi on %s; agents: %s; %d rules, %d people%s%s%s; audit %s",
        where, ", ".join(names), len(policy.rules), len(policy.roster),
        f", {limits.per_hour}/hour" if limits.per_hour else "",
        f", {limits.per_minute}/minute" if limits.per_minute else "",
        # Said out loud because it is on by default: a ceiling nobody
        # configured is one nobody knows to expect when it fires.
        f", grants {limits.grants_per_minute}/minute" if limits.grants_per_minute else "",
        audit_path,
    )
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await ricardi.close()
        # Checkpoints the WAL on the way out, so a restart does not read the
        # store through the last process's grants.db-wal.
        grants.close()
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
        elif args.action == "backup":
            into = Path(args.to).expanduser() if args.to else state_dir / "backups"
            dest = backup_store(store, into, keep=args.keep)
            print(f"snapshot {dest} ({len(store.history())} row(s)), "
                  f"keeping the newest {args.keep}")
        else:
            granted = args.action == "add"
            store.add(person=args.person, name=args.name, verb=args.verb,
                      by=args.by, granted=granted)
            word = "granted" if granted else "revoked"
            # Echoes what was stored, not what was typed: a name that differs
            # only in case is the same grant, and saying otherwise is how
            # "I granted it and they are still refused" happens.
            print(f"{word} {normalise_name(args.verb)} on "
                  f"{normalise_name(args.name)} for {normalise(args.person)}")
    finally:
        store.close()
    return 0


def catalogue_names(path: str | os.PathLike[str]) -> tuple[tuple[str, ...], str]:
    """The names already taken, and why they could not be read if they were not.

    Degrades to a reason rather than an error on purpose. `agents.toml` names
    real directories on one machine, so it is gitignored: CI has none, a fresh
    clone has none, and `config.load` refuses one whose `cwd` is somewhere
    else. Refusing here would mean the check could not run in the one place
    that runs it on every pull request. Swallowing it would mean a collision
    silently goes unchecked, which is the failure the request flow exists to
    catch -- so the caller prints the reason.
    """
    try:
        settings = config.load(path)
    except config.ConfigError as exc:
        return (), str(exc)
    return tuple(a.name for a in settings.agents), ""


def request_files(paths: list[str]) -> tuple[list[Path], list[str]]:
    """The request files to check, and the directories that held none.

    A directory means every request in it, sorted, so one command covers the
    whole directory CI checks and the single file an owner is editing. An empty
    directory is reported rather than passing quietly: a glob over nothing is a
    green run that checked nothing, which this repo has been bitten by before.
    """
    files: list[Path] = []
    empty: list[str] = []
    for one in paths:
        p = Path(one).expanduser()
        if not p.is_dir():
            # A path that is not there is left to the checker, whose message
            # about a missing request is better than anything here.
            files.append(p)
            continue
        found = sorted(p.glob("*.toml"))
        if not found:
            empty.append(f"{p} holds no requests, so nothing there was checked.")
        files.extend(found)
    return files, empty


def _describe(request) -> str:
    n = len(request.invitees)
    return (f"owner {request.owner.email}, {n} invitee{'' if n == 1 else 's'}, "
            f"${request.max_budget_usd:.2f}, {request.max_turns} turns")


def _check_request_command(args: argparse.Namespace) -> int:
    """Check provisioning requests without applying anything.

    The checker predates this command by a module and had no caller but pytest:
    the admin holding a merged request had no way to run it, and the owner was
    told to install the test dependencies to check their own file.
    """
    from ricardi import provision

    taken, why = catalogue_names(args.agents)
    if why:
        print(f"not checking for collisions with existing agents: {why}")
    names = list(taken)

    files, empty = request_files(args.paths)
    for line in empty:
        print(line)

    refused = 0
    for path in files:
        request, findings = provision.check(
            path, existing_names=tuple(names), max_budget_usd=args.max_budget_usd
        )
        if findings:
            refused += 1
            for one in findings:
                print(f"REFUSED {path}: {one}")
            continue
        # Held against the files after it, so two requests in one pull request
        # cannot both claim a name.
        names.append(request.name)
        print(f"ok      {path}: {request.name} -- {_describe(request)}")

    if refused:
        print(f"{refused} of {len(files)} request(s) refused.")
        return 1
    print(f"{len(files)} request(s) fit to review.")
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
    snapshot = actions.add_parser(
        "backup", parents=[after],
        help="write a verified snapshot of the store, and prune old ones")
    snapshot.add_argument("--to", default="",
                          help="where snapshots go (default: <state-dir>/backups)")
    snapshot.add_argument("--keep", type=int, default=14,
                          help="how many to keep (default: 14; 0 keeps every one)")
    for verb_name, text in (("add", "grant one person one verb on one agent"),
                            ("revoke", "supersede a grant; the history is kept")):
        one = actions.add_parser(verb_name, parents=[after], help=text)
        one.add_argument("--person", required=True, help="email")
        one.add_argument("--name", required=True, help="agent name")
        one.add_argument("--verb", required=True)
        one.add_argument("--by", required=True, help="who is recording this")
    # Accepted after the subcommand as well as before it, which is where people
    # type it -- the same arrangement, and for the same reason, as
    # `grants list --state-dir X`. SUPPRESS keeps the unset default from
    # overwriting a value given before the subcommand.
    catalogue = argparse.ArgumentParser(add_help=False)
    catalogue.add_argument(
        "--agents", default=argparse.SUPPRESS,
        help="agent catalogue, read only for the names already taken",
    )
    asked = sub.add_parser(
        "check-request",
        parents=[catalogue],
        help="check a provisioning request, or a directory of them, and apply "
             "nothing",
    )
    asked.add_argument(
        "paths", nargs="+",
        help="request files, or directories of them (requests/ checks the lot)",
    )
    asked.add_argument(
        "--max-budget-usd", type=float, default=0.0,
        help="refuse a request asking for more than this. The admin's ceiling "
             "is in no file, so it is given here; without it no ceiling is "
             "applied at this step",
    )
    # The same arrangement again: accepted on either side of the subcommand,
    # with SUPPRESS so an unset default cannot overwrite a value given before.
    overrides = argparse.ArgumentParser(add_help=False)
    overrides.add_argument("--agents", default=argparse.SUPPRESS)
    overrides.add_argument("--policy", default=argparse.SUPPRESS)
    overrides.add_argument("--state-dir", default=argparse.SUPPRESS, help=state_help)
    report = sub.add_parser(
        "check",
        parents=[overrides],
        help="read the configured world back: the posture dial, and whether "
             "what is configured hangs together. Changes nothing",
    )
    report.add_argument(
        "--strict", action="store_true",
        help="exit non-zero on a wholeness finding, so a deploy can gate on it. "
             "Posture notes are a readout and do not fail: the shipped example "
             "deliberately sits off the tightest setting",
    )
    report.add_argument(
        "--strict-posture", action="store_true",
        help="exit non-zero on a posture note as well, for a dial that has been "
             "decided and should stay put",
    )
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
        if args.command == "check-request":
            return _check_request_command(args)
        if args.command == "check":
            return _check_command(args)
        return asyncio.run(_serve(args))
    except (config.ConfigError, policy_mod.PolicyError, GrantError) as exc:
        print(f"ricardi: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
