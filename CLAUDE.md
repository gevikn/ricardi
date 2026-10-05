# Working in this repository

This project exists to constrain what a Claude Code agent may do. If you are
Claude working on it, that makes a few things more important than usual.

## Before claiming anything works

Run the offline suite. It is fast and there is no excuse for skipping it:

```bash
pytest
```

The live suite spends real tokens and is opt-in:

```bash
RICARDI_LIVE=1 pytest -m live
```

**Run it before touching anything in `policy.py`, `audit.py`, or
`backends/sdk.py`.** Every serious defect this project has had was invisible to
the offline suite and obvious within one live run:

- The tool gate was never invoked, because `allowed_tools` auto-approved first.
  127 offline tests passed against an enforcement point that did not run.
- The shell tool is `PowerShell` on Windows, not `Bash`, so every rule naming
  `Bash` covered nothing.
- A test asked an agent to `echo` a string it could simply predict, so it
  measured nothing while appearing to pass.

Unit tests that call `_gate` directly prove the logic is right. They say nothing
about whether the SDK ever calls it. Only a live run does.

## Rules that are not negotiable

**Never weaken a policy test to make it pass.** If a test says a command is
denied and it now runs, the bug is the code, not the test. The tests are the
product here.

**Never add anything that can auto-approve a tool.** No `allowed_tools`, no
`permission_mode`, no `setting_sources` that loads user or project settings. Any
of them silently disables the gate. If you believe you need one, you have found
a design problem, not a configuration one.

**Default deny, everywhere.** A new condition that cannot be evaluated must make
a rule *not match*, never match-and-allow. When in doubt, deny and say why.

**Do not log raw tool input.** `audit.py` summarises and truncates on purpose. A
log that records every argument eventually records a password.

## Where things belong

| File | Says |
| :- | :- |
| `agents.toml` | what an agent **is** — name, cwd, system prompt |
| `policy.toml` | who may ask, and what may **run** |

Nothing in `agents.toml` grants anything. It refuses to load if it still carries
`allowed_tools` or `permission_mode`, rather than accepting keys that look like
they grant something and do not. Keep it that way: one reviewed file grants
everything.

The two seams — `transports/` and `backends/` — exist so neither end knows the
other. A change that makes the Slack transport aware of agents, or the SDK
backend aware of Slack, is going the wrong way. `Caller` is how context crosses
that line.

## Style

Comments explain **why**, not what. The code says what it does; the comment is
for the decision behind it, the alternative rejected, or the bug it prevents.

Prefer a test that documents a limitation over a comment claiming there is none.
`tests/test_policy.py` contains cases asserting which shell evasions *get
through* — that is deliberate and should stay.

## Honest reporting

If a live test fails, say so with the output. If something is unverified, say it
is unverified. This repository is a permission boundary; "it should work" is not
a claim anyone can use.
