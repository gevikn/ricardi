# Contributing

Thanks for looking. This is a small project with a narrow job, and it would
rather stay small.

## Getting set up

```bash
uv venv && uv pip install -e ".[slack,sdk]"
cp agents.example.toml agents.toml
cp policy.example.toml policy.toml
pytest
```

The offline suite needs no tokens, no network and no Slack. If it does not pass
on a clean clone, that is a bug worth reporting on its own.

The live suite starts real Claude Code sessions and spends real tokens:

```bash
RICARDI_LIVE=1 pytest -m live
```

## How this is built

Two seams, so neither end knows the other:

```
Slack  ──►  transports/  ──►  app.py  ──►  backends/  ──►  Claude Code
```

A change that teaches the Slack transport about agents, or the SDK backend
about Slack, is going the wrong way. `Caller` is how context crosses that line.

Policy is one decision point with two enforcement points — the inbound message
and every tool call. New enforcement points call `decide()`; they do not invent
their own rules.

## What a good change looks like

**It comes with tests, and the tests fail without it.** For anything touching
policy or the gate, that includes a live test, because this project's worst
defects have all been invisible offline. `CLAUDE.md` lists them; they are worth
reading before you start.

**It fails closed.** A new condition that cannot be evaluated makes a rule not
match. It never matches-and-allows.

**It does not add anything that can auto-approve a tool.** No `allowed_tools`,
no `permission_mode`, no settings sources. Each silently disables the gate. If
you think you need one, that is a design problem.

**It documents limitations rather than implying there are none.** There are
tests in `tests/test_policy.py` that assert which shell evasions *get through*.
Adding to that set is a contribution; quietly deleting one is not.

**Comments say why.** The code already says what.

## What is likely to be declined

- A richer rule language. If rules need loops or arithmetic, the answer is the
  external PDP hook, not a bigger DSL.
- Anything that makes `agents.toml` able to grant permissions again. One
  reviewed file grants everything, on purpose.
- Features that need new Slack scopes, unless the capability genuinely cannot
  be had without them. Avoiding a reinstall is why identity is a static roster.
- Streaming an agent's working output into the channel. It reads well in a demo
  and is noise in a real channel.

## Pull requests

Branch from `main`, keep the change focused, and say in the description what you
ran — including whether the live suite passed, and on which platform. Windows
and Unix differ in ways that matter here: the shell tool has a different name on
each.

Every PR is reviewed before merge, including ones written with Claude's help.
If Claude wrote part of a change, keep the `Co-Authored-By` trailer on the
commit; attribution is cheap and honest.

## Reporting a problem

Security issues: see `SECURITY.md`. "Policy said deny and the tool ran anyway"
is the most valuable report this project can receive.

Everything else: an issue with what you expected, what happened, your platform,
and the relevant lines from `state/audit.jsonl` if a decision was involved.
