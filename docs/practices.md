# Running this well

Opinions from using it, in rough order of how much trouble they save.

## Start read-only and stay there longer than feels necessary

A `scout` that can only `Read`, `Grep` and `Glob` answers most of what two
engineers actually ask each other, and the worst thing a bug in it can do is
say something wrong. Most teams who reach for a write-capable agent on day one
did not need one on day one.

Add `bench` when you have a specific experiment you keep wanting to run, not in
anticipation.

## Give each agent a worktree it can afford to lose

```bash
git worktree add ~/experiments/bench -b experiments
```

No push credentials in it. Then "do not push to main" is not a rule anyone has
to enforce — it is a thing that cannot happen. Policy is the second line and
should never be the only one.

This matters more than any rule you will write. A tight policy over `~/` is
worse than a loose policy over a scratch directory.

## Review policy.toml the way you review IAM

It is the file that grants everything. Three habits:

- **One change per PR.** "Added bench and also widened the shell rules" is how
  a widening gets merged because the feature looked fine.
- **Read deny rules first.** They are what stands between an honest mistake and
  a bad afternoon.
- **Name every rule.** The name lands in the audit log, and `rule: ""` at 2am
  tells you nothing.

## Prefer narrow allows over clever denies

`command_matches = ["pytest*", "python *"]` is worth more than a long deny list,
because a deny list has to anticipate and an allow list only has to permit. Keep
denies for the handful of things that must never happen regardless.

And know what they cannot do: `git -C /repo push` does not contain the string
`git push`. See `SECURITY.md`.

## Write `shell`, not a platform's name for it

`tools = ["shell"]` covers `Bash` on macOS, Linux and WSL and `PowerShell` on
Windows. Naming one of them directly means the rule does nothing on the other
platform — harmless for an allow, dangerous for a deny, and invisible either
way until someone runs the bot on the other machine.

Deny rules are better off still naming no tool at all and matching on the
command, so they catch anything that carries one.

## Set a ceiling on every agent

`max_budget_usd` and `max_turns` in `agents.toml` are per turn, and the SDK
enforces them. On a machine left running, "no limit" should be a number you
chose rather than one you never typed. A turn that hits a ceiling still reports
what it got to, marked as stopped early, so the channel can tell a short answer
from a finished one.

Each turn's cost goes to the log, so `grep "turn cost" state/service.log` tells
you where the money went.

## Keep the roster short and the channel private

The roster is identity. Two people who agreed on something is the use case this
was built for; twenty people who were added over time is a different system that
needs features this does not have, like rate limiting and per-agent approval.

One private channel per agreement beats one busy channel for everything.

## Read the audit log occasionally

`state/audit.jsonl`, one object per decision. The interesting lines are the
denials, because they are invisible in Slack. A cluster of them usually means
either someone is hitting a limit they did not know about, or an agent is
circling something it should not be near. Both are worth knowing.

```bash
grep '"allow": false' state/audit.jsonl | tail -20
```

## Let the agent see its own limits

A denied tool gets a reason the model reads. Write deny reasons that tell it
what to do instead — "ask a person to run it" beats "forbidden" — and the agent
stops, says so, and reports back. Vague refusals produce agents that try six
other routes to the same place.

Say so in the system prompt too: *if something is not permitted, say so and stop
rather than looking for another way around*.

## Do not stream progress into the channel

It reads well in a demo and is noise in a real channel. The announced commands
are the useful signal; everything else belongs in the log.

## The tools on offer differ by platform, so read-only is not read-only

With the same policy on Windows and Linux the decisions match — there are tests
pinning that. **Which tools exist does not match.** Asked to list a directory
with every tool permitted, a Linux session reached for the shell and nothing
else; `Glob` and `Grep` were not in its tool set at all. On Windows they were.

The consequence catches the obvious read-only policy:

```toml
tools = ["Read", "Grep", "Glob"]   # thorough on one platform, nearly empty on another
```

On a machine without `Glob` and `Grep`, that agent can read a file whose name it
is given and discover nothing by itself. It will say so clearly — the refusals
are good — but it will not be the agent you thought you configured.

So: `Read` is the one you can rely on. If an agent needs to find things, give it
a narrow shell as well, which is why the shipped example permits `ls`, `cat`,
`dir`, `type` and `Get-Content` alongside the read tools. And check
`python -m ricardi.report` on each machine after a week: an allowed tool that
never appears under "Tools used" on one of them is this, happening quietly.

## Restart it on purpose, early

Ricardi's agents are child processes, so stopping it stops them. The transcripts
survive and sessions resume by id, but you want to find that out on a Tuesday
afternoon rather than mid-incident. Run it as a service before you depend on it.
