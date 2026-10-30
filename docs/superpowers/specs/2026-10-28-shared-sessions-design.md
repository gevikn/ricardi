# Shared sessions

Design, 2026-10-28. What Ricardi is for, and what has to be built to deliver it
as a service rather than one person's box.

Provenance: decisions attributed to "the owner" below were made in conversation
on 2026-10-28. Calls marked **(builder's call)** were made in this document and
are open to revision. Findings cited as H1–H5, F1–F2, R1–R7 and L0–L4 come from
the access simulation, which is runnable — see "Evidence" at the end.

---

## 1. Intent

> I (and others) often want to invite a person or two — generally not a large
> group — to our Claude session, where they can see the back and forth and
> ideally interact with it.

That sentence is the product. Everything below is machinery in service of it.

**In one line:** make a Claude Code session multiplayer for a handful of invited
people, on a machine the owner controls, without anyone having to trust the thing
in the middle.

### Why it cannot be "share the session I am already in"

The Agent SDK drives sessions *it* starts. More fundamentally, Ricardi's
enforcement exists *because* it owns the process: `sdk.py:187` installs a
`PreToolUse` hook on a session it spawned, and that hook is what announces
commands before they run and refuses what policy denies. Nothing Ricardi did not
spawn has a hook on it.

**You can share the session you are already in, or you can have policy. Not
both.** Claude Code Channels and the peer-messaging socket deliver visibility and
interaction without enforcement, which makes them right for inviting a trusted
colleague to watch and wrong for a session with a budget, a path boundary and an
audit trail. That session has to be Ricardi's from birth.

The price is intentionality: the owner works in the shared session from the
start. See §11 for the one friction worth removing (it is not intentionality).

---

## 2. First principle: an accident boundary, not an adversary boundary

**Do not invite people you do not trust.**

Ricardi's safety layers err toward caution and accident prevention. They are not
built to withstand someone adversarial who has already been invited, and they
cannot be, for a structural reason: one session is one process with one
filesystem view, so a reader and a writer on the same name share the same mount.
Per-person filesystem permissions do not exist inside a shared session (H5, L1).

This belongs in the README, not only here:

> Invitees can cause writes indirectly. A read tier stops a colleague changing
> something by accident. It does not stop a colleague who is trying.

It is a deliberate choice. A boundary that held against an adversary would cost
the shared transcript, which *is* the product.

**But the dial must exist.** An operator needing a higher bar must be able to
write policy for it. §9 lists what already supports that and the four things that
do not.

---

## 3. Goals, non-goals, anti-goals

### Goals, ranked

1. **Sharing with specific invited people.** Small groups. The owner invites.
2. **Transparency over trust.** Every command announced before it runs; every
   decision, including the refusals nobody sees, in an append-only log; the
   ceiling a file two people can diff. Test for any feature: can you still
   answer "why was this allowed?" at 2am.
3. **A thin centre and thinking arms.** Ricardi resolves who, which, and
   whether. `router.py` stays pure and is deliberately not a model.
4. **Subtraction only, fail closed.** A flag only takes away. An unmatched
   request denies. An unknown flag is refused rather than dropped. This is why
   runtime-mutable grants are acceptable — *provided they can only narrow an
   immutable ceiling.*
5. **Filesystem first, policy second.** The `/workspace` mount is the boundary;
   `path_under` is the backstop behind it. Prefer impossible over forbidden.

### Non-goals

No model in the routing layer. No inbound endpoint. Not a person. Agents are
**repositories, not roles**. No Discord or Telegram transport — though the
roadmap heading "Already solved elsewhere" overstates the case and should be
reworded: Channels solves the *transport*, and nothing above it (no named
catalogue, no enforcement at the point a tool runs, no tiers, no
announce-before-run, no audit, no budget ceilings).

### Anti-goals — failure even if it works

- A permission exists and nobody can trace where it came from.
- A refusal is silent. (Already been bitten: a denial delivered, correct and
  invisible, indistinguishable from a dead bot.)
- An agent does something permitted that nobody reading the rules predicted.
- A convenience quietly widens reach.
- **Friction that gets routed around.** A system people bypass — by SSHing to
  the box, by asking a colleague to run it, by granting themselves `write`
  permanently because the read tier could not run tests — is worse than a looser
  one they use.

---

## 4. The model

```
[workspace] 1:N [channel] 1:1 [name] 1:1 [Claude session] 1:N [repos / cwds]
                                      |
                                      +-- N:1 [owner]           who may invite and delegate
                                      +-- N:1 [ClaudeAccount]   what it may spend
                                      +-- N:1 [GitIdentity]     what it may push -- default: none
```

**The name is the unit of identity, memory and scope. Scope lives on the name;
verbs live on the person.**

### One channel, one name

A name belongs to exactly one channel. *"You want to talk to that session? Join
that channel."*

The reason is not tidiness. A session is **serial**: turn N+1's context is the
result of turn N, so two simultaneous turns on one session are not a concurrency
problem to optimise — they are incoherent, and they would have the agent editing
files under itself. That is the same objection the README raises to `resume`:
*"two agents diverging in one working tree."* The per-agent `live.lock` is
Ricardi **admitting** that constraint, not imposing one. Different names already
run in parallel; one name cannot.

Given a name has one queue, giving it one channel is honest. This deletes:

- Which channel sees the transcript — answered by construction.
- Cross-channel memory leakage — impossible.
- A name attached to two channels bridging two sets of humans.
- `attach` / `detach` as ongoing verbs — they become provisioning, done once.
- A `-n <name>` selector and a user-scoped `--list`. Both existed only because a
  channel could face several names. What survives is smaller and different: a
  **directory** (which sessions am I in), not a selector.
- Most of `ThreadBindings`. Threads still carry sticky *flags*; "which agent is
  this thread talking to" is answered by the channel.

Cost: consulting a session without joining its channel. That is exactly the
context bridge that leaks, so refusing it is the point.

**Not fixed by this: the queue.** Two people in the same channel still wait on
the same name. That is now obviously correct rather than mysterious — a shared
terminal has one prompt, and everyone can see whose turn is running.

### Membership versus grant

1:1 makes it tempting to say channel membership *is* the permission. Keep them
separate:

- **Membership** grants presence: you can see the session. Slack enforces this.
- **A grant** sets the tier: none, read, write, exec.

An owner inviting does both in one act — adds to the channel *and* records the
grant. Someone added by anyone else gets presence and nothing more. This is what
keeps H1 closed: otherwise whoever can add a Slack member can confer capability,
which is the opposite of "I invite a person or two."

### Per-edge state today

| Edge | State |
| :- | :- |
| workspace 1:N channel | free: one token pair per process |
| channel 1:1 name | **to build.** Today every channel sees the whole catalogue — M:N by accident. |
| name 1:1 session | **already exactly this.** `SessionIds` is keyed by agent name and resumes every turn. |
| name 1:N repos/cwds | to build: `AgentSpec.cwd` is one string, `path_under` one root |
| name N:1 owner | **does not exist.** The node the model was missing. |
| name N:1 ClaudeAccount | one key per process today. `ClaudeAgentOptions` has an `env` field and `sdk.py:194` builds a client per agent, so the seam exists. |
| name N:1 GitIdentity | deliberately absent; `practices.md` says keep it that way |

---

## 5. Two administration planes

The origin story's best structural idea. It dissolves the earlier `-n setup`
sketch, where a god agent addressable from a channel needed `cwd = /workspace`,
so whoever reached it reached every project.

| Plane | Where | Does |
| :- | :- | :- |
| **admin** | outside Slack | mints Claude keys, creates repos, lays out disk, registers names, writes the ceiling |
| **owner** | in Slack | invites and sets each invitee's tier, delegates, sets up a repo — bounded by what admin provisioned |

**The box never mints a credential.** Provisioning may *require* a key; it must
never create one. An Anthropic Admin credential in the container would be
something that can create spend — far worse to hold than any single API key.

---

## 6. Where configuration lives

Two things with different lifecycles have been sharing one file.

**The ceiling** — `path_under`, the credential denies, `*.pem`,
`/config/claude/*`, budget caps, and who may grant at all. Changes rarely, must
be reviewed. Stays `policy.toml`, stays mounted `:ro`. Docker enforces the outer
bound, which is stronger than any check we could write.

**The grants** — who holds which verb on which name; which name belongs to which
channel. Many writers, constantly changing. **SQLite in `state/`.** Standard
library, no new service, no port, no credential, so it keeps the *"No ports.
Socket Mode dials out, so nothing needs to listen"* property that is most of why
this deployment is simple. Real transactions, so a grant and its audit row commit
together. One file, so a snapshot is a single `.backup` call. `.dump` gives a
text diff.

**Correction, 2026-10-30:** an earlier version of this line said *"so restic
already backs it up"*. That was false for this deployment and stayed false
until the day the store became load-bearing: the restic sources cover the
owner's laptop, not `/opt/ricardi`, and `state/` is the one directory the
deploy promises never to touch, so nothing else copied it either. Backing it
up is `ricardi grants backup` on a timer; see `docs/deploying.md`.

The invariant that makes runtime mutation safe is one this codebase already
states three times — in the flags, in provisioning, and in tiers: **the mutable
layer can only ever subtract from an immutable layer the process cannot write.** A
grant is intersected with the ceiling, so a buggy or compromised mutation path
can only produce something *too tight*.

Three properties to build in from the start:

- **Append-only.** Never `UPDATE` or `DELETE` a grant; insert a superseding row.
  The toml file answered "why can C1 do this?" for free; a mutable table does not
  unless history is the storage model.
- **Fail closed on corruption, unlike the rest of `state/`.** `ThreadBindings`
  and `SessionIds` deliberately shrug off a corrupt file — *"must not stop
  Ricardi answering."* A corrupt **grant** store must refuse to start, as
  `PolicyError` does. Same directory, opposite rule.
- **No cache.** In-process SQLite is microseconds, so the decision point reads it
  per call.

Postgres is wrong for a table with dozens of rows: a service, a port, a
credential, a second backup story. If it is ever needed, `PolicyPoint` is already
a `Protocol` *"so a real policy service can replace the rules engine without
anything above it changing."* Do not spend that escape hatch now.

---

## 7. Verbs

Two planes. Mixing them is what made this slippery.

### Plane 1 — what the agent may do, when *you* ask

Enforced per tool call today; what is missing is binding it to a person.

| Verb | Tools |
| :- | :- |
| `use` | may address the name at all (today's `ask`) |
| `read` | Read, Grep, Glob — already `READING_TOOLS` |
| `write` | Edit, Write |
| `exec` | Bash, PowerShell — already its own category via `SHELL_ALIAS` |

**Not a ladder.** `exec` is not "write, but more" — a shell reads, writes, reaches
the network and spends money. Both useful middles exist: write-without-shell, and
exec-without-write (run the suite on a tree you cannot change).

### Plane 2 — what *you* may do to Ricardi

New. `Request.action` is already a plain string and `Rule.actions` already matches
on it, so each of these is a new value at the **existing** decision point — same
audit trail, same fail-closed default. Because an unmatched request denies,
adding a verb to the vocabulary grants nobody anything until a rule names it. The
whole vocabulary can land before any of it is wired, with no flag day. (Verified:
the simulation's undesigned `attach` verb denies by default.)

| Verb | Reachable from | Means |
| :- | :- | :- |
| `list` | Slack | see the directory, scoped to you |
| `grant` / `revoke` | Slack | invite and disinvite, i.e. set a person's tier |
| `reset` | Slack | forget a name's session id (see §15.6) |
| `scope` | Slack | **narrow only** what a name may reach |
| `create` / `destroy` | **admin plane only, host-side** | register or remove a name |

`create` and `destroy` are **not** Slack-reachable, which the first draft of this
table got wrong. `agents.toml` is mounted `:ro`, validated once by `config.load`
at startup and never re-read, so a running container cannot register a name even
if policy allowed it. §5 already puts key-minting and repo creation outside
Slack; name registration belongs there with them. **(builder's call)**
Registering a name requires a restart — the deploy already recreates the
container, so this costs nothing and avoids a config-reload path that would have
to re-validate a live catalogue against running sessions.

`grant`, `revoke` and `scope` are the escalation verbs. Everything else is inert
by comparison.

### How a grant composes with the ceiling

The first draft claimed both that the mutable layer "can only ever subtract" and
that an owner's grant lets someone in. Those do not compose, and the resolution
matters enough to state precisely:

- **The ceiling grants to the *name*.** A reviewed rule says what the maximum
  envelope is for `n1`: which roots, which tools, up to which tier, what budget.
- **A grant assigns a *person* a point inside that envelope.** It is not an
  allow in the rule sense.
- **Neither permits anything alone.** The effective permission is the
  intersection, so a grant can only ever select at-or-below what the reviewed
  file already permitted for that name. Subtraction-only survives.

Mechanically, **the asker's tier becomes a field on `Request`, exactly as
`flags` already is.** The decision point reads the grant store, puts the tier on
the request, and ceiling rules match against it. That keeps enforcement inside
the audited fail-closed path rather than in application code — which goal 2
requires, since a permission decided outside `policy.decide` is one the audit log
cannot explain.

`Request` and `Rule` therefore need to carry, for plane-2 verbs, the **target
person** and the **tier being conferred**. Without them, `grant bob read on n1`
and `grant bob exec on n1` are indistinguishable at the decision point — both are
`action="grant", agent="n1"` — and "at or below the owner's own tier" (§7) and
"never widenable" (R7) cannot be enforced in policy at all. This is a step-1 data
model change, not a later edit.

**Delegation:** grants are delegable **by the owner of that name**, at or below
the owner's own tier, never above the ceiling. This is why the `owner` node had
to exist.

**`reset` and `destroy` are separate verbs with separate grants** (R6).
Detaching or forgetting is not the same as removing, and a transcript left intact
is re-exposed if the name is re-used.

**An owner must not be able to widen their own name** (R7). A name's path
ceiling comes from admin and is narrowable but never widenable by its owner —
otherwise an owner walks `cwd` out to `/workspace`, which contains every project,
since the Docker mount is the only hard bound.

---

## 8. Tiers, and how they are enforced

### The correction that shaped this section

An earlier sketch mapped GitHub `read` to a forced `-ro` flag. **That was wrong,
and `practices.md` had already said so.**

`-ro` permits only `Read`, `Grep`, `Glob`, `NotebookRead`. So a reader cannot run
`pytest`, `git log`, or `ls` — and `practices.md` states that *"'Summarise what
this repo does' sounds read-only and needs `ls`, `cat` and `git log`,"* then draws
the conclusion: *"Permission belongs in policy, where it can depend on who is
asking."* **A capability tier is a policy rule, never a forced flag.** (F1, F2.)

A second reason the forced-flag design was wrong: thread flags are deliberately
sticky and union-only. A per-person restriction written to the thread would leave
the thread narrowed for the *next* person, so one reader would poison the channel
for everyone. If any inherent restriction is ever applied, it is computed per
request from the principal and never stored.

### Why a command allowlist cannot express the read tier

`command_matches` globs the flattened command string. As a **deny** ("does this
look bad") it is sound, and that is how it is used today. As an **allow** ("is
this safe") it is unsound — a prefix match says nothing about what follows, and
an allow on `pytest*` matches `pytest` with a destructive command chained after
it (H4, proven).

It is worse than metacharacters. The commands that look safest already execute
code the repository controls: `pytest -q` was **demonstrated** writing a file
because it collects and runs `conftest.py`, with no metacharacter involved;
`git log` honours `core.pager` and aliases out of `.git/config`; every build and
test runner executes project configuration (H5). **A command allowlist bounds the
string, never the behaviour.**

### What actually separates the tiers

Having corrected away both mechanisms — forced `-ro` (F1, F2) and the command
allowlist (H4) — the first draft left `read` and `exec` with no enforceable
difference at all. The resolution is in what the owner said when the tiers first
came up:

> read means: I can talk to Ricardi and it can't do write operations on my
> behalf. write means: I can talk to Ricardi and I can have it write stuff. Then
> we need to think about execute on behalf of and **scope of execute (which is
> really a Claude session scope definition)**.

So **`exec` is not a person-tier at all — it is the name's scope**, which §4
already establishes lives on the name. That gives three person-tiers, not four:

| Tier | The agent may use | Enforced by |
| :- | :- | :- |
| **none** | nothing — presence only, see §4 | no grant row |
| **read** | `READING_TOOLS` + shell; **`Write` and `Edit` denied** | tool name, which is sound — unlike a command string |
| **write** | the above plus `Write` and `Edit` | tool name |

What the shell may then *do* is the name's business, not the person's: roots,
`command_matches` denies, budget, turns. One scope per name, applying to everyone
in its channel.

This is enforceable because it keys on **tool names**, which H5 does not
undermine — H5 defeats allowlists of command *strings*, not the tool allowlist
`READING_TOOLS` already is.

### The decision

Per §2, this is an accident boundary. So: **the read tier gets shell, and the
documentation says plainly that it is not airtight** — a reader cannot write
*directly*, and can cause a write indirectly through the shell. F1 and F2 stop
being problems. Defence rests on the layers below rather than on a rule that
cannot hold. But see §15.1: the SDK's own bash sandbox may make this a real
boundary rather than a documented caveat.

The alternative, for an operator who needs the read tier to hold against someone
adversarial, is a **separate name over a read-only mount** with its own session.
Expressible today, and it costs the shared transcript. That trade is real and
should be documented, not engineered around.

### Layers of defence

| Layer | Mechanism | Holds against |
| :- | :- | :- |
| **L0 — don't need it** | `Read`, `Grep`, `Glob` cover most listing and finding. Residual gap is narrow: `git log`, test output. | everything, by not running |
| **L1 — the filesystem** | a read-only mount; the indirect write becomes impossible | an adversary |
| **L2 — policy** | deny on argv *shape*: metacharacters, interpreters with inline code (`-c`, `-e`), heredocs. Porous by construction. | mistakes, known-bad shapes |
| **L3 — the hold** | announce, wait, proceed only if nobody objects | injection, when someone is watching |
| **L4 — containment** | budget and turn ceilings, no push credentials, append-only audit, a checkout you can afford to lose | assumes the above failed |

### The hold

The only layer that addresses prompt injection, and the answer to R1 — a reader
shares the session's memory with a writer, so a reader can leave instructions the
agent acts on when the *writer* next asks. No static rule separates a legitimate
`python -c` from an injected one; a human reading it often can.

**It is a later addition, not an architectural change.** Verified against
`claude_agent_sdk` 0.2.163: the gate is already `async`, already receives
`tool_use_id` so a pending hold can be keyed to one call, already announces via
`caller.notice`, and already denies with a reason the model reads — so a veto is
feedback, not a crash. And `HookMatcher` has a first-class `timeout` field
(`types.py:619`, default 60s), so the hold window is
`HookMatcher(hooks=[...], timeout=<window + margin>)`.

Two invariants to keep while building anything else, both free now and expensive
to retrofit:

- **A veto is handled in `app.py` as its own command and never relayed to the
  backend.** Otherwise it blocks on `live.lock` behind the very turn it is trying
  to stop — the stop message waiting for the thing it stops. `list` and `who`
  already demonstrate a command that never touches the agent.
- **A hold blocks the name.** If one thread should keep talking while another
  thread's call is held, *that* is the architectural change: per-call concurrency
  instead of per-agent, against "an arm does one thing at a time."

Open: who may veto, and what happens on timeout — both already named in the
roadmap, both unresolved here. See §12.

---

## 9. The paranoia dial

Already possible with no code change: mount a name's tree `:ro`; give a name no
shell rule; deny argv shapes with `command_matches`; split reader and writer into
two names over two mounts of one repo; per-name `max_budget_usd` and `max_turns`;
replace the engine via `PolicyPoint`.

Four gaps:

1. **The hold is not built, and cannot be reached by configuration at any
   setting.** The layer a paranoid operator wants most.
2. **No OS-level isolation between names** — *partly wrong, see §15.1.* One
   container, one uid (1000), and two names separated by `path_under` and nothing
   else. But `ClaudeAgentOptions.sandbox_settings` exists and is an L1-grade
   mechanism this design had not accounted for.
3. **"The asker must be known" is not expressible.** `Principal.known` is
   computed and never read, so an operator can avoid H1 only by never once
   forgetting `users` on an allow rule — they cannot *require* it.
4. **Budget ceilings are enforced in-process.** The external cap needs the
   per-name `ClaudeAccount` edge.

**The real gap is that the dial has no readout.** Every item above is "you can
write it correctly" rather than "it cannot be wrong" — the inverse of prefer
impossible over forbidden. Paranoia is achievable but not verifiable.

**(builder's call)** Add a posture report — `ricardi check --strict` over the
loaded config, asserting properties rather than describing rules: every allow
names `users`; every tool allow is bounded by the asking name's own `cwd`; no
allow uses `command_matches`; shell denied or argv-shape-denied; each mount
read-only; each name has a budget and a turn ceiling; every rule named. It reads
config that already exists, so it is cheap, and it is where the H1 and H2
regressions belong once fixed.

---

## 10. Identity

The roster is Slack user id to email, in `policy.toml`. Rules match on email.

**The Slack-to-GitHub join is collected at invite time**, from the person doing
the inviting, who knows both. This is better than the alternatives: GitHub will
not resolve a user by email unless it is public, and the one reliable automatic
mapping (SAML external identities / SCIM) needs Enterprise Cloud with SSO — on a
personal account there are collaborators but no teams and no SAML.

**GitHub permission is resolved once, at invite, and written as a grant** (R4).
Not looked up per request. A live lookup would make every repo decision depend on
an external service that `policy.py` is contractually required to fail closed
against — one GitHub outage would take Ricardi down completely — and a TTL cache
to avoid that buys revocation lag for no benefit. Resolving once gives no cache,
no lag, and an auditable grant. Re-check on demand.

### The endpoint, verified

`GET /repos/{owner}/{repo}/collaborators/{username}/permission`, documented as
"Get repository permissions for a user". Returns a `permission` field whose
values are **`admin | write | read | none`** — the coarse set — plus a `role_name`
carrying the finer roles. The first draft listed `maintain` and `triage` under
`permission`; those live in `role_name`.

Conveniently, the coarse four map exactly onto §8's three tiers:
`none` to none, `read` to read, and `write`/`admin` to write. No translation
table needed.

Two consequences that make the credential question **worse**, not better:

- **The caller needs push access to the repo to view anyone else's permission** —
  without it the call is a 403, *"Must have push access to view collaborator
  permission."* So Ricardi's credential must itself hold push access on every
  repo it resolves against. That is a far stronger credential than "read the
  permission of a user".
- **One source states the endpoint works for user accounts and not GitHub
  Apps.** If that holds, the scoped App-installation token recommended below is
  not available and a personal token is the only route — which carries the
  owner's whole reach into the container, the thing §5 says to avoid.
  **Verify this against the live GitHub docs before building.** It may be
  outdated, and it changes whether identity resolution can be done safely on the
  box at all, or must move to the admin plane.

GitHub permission is a good *necessary* condition and a bad *sufficient* one.
`write` means "may push to this repo"; it says nothing about whether Claude Code
may run a shell on *this* box or spend *this* budget. So it narrows; it never
grants alone.

---

## 11. Provisioning

The owner supplies: workspace name, session name, their own identity, the initial
invitee list (possibly empty, each with Slack id *and* git username), and
optionally a repo name with permissions — cloned if it exists, created with
defaults if not.

### The request surface: a reviewed pull request

**Built, 2026-10-28.** `provision.py`, `provision.example.toml`, `requests/`,
29 tests.

An owner cannot ask for a session in Slack, because what a request produces *is*
the channel — and §5's dangerous half must not be channel-reachable at all. The
owner is also usually not on the box. So the request is a **file in a pull
request**: the owner writes it, CI checks it, an admin reviews a diff and
presses merge.

That is not a workaround for lacking a UI. It is the same shape as the file that
grants everything else here, the escalation step is a human action, and it adds
**no inbound port** — which `compose.yaml` calls most of the reason this
deployment is simple.

Rejected, and why:

- **A web admin console.** Needs a listening port, sessions, its own authz, and
  it would put a remotely reachable path on `create` and `scope`. Admins are a
  handful of people doing rare operations; this is the wrong weight.
- **A CLI authenticating with a key.** A long-lived bearer token unlocking
  `create`, `scope` and `grant` would be the most valuable secret in the system,
  needing storage, rotation and revocation we would have to build. Still worth
  considering *without* the key: the box is already behind Cloudflare Access,
  which dials out exactly like Socket Mode and supplies a **verified email** —
  the same namespace the roster is keyed on. That is the route if an interactive
  surface is ever wanted.
- **A DM to the bot.** Dodges the chicken-and-egg and needs no new surface, at
  the cost of `im:history` and `im:write`. Cheapest way to test whether the flow
  is right, and the request format built here is reusable behind it.

The checker refuses what a human should not have to notice: an unaddressable or
reserved name, a collision with an existing session, a filename that disagrees
with the name inside, a person missing their git username, a tier outside
`read`/`write`/`none`, the same person listed twice, two people sharing an email,
the owner listed as their own invitee, a zero budget or turn ceiling, a repo that
is not `owner/repo`, and **any key it does not recognise** — so `teir = "read"`
fails rather than silently provisioning someone with no tier.

Two deliberate touches: a request for tier `exec` is refused with the reason
(*"what a shell may do is the scope of the session"*), so §8's decision is
encoded where someone would trip over it; and a test drives the real
`config.load` with an accepted name, so the two validators cannot drift and
leave a reviewed request to fail at the restart that would have applied it.

**Not built: the applier.** A merged request is an instruction to an admin, not
a trigger. Applying it needs the grant store and the answers in §14.

### Applying a request

**This is a job with partial-failure states, not a function** (R5). It spans:
mint key (admin plane), create or clone repo, lay out disk, register the name,
create the channel, invite people, record grants. Specific bad states to design
against:

- a name registered with no repo
- a channel full of invitees whose name does not resolve
- a key minted and attached to nothing
- a repo created with collaborators not yet added
- a name whose `cwd` does not exist — note `config.load` **refuses** an agent
  whose `cwd` is not a directory, so disk layout must precede registration

**(builder's call)** Model it as idempotent steps with a recorded state per step,
plus a reconcile command that can be run repeatedly until a name is whole. Do not
attempt rollback across systems; converge forward.

### Slack scopes

Checked against Slack's reference rather than guessed. The **method** is
`conversations.create` with `is_private: true`; the **bot scope** for a private
channel is **`groups:write`** (`channels:manage` is the public-channel
counterpart). `conversations:create` — named in the first draft — is a method, not
a scope.

`groups:write` lets an app create private channels and manage ones **it has been
added to**, which confirms the constraint: **a bot cannot invite people into a
private channel it is not already in.** So the sequence is create-then-join
before any invite, not create-then-invite. Resolving people to ids needs
`users:read`.

The required scope for `conversations.invite` is **not yet confirmed** — check its
reference page directly. None of the above are in the manifest today
(`app_mentions:read`, `chat:write`, `channels:history`, `groups:history`), so
hands-off creation is a manifest change plus an app re-install, not just code.
The roadmap already declines `users:read.email` for directory auto-discovery;
this is a narrower need.

**This may not be required at all** — see §15.2. If the owner creates the private
channel, invites the bot, and then runs provisioning, step 5 loses the manifest
change, the re-install and three scopes.

Also, per §4: **shared names belong in private channels.** In a public channel
anyone in the workspace can join, which combined with H1 is workspace-wide
access (R3).

### The owner CLI

The friction that will actually bite is not intentionality — it is being forced
into Slack. `local.py` already exists and its docstring says *"every other part
of Ricardi runs exactly as it does in production."* A thin CLI over it lets the
owner drive the shared name from a terminal while invitees watch in Slack, which
makes being intentional up front nearly costless.

Two things it needs: both transports live at once (today `local.py` replaces the
Slack transport), and a token mapped to a principal, since an HTTP request
carries no Slack user id and policy would have nobody to check.

---

## 12. Findings to fix

From the simulation. **H1 and H2 are done** (2026-10-28), with 14 tests in
`tests/test_access_scoping.py` and `tests/test_sdk_backend.py`; suite 341 passed,
20 skipped. The simulation re-run shows 18 as intended, 1 hole, 2 frictions.
The rest are open.

| # | Fix | Size |
| :- | :- | :- |
| **H1** | Read `Principal.known` in `Rule.matches` so no allow can match an unknown asker. The field is already computed and **nothing reads it.** This is the whole fix and it applies to every action. **The "every allow must name `users`" loader rule applies to `ask` and `list` rules only** — every *tool* allow in the live and example policies deliberately names no users, and under §7 that is correct, because per-person capability comes from the tier, not from the rule. Enforcing it on tool rules would break the shipped examples and the running deployment. | one condition |
| **H2** | Intersect every tool rule with **the name's explicit list of permitted roots** — not with `cwd`. `cwd` is one string and the `name 1:N repos/cwds` goal would otherwise be met by pointing it at a parent directory, which makes the intersection nearly as wide as `/workspace` and silently reverts this fix. So a name carries `roots = [...]` in config, separate from the single `cwd` the SDK takes, and the engine intersects against that list. | small, engine + config |
| **H3** | A `command_matches` deny for pushing. Mitigated today only by the first line (no push credentials), which the provisioning plan removes. | one rule |
| **H4** | Have the loader refuse `command_matches` on an **allow** rule, or require a single simple command before an allow can match. Sound as a deny, unsound as an allow. | small, loader |
| **queue** | `_relay` tells a queued person *"Passed it to n1. Still working"* — false; their turn has not started. Distinguish waiting-for-someone-else from working-on-yours. | wording |
| **timeouts** | `sdk.py:187` passes no `timeout`, so the gate runs on a 60s default Ricardi never chose. `policy.py` promises a timeout denies — that covers Ricardi's own timeout, not the CLI's hook timeout. Pass an explicit `timeout`, and **establish whether hook-timeout expiry fails open or closed.** Latent today (microsecond rules); live with the external PDP the roadmap plans. | unverified — needs a live session |

Also unresolved and carried forward: **R2**, a reader can exhaust the owner's
budget, since per-name budget is shared by every member. Read does not mean
harmless once money is involved.

---

## 13. Build order

Forced, bottom-up:

1. **H1 and H2.** Two small fixes that the rest would otherwise be built on top
   of. H1 matters more under 1:1, because channel membership is now the join to
   the session.
2. **The mutation layer.** SQLite grants in `state/`, append-only, fail-closed on
   corruption. First, because channel-to-name binding and tiers are both
   runtime-mutable state — building them on a JSON file is building something
   we have already agreed to throw away. **Includes a seed migration** (§15.4):
   absence of a grant row means no access, so the live deployment needs its
   current roster written in as rows, or the deploy that lands this is a flag day
   that locks everyone out.
3. **Owner, grants and tiers.** The `owner` node, plane-2 verbs as new `action`
   values, delegation bounded by the ceiling.
4. **Channel 1:1 name.** Including the directory that replaces the dropped
   selector, and retiring most of `ThreadBindings`.
5. **Provisioning.** The job, the reconcile command, the Slack scope change.
6. **Later, independently:** the hold; the owner CLI; `ricardi check --strict`;
   per-name `ClaudeAccount`.

---

## 14. Left to the builder, and open questions

**Builder's calls recorded above and in §15:** the posture report (§9);
converge-forward provisioning rather than cross-system rollback (§11); name
registration requires a restart (§7); the grant/ceiling composition and
`Request.tier` (§7); three person-tiers with `exec` as name scope (§8); H1 scoped
to `ask`/`list` and H2 keyed to an explicit root list (§12); plus §15.3–§15.6.

**Open — these change what gets built and only the owner can settle them.**
Re-ranked: the first three gate the build order rather than the deferred tail.

1. **Is the SDK's bash sandbox in scope?** (§15.1) It could turn §8's "not
   airtight" from a documented caveat into a real boundary, which would change
   both the first principle's practical reach and the posture report's
   assertions.
2. **Must Ricardi create the private channel and invite people, or may
   provisioning require the owner to do that by hand?** (§15.2) Hands-off costs a
   manifest change, an app re-install and three new scopes, and a bot cannot
   invite into a private channel it is not in. By-hand makes step 5 materially
   smaller. Requirement or convenience?
3. **Is the grant store ever shared between several Ricardi instances** — one per
   Slack workspace — or always one instance, one store? SQLite is comfortable for
   the second and the wrong tool for the first, and it is the second thing built.
4. **Does a name ever hold a git identity that can push?** `practices.md` says
   the right answer today is none at all — *"then 'do not push to main' is not a
   rule anyone has to enforce, it is a thing that cannot happen"* — and calls that
   more important than any rule you will write. The provisioning plan (create a
   repo, add collaborators) reverses that deliberate absence and is the one
   widening here that reaches off the box into other people's accounts.
5. **Does the first release need a per-person spend bound?** (R2) A reader can
   exhaust the owner's budget, since budget is per name and shared by every
   member. If this must ship, `limits.py` grows from in-memory per-person turn
   counting into persistent per-(person, name) cost accounting — real scope inside
   step 3, not step 6.
6. **Who may veto a held command, and what happens on timeout.** Already named in
   the roadmap. A veto that fails open is worse than no hold. Sits in step 6.
7. **Does re-keying a name cost it its memory?** Whether a session started under
   one Claude key resumes cleanly under another is unverified. Cheap to test, and
   it gates per-name accounts in step 6.

---

## 15. Resolutions from the builder check

A fresh engineer read this document against the code on 2026-10-28. Two real
contradictions, one unknown mechanism, and several gaps. The contradictions are
fixed in place above (§7's verb table and grant composition, §8's tier
enforcement, §9's gap 2, §11's Slack scopes, §12's H1 and H2). What follows is
the rest.

### 15.1 The SDK has a bash sandbox, and this design had not accounted for it

`ClaudeAgentOptions.sandbox_settings` exists in `claude_agent_sdk` 0.2.163
(`types.py:904-948`): filesystem sandboxing for bash, plus network isolation with
`allowedDomains`, `deniedDomains` and `allowUnixSockets`, plus `excludedCommands`
and `allowUnsandboxedCommands`.

That is an **L1-grade mechanism** — a real boundary, not a policy rule — and it
would apply *within* a shared session, which is precisely what §2 says is
impossible. If usable, the read tier could have a genuine filesystem boundary
instead of a documented caveat, and §9's second gap narrows considerably.

Three things to establish before relying on it:

- `enabled` is **macOS/Linux only** and defaults to `False`. The box is Ubuntu,
  so this is fine, but local development on Windows would not exercise it.
- `enableWeakerNestedSandbox` exists for "unprivileged Docker environments",
  which is exactly this deployment. **Weaker** deserves reading carefully before
  any safety claim rests on it.
- **`autoAllowBashIfSandboxed` defaults to `True`.** Enabling the sandbox naively
  would auto-approve bash *before* the `PreToolUse` gate — the same
  approve-before-the-hook hazard `sdk.py` already documents for `allowed_tools`,
  and it would bypass the enforcement point everything in this design rests on.
  **Set it to `False` explicitly, and add a test that the gate still fires.**

This does not change the first principle in §2 — the default posture stays
accident-prevention — but it may change how high the dial goes.

### 15.2 Channel creation may not be Ricardi's job

See §11. Needs the owner's answer; it is question 2 in §14.

### 15.3 A `scope` narrowing bites at the gate immediately, and at `cwd` on next connect

`cwd` is passed into `ClaudeAgentOptions` at construction and `_connect` caches
the client for the life of the process. So a narrowing written to the grant store
is enforced at the policy gate on the very next tool call, but the session's
actual working directory does not change until a reconnect.

**(builder's call)** That asymmetry is safe — the gate is the enforcement point,
so a stale `cwd` is a convenience issue (relative paths), not a permission one.
But `scope` must *say so* when it is used, or an owner will narrow a scope and be
wrong about what just happened. Do not build drop-and-reconnect: it would
interact with `_Live.lock` and with the hold for no safety gain.

### 15.4 Absence of a grant row is not the same as corruption

§6 specifies fail-closed on corruption and says nothing about absence.

**(builder's call)** No row means no access — anything else leaves H1 open. The
consequence is that landing the grant layer is otherwise a flag day for the live
deployment, so step 2 includes a **seed migration** that writes the current
`policy.toml` roster in as rows at the tier their existing rules imply.

### 15.5 Bootstrap: the first owner comes from the ceiling

Who makes the first grant, when nobody has a `grant` grant yet? §6 already puts
"the list of people who may grant at all" in the reviewed file, so the answer is
consistent with what is there: **admin writes the owner into `policy.toml` when
the name is provisioned.** The DB never bootstraps itself, and there is no
self-service path to becoming an owner.

### 15.6 `reset` forgets the id; deleting a transcript is a different verb

`SessionIds.forget` drops the id and leaves the transcript under
`/config/claude`. §7's phrase "wipe its memory" read like deletion.

**(builder's call)** `reset` forgets the **id only** — the agent starts a fresh
conversation, and the old transcript remains on disk and in backups. That is the
reversible default. Deleting a transcript is a separate, explicitly named
destructive verb, not yet designed, and R6's warning stands: a name re-used after
a `reset` still has a readable history on disk, so the privacy promise `reset`
makes is "the agent has forgotten", not "the record is gone". Say that in the
user-facing help, because the two are easy to confuse and only one is true.

### 15.7 Line references

The builder found `sdk.py:187/194` are off by one (186/193) and otherwise point
where this document says. Cited line numbers throughout are indicative; verify
before editing.

---

## Evidence

- [`scripts/access_simulation.py`](../../../scripts/access_simulation.py) — 21
  scenarios run through the real `RulesPolicy`. Run:
  `.venv/Scripts/python.exe scripts/access_simulation.py`. **18 as intended, 1
  hole, 2 frictions** after the H1 and H2 fixes, from 15/4/2 before them. The 18
  matter as much as the 3: they confirm that an undesigned verb denies, an
  unbounded `Grep` denies, a cross-name *write* denies and a transcript read
  denies.
- [`docs/access-simulation.md`](../../access-simulation.md) — findings H1–H5,
  F1–F2, R1–R7, the layer table, the paranoia-dial analysis, and the SDK
  hook-timeout finding.
- `tests/test_access_scoping.py` — H1 and H2 as regression tests, so a future
  widening fails CI rather than waiting to be rediscovered by a simulation.
