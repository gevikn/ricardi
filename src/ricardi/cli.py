"""Run Ricardi, against Slack or against the local stub."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from ricardi import config, policy as policy_mod
from ricardi.audit import AuditedPolicy, AuditLog
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
    names = ", ".join(a.name for a in settings.agents)
    print(
        f"ricardi on {where}; agents: {names}; "
        f"{len(policy.rules)} rules, {len(policy.roster)} people"
        + (f", {per_hour}/hour" if per_hour else "")
        + (f", {per_minute}/minute" if per_minute else "")
        + f"; audit {audit_path}",
        flush=True,
    )
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await ricardi.close()
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
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    _load_env(Path(args.env))

    try:
        return asyncio.run(_serve(args))
    except (config.ConfigError, policy_mod.PolicyError) as exc:
        print(f"ricardi: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
