"""What `ricardi check` asserts about a configuration that already loaded.

Two families, because they answer different questions and deserve different
consequences.

**Posture** (§9): *is the dial where you think it is.* Every item here is a
legitimate thing to mean. The shipped example sits deliberately off the tightest
setting -- it allows commands by argv shape, and points one read rule at the
directory above every checkout -- so these are dial positions, not defects. The
gap §9 names is that paranoia was achievable and not verifiable; this is the
readout.

**Wholeness** (§16): *does the configured world hang together.* Every item here
is a defect: something is configured and cannot work, or is in the store and
matches nothing. These are what a deploy should refuse to land on.

One predicate per assertion, pure, returning what it found rather than a printed
line. The startup warnings, the command and the tests then read the same answer,
for the same reason §11's tests drive the real `config.load`: two copies of a
safety assertion is one copy that goes stale.

Read-only throughout: no network, no off-box reach, no credential contents, and
nothing secret in the output -- a report is something people paste into a
channel.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from ricardi import policy as policy_mod
from ricardi.config import Settings
from ricardi.grants import Grant, normalise, normalise_name


@dataclass(frozen=True)
class Finding:
    """One thing that is so, said once.

    `assertion` is a slug rather than prose so a reader can grep a report, and
    so the composed report can be asserted against by assertion rather than by
    matching the sentence.

    `gating` is whether a deploy should refuse to land on this. Every posture
    item is false -- they are dial positions, and the shipped example sits off
    the tightest setting on purpose. One wholeness item is false too: a roster
    identity holding no grant is the correct state for somebody just added, and
    §16 words that one as "reported as inert" for exactly this reason. A gate
    that fails on a correct configuration is a gate that gets deleted.
    """

    family: str
    assertion: str
    text: str
    gating: bool = True


# The helpers below reach into `policy`'s own matching -- `_tool_matches`,
# `_any_glob`, `_under` -- rather than reimplementing it. Private names across a
# module boundary is a smell worth one sentence: a readout that matched names its
# own way would eventually disagree with the decision it claims to report, and
# `_glob` once let `GIT PUSH` past a deny precisely because two comparisons
# disagreed about case. One implementation, used by both.


def _is_tool_rule(rule: policy_mod.Rule) -> bool:
    """Does this rule bear on tool calls? Naming no actions means every action."""
    return not rule.actions or "tool" in rule.actions


def _names_shell(rule: policy_mod.Rule) -> bool:
    """Does this rule cover a shell? Naming no tools covers every tool there is."""
    if not rule.tools:
        return True
    return any(policy_mod._tool_matches(t, rule.tools) for t in policy_mod.SHELL_TOOLS)


def _only_shell(rule: policy_mod.Rule) -> bool:
    """Is every tool this rule names a shell?

    A command carries no path, so `path_under` can never be satisfied by one --
    asking a correctly written shell rule for a path bound would report every
    one of them, which is how a report stops being read.
    """
    if not rule.tools:
        return False
    lowered = {t.strip().lower() for t in rule.tools}
    shells = {t.lower() for t in policy_mod.SHELL_TOOLS} | {policy_mod.SHELL_ALIAS}
    return lowered.issubset(shells)


def _applies_to(rule: policy_mod.Rule, name: str) -> bool:
    """Does this rule reach this agent name? Naming no agents reaches every one.

    Matched with policy's own glob, so the readout cannot disagree with the
    decision it is reporting on.
    """
    return not rule.agents or policy_mod._any_glob(name, rule.agents)


# ====================================================== family A: posture


def allows_without_users(policy: policy_mod.RulesPolicy) -> list[str]:
    """Names of allow rules that say nothing about who may use them.

    `Rule.matches` treats an unnamed dimension as matching everything, so an
    allow with no `users` is an allow for anyone the transport can carry -- the
    one omission that turned a write rule in the shipped example into a rule
    for everybody, and the reason §9 wants this asserted rather than described.
    """
    return [
        rule.name or "(unnamed)"
        for rule in policy.rules
        if rule.effect == "allow" and not rule.users
    ]


def unbounded_tool_allows(policy: policy_mod.RulesPolicy) -> list[str]:
    """Names of allow rules that let any tool run, because they name none.

    `Rule.matches` treats a dimension the rule does not name as matching
    everything, so an allow with no `tools` is an allow for every tool there
    is. Naming no `actions` is broader still: every action, tool calls among
    them.
    """
    return [
        rule.name or "(unnamed)"
        for rule in policy.rules
        if rule.effect == "allow"
        and not rule.tools
        and (not rule.actions or "tool" in rule.actions)
    ]


def allows_outside_their_roots(
    policy: policy_mod.RulesPolicy, settings: Settings
) -> list[tuple[str, str]]:
    """(rule, why) for every tool allow not bounded inside a name's own tree.

    Two shapes, one assertion, because both answer "how far does this reach":
    no `path_under` at all reaches every path the process can open, and a
    `path_under` above the roots of the names it applies to reaches their
    siblings. The shipped example does the second deliberately -- one read rule
    pointed at the directory that holds every checkout -- which is exactly the
    kind of decision this is here to surface rather than refuse.
    """
    roots = {a.name: (a.roots or (a.cwd,)) for a in settings.agents}
    found: list[tuple[str, str]] = []
    for rule in policy.rules:
        if rule.effect != "allow" or not _is_tool_rule(rule) or _only_shell(rule):
            continue
        label = rule.name or "(unnamed)"
        if not rule.path_under:
            found.append((
                label,
                "names no path_under, so it reaches every path the process can "
                "open, not only the agents' trees",
            ))
            continue
        reachable = [
            root
            for name, name_roots in roots.items()
            if _applies_to(rule, name)
            for root in name_roots
        ]
        if not reachable:
            # A rule that reaches no name in the catalogue bounds nothing
            # because it can never match. `dead_rules` says so once, in its own
            # words; guessing about its path here would say it twice.
            continue
        if not any(policy_mod._under(rule.path_under, root) for root in reachable):
            found.append((
                label,
                f"is bounded to {rule.path_under}, which is not inside "
                f"{', '.join(sorted(set(reachable)))} -- so it reaches outside "
                "the tree of the name that asks",
            ))
    return found


def allows_matching_commands(policy: policy_mod.RulesPolicy) -> list[str]:
    """Names of allow rules that decide by argv shape.

    H4: `command_matches` is sound as a deny and unsound as an allow. The
    patterns are prefix-shaped, so `ls *` matches `ls -a && anything-else`: an
    allow built on one grants whatever follows the shape it recognised. A deny
    built on the same patterns is fine, and the shipped example's denies are.
    """
    return [
        rule.name or "(unnamed)"
        for rule in policy.rules
        if rule.effect == "allow" and rule.command_matches
    ]


def names_with_unbounded_commands(
    policy: policy_mod.RulesPolicy, settings: Settings
) -> list[str]:
    """Names that may run a shell with no deny narrowing what runs.

    §9 asks for "shell denied, or argv-shape-denied". A name with no shell allow
    at all satisfies the first and is the quietest posture there is, so it must
    not read as the loudest. Only a *deny* counts as narrowing: an allow's own
    `command_matches` is unsound as a bound, which `allows_matching_commands`
    reports on its own terms.
    """
    found: list[str] = []
    for spec in settings.agents:
        granted = any(
            rule.effect == "allow"
            and _is_tool_rule(rule)
            and _names_shell(rule)
            and _applies_to(rule, spec.name)
            for rule in policy.rules
        )
        if not granted:
            continue
        narrowed = any(
            rule.effect == "deny"
            and _is_tool_rule(rule)
            and _applies_to(rule, spec.name)
            and (rule.command_matches or _names_shell(rule))
            for rule in policy.rules
        )
        if not narrowed:
            found.append(spec.name)
    return found


def names_without_ceilings(settings: Settings) -> list[tuple[str, str]]:
    """(name, which) for every name missing a budget or a turn ceiling.

    Zero means "no ceiling" in `agents.toml`, deliberately -- but on a machine
    left running, no ceiling is a decision, and this is where it gets noticed
    before the bill does.
    """
    found: list[tuple[str, str]] = []
    for spec in settings.agents:
        missing = [
            field
            for field, value in (("max_budget_usd", spec.max_budget_usd),
                                 ("max_turns", spec.max_turns))
            if not value
        ]
        if missing:
            found.append((spec.name, " and ".join(missing)))
    return found


def unnamed_rules(policy: policy_mod.RulesPolicy) -> list[int]:
    """1-based positions of rules with no `name`.

    Every other finding here quotes a rule by name, so an unnamed rule is one
    nobody can act on: "(unnamed) allows every tool" sends a reader to read the
    whole file. Positions rather than indices, because that is how a person
    counts `[[rule]]` blocks.
    """
    return [
        i for i, rule in enumerate(policy.rules, start=1) if not rule.name.strip()
    ]


# ==================================================== family B: wholeness


def unrostered_owners(policy: policy_mod.RulesPolicy) -> list[tuple[str, str]]:
    """(name, email) for every [[owner]] whose email is not on the roster."""
    on_roster = {normalise(e) for e in policy.roster.values()}
    return [
        (name, email)
        for name, emails in policy.owners.items()
        for email in emails
        if normalise(email) not in on_roster
    ]


def warn_unrostered_owners(policy: policy_mod.RulesPolicy) -> None:
    """Say so at startup, rather than refuse to start.

    An owner who is not on the roster can never act: `decide` refuses an unknown
    principal before any conferral check. That is a typo worth a line in the log,
    but not worth a load-time error, which would turn it into a total outage.
    """
    for name, email in unrostered_owners(policy):
        logging.getLogger("ricardi").warning(
            "owner %s of %s is not in the [[identity]] roster, so they cannot act "
            "as an owner until they are added.", email, name,
        )


def warn_unbounded_tool_allows(policy: policy_mod.RulesPolicy) -> None:
    """Say so at startup, rather than refuse to start.

    "Every tool, for these people, under this root" is a legitimate thing to
    mean, and the shipped example is careful never to need it -- so this is a
    warning, not an error. What it buys is that the breadth has to be meant:
    the loader now rejects the typo that produces it by accident, and this
    catches the one somebody wrote on purpose and forgot.
    """
    for name in unbounded_tool_allows(policy):
        logging.getLogger("ricardi").warning(
            "rule %r allows every tool there is, because it names none. "
            "Add tools = [...] if that is not what was meant.", name,
        )


def names_without_grants(
    names: Sequence[str], grants: Iterable[Grant]
) -> list[str]:
    """Catalogue names that no grant row reaches.

    A grant row is required to address a name, so a name without one is
    configured and unreachable -- the half-applied state §11 warns about, and
    the one a hand-applied name lands in when the last step is forgotten.
    """
    held = {normalise_name(g.name) for g in grants}
    return [name for name in names if normalise_name(name) not in held]


def grants_for_unknown_names(
    names: Sequence[str], grants: Iterable[Grant]
) -> list[str]:
    """Names in the store that the catalogue does not declare.

    The other direction, and usually a typo in `grants add --name` or a name
    removed from the catalogue with its rows left behind. Either way the rows
    grant nothing, and a grant that looks present and does nothing is how
    "I granted it and they are still refused" happens.
    """
    known = {normalise_name(n) for n in names}
    seen: dict[str, str] = {}
    for g in grants:
        key = normalise_name(g.name)
        if key not in known:
            seen.setdefault(key, g.name)
    return list(seen.values())


def dead_rules(
    policy: policy_mod.RulesPolicy, names: Sequence[str]
) -> list[str]:
    """Names of rules that can never match any name in the catalogue.

    A rule naming `agents = ["shrared"]` is not a rule about a typo: it is a
    rule about nothing, and if it was a deny then the thing it meant to refuse
    is allowed. The same class as a grant row for a name that does not exist,
    on the other side of the file. Matched with policy's own glob, so a rule
    naming `shared*` counts as live when some name matches it.
    """
    return [
        rule.name or "(unnamed)"
        for rule in policy.rules
        if rule.agents and not any(_applies_to(rule, n) for n in names)
    ]


def granted_people_off_roster(
    policy: policy_mod.RulesPolicy, grants: Iterable[Grant]
) -> list[str]:
    """People holding grants who are not on the roster.

    `decide` refuses an unknown principal before any conferral check, so these
    grants can never be used by the people they name.
    """
    on_roster = {normalise(e) for e in policy.roster.values()}
    seen: dict[str, str] = {}
    for g in grants:
        key = normalise(g.person)
        if key not in on_roster:
            seen.setdefault(key, g.person)
    return list(seen.values())


def inert_roster_identities(
    policy: policy_mod.RulesPolicy, grants: Iterable[Grant]
) -> list[str]:
    """Roster identities holding no grant at all.

    Not a defect on its own: a teammate added to the roster is inert until
    somebody grants them something, which is the correct order. Reported
    because it is the whole difference between "added" and "added and able to
    ask", and that difference is invisible from the file.
    """
    held = {normalise(g.person) for g in grants}
    seen: dict[str, str] = {}
    for email in policy.roster.values():
        key = normalise(email)
        if key and key not in held:
            seen.setdefault(key, email)
    return list(seen.values())


def credentials(
    env: Mapping[str, str], home: str | os.PathLike[str]
) -> list[str]:
    """What is wrong with the credential this process would use. Existence only.

    `health.py` classifies a credential from real turns; this answers before any
    turn has run, which is the only moment a deploy can act on. Nothing here
    reads a credential's value and nothing prints one: a report is something
    people paste into a channel.

    The same three rules as `install-service.sh`'s preflight, deliberately a
    second implementation -- that script runs before the virtualenv exists, so
    it cannot call this. Both are pinned by tests, which is the only thing that
    keeps two copies honest.
    """
    api = bool(str(env.get("ANTHROPIC_API_KEY", "")).strip())
    oauth = bool(str(env.get("CLAUDE_CODE_OAUTH_TOKEN", "")).strip())
    stored = Path(home).expanduser() / ".claude" / ".credentials.json"
    found: list[str] = []
    if api and oauth:
        found.append(
            "both ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN are set. They "
            "conflict; keep exactly one."
        )
    if (api or oauth) and stored.exists():
        found.append(
            f"a stored login at {stored} takes precedence over the credential in "
            "the environment, so the one in .env is ignored until that login "
            "stops refreshing -- weeks later, with a good credential two feet "
            "away. Keep one."
        )
    if not api and not oauth and not stored.parent.is_dir():
        found.append(
            "no credential: neither ANTHROPIC_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN "
            f"is set and there is no {stored.parent}. The SDK spawns the claude "
            "CLI, which has to be signed in, so every turn would fail."
        )
    return found


# ========================================================= the two reports


def posture(policy: policy_mod.RulesPolicy, settings: Settings) -> list[Finding]:
    """Family A, in the order a reader wants: who, then what, then where."""
    found = [
        Finding("posture", "allow-without-users",
                f"rule {name!r} names no users, so it allows anyone the transport "
                "can carry.")
        for name in allows_without_users(policy)
    ]
    found += [
        Finding("posture", "unbounded-tool-allow",
                f"rule {name!r} allows every tool there is, because it names none.")
        for name in unbounded_tool_allows(policy)
    ]
    found += [
        Finding("posture", "allow-outside-roots", f"rule {name!r} {why}.")
        for name, why in allows_outside_their_roots(policy, settings)
    ]
    found += [
        Finding("posture", "allow-by-argv-shape",
                f"rule {name!r} allows by argv shape, which is sound as a deny and "
                "unsound as an allow: the patterns are prefix-shaped.")
        for name in allows_matching_commands(policy)
    ]
    found += [
        Finding("posture", "unbounded-commands",
                f"name {name!r} may run a shell and no deny narrows what runs.")
        for name in names_with_unbounded_commands(policy, settings)
    ]
    found += [
        Finding("posture", "missing-ceiling",
                f"name {name!r} has no {which}, so a turn on it is unbounded.")
        for name, which in names_without_ceilings(settings)
    ]
    found += [
        Finding("posture", "unnamed-rule",
                f"rule {position} has no name, so no finding about it can say "
                "which rule it means.")
        for position in unnamed_rules(policy)
    ]
    # Set here rather than on seven call sites: one omission there would make a
    # dial position fail a deploy, which is the failure this field exists to
    # prevent.
    return [replace(one, gating=False) for one in found]


def wholeness(
    settings: Settings,
    policy: policy_mod.RulesPolicy,
    grants: Sequence[Grant] | None,
    *,
    env: Mapping[str, str],
    home: str | os.PathLike[str],
) -> list[Finding]:
    """Family B: the configured world, checked against itself and the store.

    `grants` is None when the store could not be read, which is emphatically
    not the same as a store with nothing in it: the four assertions that need
    it are skipped rather than answered, and the caller has to say so.
    Reporting "every name is ungranted" because a database would not open would
    send somebody to re-seed a store that is fine.
    """
    names = [a.name for a in settings.agents]
    found = [
        Finding("wholeness", "dead-rule",
                f"rule {name!r} names agents that the catalogue does not declare, "
                "so it can never match -- and a deny that cannot match allows "
                "what it meant to refuse.")
        for name in dead_rules(policy, names)
    ]
    found += [
        Finding("wholeness", "unrostered-owner",
                f"owner {email} of {name} is not on the [[identity]] roster, so "
                "they cannot act as an owner until they are added.")
        for name, email in unrostered_owners(policy)
    ]
    if grants is not None:
        found += [
            Finding("wholeness", "name-without-grants",
                    f"name {name!r} is in the catalogue and has no grant row, so "
                    "nobody can address it. `ricardi grants add`, or `grants seed` "
                    "for what policy.toml already permits.")
            for name in names_without_grants(names, grants)
        ]
        found += [
            Finding("wholeness", "grant-for-unknown-name",
                    f"the store holds grants on {name!r}, which the catalogue does "
                    "not declare, so those rows grant nothing.")
            for name in grants_for_unknown_names(names, grants)
        ]
        found += [
            Finding("wholeness", "granted-person-off-roster",
                    f"{person} holds grants and is not on the [[identity]] roster, "
                    "so they are refused before the grant is ever read.")
            for person in granted_people_off_roster(policy, grants)
        ]
        found += [
            Finding("wholeness", "inert-identity",
                    f"{email} is on the roster and holds no grant, so they can ask "
                    "for nothing yet.",
                    # Reported, never gating: somebody added to the roster
                    # before anyone grants them anything is in the correct
                    # state, and a deploy must not refuse to land on it.
                    gating=False)
            for email in inert_roster_identities(policy, grants)
        ]
    found += [
        Finding("wholeness", "credential", text)
        for text in credentials(env, home)
    ]
    return found
