# Ricardi

[![tests](https://github.com/gevikn/ricardi/actions/workflows/tests.yml/badge.svg)](https://github.com/gevikn/ricardi/actions/workflows/tests.yml)


A Slack bot that is a proxy to Claude Code agents running on a box. Humans and
Ricardi share a channel; Ricardi holds the agents and carries messages both
ways.

> **Everyone on the roster can run code on your machine.** That is what Ricardi
> is for rather than a flaw in it — and its safety layers err toward preventing
> accidents, not toward withstanding somebody who is trying. A read-only tier
> stops a colleague changing something by mistake; it does not stop a colleague
> who means to, because a shell can cause a write indirectly whatever the rules
> say. **Do not invite people you would not hand a terminal.**
> [`SECURITY.md`](SECURITY.md) is the rest of it and is worth reading before you
> point this at anything you care about.

```
@ricardi list agents
@ricardi tell shared to summarise what changed this week
```

With one agent in the catalogue there is nothing to pick, so the name is
optional and `@ricardi what changed this week` reaches it. Naming an agent is
how you say which repository you mean; with one, that question has a single
answer.

Once a thread is talking to an agent, replying in the thread is enough — no
mention needed.

A request can give something up on the way in:

```
@ricardi -ro tell shared what does the policy engine do
@ricardi -dry tell shared to fix the failing backup test
```

`-ro` refuses every tool that could change anything; `-dry` lets it plan and
say what it would have run. **A flag only ever subtracts** — it narrows what
policy already allows you, and no flag can grant anything. That is what makes
it safe to add more of them.

A flag sticks to the thread rather than the message. You can open a thread
`-ro` and walk away, and it still holds when a collaborator replies without
repeating it.

## How it fits together

Two seams, so neither end knows about the other:

```
Slack  ──►  transports/  ──►  app.py (one turn)  ──►  backends/  ──►  Claude Code
            slack.py          router.py (pure)        sdk.py
            local.py          store.py (threads)      fake.py
```

- **transports/** — where the humans are. `slack.py` speaks Socket Mode;
  `local.py` is plain HTTP, for driving the whole thing without Slack.
- **backends/** — where the agents are. `sdk.py` owns real Claude Code sessions
  through the Claude Agent SDK; `fake.py` answers without spending tokens.
- **router.py** — turns a message into one decision. Pure, no I/O, no model.

Socket Mode matters: it dials **out** to Slack, so the box needs no public
hostname, no inbound port and no request-signature checking.

### The one thing to understand about the agents

The Agent SDK drives sessions *it* starts. Each agent here is a `claude`
process Ricardi spawned and holds open, which is why the answer comes back on
the same call and why nothing about this is undocumented.

It cannot attach to a session you started by hand in a terminal. Resuming one
by id does not join it — it starts a second process replaying the same
transcript, and you end up with two agents editing one working tree. So the
agents Ricardi offers are Ricardi's own, declared in `agents.toml`.

Agents are child processes: if Ricardi dies they die with her. The transcripts
survive, and Ricardi keeps each agent's session id in `state/sessions.json`, so
a restart resumes the same conversation instead of starting the agent over.
Both halves are needed — a surviving thread binding routes the next message to
the right agent, and only the session id makes that agent remember what was
said. Losing either one looks identical from Slack.

## Run it without Slack

This is the whole system minus the Slack hop, and it needs no credentials:

```bash
uv venv && uv pip install -e ".[sdk]"
cp agents.example.toml agents.toml
cp policy.example.toml policy.toml

# The example agents work in scratch directories, which is the habit worth
# keeping. Create them, or edit the cwd values to point somewhere you have.
mkdir -p ~/repos/shared

python -m ricardi.cli --transport local --backend fake --port 8809
```

Then drive it:

```bash
curl -s localhost:8809/health
curl -s -XPOST localhost:8809/say -d '{"text":"list agents","thread":"T1"}'
curl -s -XPOST localhost:8809/say -d '{"text":"tell shared to read README.md","thread":"T1"}'
curl -s localhost:8809/replies
```

The example policy includes one rule letting the local stub's default user ask
things, so this works before any Slack app exists. **Delete that rule and its
roster entry** once you move to Slack — it is scoped to a channel named `local`,
which Slack can never produce, but it has no business in a real policy.

Swap `--backend fake` for `--backend sdk` and the same calls go to real Claude
Code sessions.

## Tests

```bash
pytest                                  # no tokens, no network, no Slack
RICARDI_LIVE=1 pytest -m live           # real Claude Code sessions
```

The offline suite covers the command surface, the turn logic, thread bindings,
the catalogue's validation, both enforcement points, Slack's event shapes on
real payloads, and the full path over real HTTP.

The live suite starts actual agents in scratch directories and checks they read
their own files, remember the previous turn, stay out of each other's
directories, and surface a hung turn instead of going quiet. It also checks the
gate against a live session: that a forbidden command does not run, that a
permitted one does and is announced first, and that a write outside the allowed
root never lands.

## Deploying it

```bash
cp .env.example .env              # Slack tokens, and a credential for Claude
cp docker/agents.example.toml agents.toml
cp docker/policy.example.toml policy.toml
mkdir -p state claude-config workspace

docker compose up -d --build
```

That assumes an `ANTHROPIC_API_KEY` in `.env`, which is what a box left running
wants: it expires when you rotate it rather than on a clock you cannot see. To
use a subscription login instead — and why not to use both — see
[`docs/running.md`](docs/running.md).

No ports are published: Socket Mode dials out, so nothing needs to listen. The
container is also the only deployment where the agent's reach is enforced by
something other than a config file — it sees the directory you mounted at
`/workspace` and nothing else on the machine.

systemd and Windows are in [`docs/running.md`](docs/running.md).

## Slack setup

The only part that needs a human. From <https://api.slack.com/apps>:

1. **Create an app** — any workspace member can. Pick a development workspace.
2. **Socket Mode** → enable it. Basic Information → App-Level Tokens → generate
   one with scope `connections:write`. That is `SLACK_APP_TOKEN` (`xapp-…`).
3. **Event Subscriptions** → subscribe to `app_mention`, plus `message.channels`
   for public channels and `message.groups` for private ones. Without the
   `message.*` event, mentions still work but replying in a thread does not.
4. **OAuth & Permissions** → bot scopes `app_mentions:read`, `chat:write`, and
   `channels:history` (public) / `groups:history` (private).
5. **Install to workspace.** This is the only step that can need an admin: if
   the workspace has app approval turned on, an owner or admin has to approve
   it. On Enterprise Grid it can need an org admin.
6. Copy the bot token (`xoxb-…`) into `.env` as `SLACK_BOT_TOKEN`.
7. In Slack, `/invite @ricardi` into a channel.

```bash
cp .env.example .env    # then paste both tokens in; .env is gitignored
python -m ricardi.cli --transport slack --backend sdk
```

Creating your own free workspace avoids the approval gate entirely while you
are still shaking bugs out.

## Policy

Two questions, one decision point in `policy.py`:

| Enforced at | Question |
| :- | :- |
| the inbound message | may this person address this agent in this channel? |
| every tool call | may this agent run *this tool with these arguments*, for this person? |

The second is the one that matters: what two engineers agreed Claude may do on
their box becomes executable rather than written down. A refused tool carries a
reason the model reads, so the agent adapts instead of flailing at the same wall.

It is wired as a **`PreToolUse` hook**, not `can_use_tool`, and that distinction
is the whole ballgame. The SDK auto-approves anything an `allowed_tools` entry
covers *before* consulting `can_use_tool`, so a gate built on that callback
silently never runs for exactly the tools you listed — it looks like it is
working, and it is not. Allow rules in settings files shadow it the same way. A
`PreToolUse` hook fires unconditionally. So Ricardi passes no `allowed_tools`,
no `permission_mode`, and `setting_sources = []`: nothing exists that could
auto-approve, and the hook is the only thing that can grant a tool.

That is also why `agents.toml` no longer takes `allowed_tools` or
`permission_mode` — it refuses to load if they are present, rather than
accepting settings that look like they grant something and no longer do. One
reviewed file grants everything.

**Everything fails closed.** No policy, an unparseable policy, a policy that
raises, a condition that cannot be evaluated, and a request no rule mentions all
deny. Deny overrides allow, so there is no rule ordering to get wrong.

Identity is a roster in `policy.toml` mapping Slack user IDs to emails — no
directory lookups, so no extra Slack scopes, no reinstall, and nothing that can
change under you without a reviewed diff. See `policy.example.toml`.

`[limits]` in `policy.toml` caps how many turns one person may start, and
`max_budget_usd` / `max_turns` in `agents.toml` cap what one turn may cost and
how long it may run. Both matter on a machine left running: the first bounds how
many turns there are, the second bounds each one. A turn stopped by a ceiling
says so rather than letting a truncated answer read as a finished one.

`python -m ricardi.report` reads the audit log back: what ran, what was
refused, and which rule refused it.

An `announce` obligation posts a command into the thread before it runs. It is a
**record, not a checkpoint** — nothing waits for a reply and there is no way to
veto from Slack. It makes use visible, not reversible. The durable version is
the audit log at `state/audit.jsonl` (rotated by size), one JSON object per decision, including
everything that was refused and so never appeared in the channel at all. Tool
inputs there are summarised and truncated, because a log that records every
argument eventually records a password.

### A trap worth knowing

**The shell tool is named per platform** — `Bash` on macOS and Linux,
`PowerShell` on Windows. A rule naming only one covers nothing on the other.
For an allow that is merely surprising (the agent gets refused). For a **deny**
it is dangerous: scope `git push` to `Bash` on a Windows box and it does not
apply. So deny rules here name no tool at all and match on the command, which
catches whichever shell is in play.

### What policy is not

`command_matches` will not stop someone determined. No list of shell patterns
survives `$(…)`, pipes, or a base64 payload, and anyone claiming otherwise is
selling a feeling. It catches honest mistakes and makes intent legible.

The real boundary is structural, and policy is the second line behind it:

- Give the experiment agent **its own git worktree with no push credentials**.
  Then "don't push to main" isn't enforced, it's impossible.
- Point `cwd` at nothing you'd mind losing, and nowhere near a `.env`.
- Use the SDK's `sandbox` option rather than inventing one.

## agents.toml

The agents Slack can reach are declared, not discovered — a fixed list gives
the channel stable names, and pins each agent's working directory and tool
allowance ahead of time rather than leaving it to whoever is typing. See
`agents.example.toml`.

Two things to get right:

- **`permission_mode`.** Nobody is at a terminal to answer a permission prompt.
  An agent whose tools need approval will stall until the turn deadline.
- **`cwd` and `allowed_tools`.** This is the security boundary. An agent with
  `Bash` can do whatever the box's user can, at the request of anyone in the
  channel.

## Documentation

| | |
| :- | :- |
| [`SECURITY.md`](SECURITY.md) | the threat model, and what silently disables enforcement |
| [`docs/running.md`](docs/running.md) | keeping it running: Docker, systemd, Windows |
| [`docs/practices.md`](docs/practices.md) | how to scope agents and review policy |
| [`docs/roadmap.md`](docs/roadmap.md) | what is planned, including Teams |
| [`docs/name.md`](docs/name.md) | why a transparent octopus |
| [`requests/README.md`](requests/README.md) | asking for a shared session, as a reviewed pull request |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | how to work on it |
| [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) | argue about the work, not each other |

## Known limits

- A turn is delivered as one message at the end. Progress is not streamed, so a
  long turn shows only "still working" until it finishes. Everything the agent
  said is kept rather than just its closing line, which can be verbose; output
  past 3500 characters is truncated with a note.
- One turn at a time per agent, since a session is a single conversation. A
  second person asking the same agent waits behind the first.
- `tell`/`ask`/`<agent>:` is parsed exactly, not interpreted. A sentence like
  `note: the build is red` is read as addressing an agent called `note`. With
  a choice of agents that is an error; with only one there was nowhere else it
  could have been going, so it is sent there as typed.
- Progress inside a turn is not streamed; only announced commands appear while
  work is happening.
- Thread bindings are never evicted, so `state/threads.json` grows with every
  conversation. Harmless at this size, but it is a file that only grows.
