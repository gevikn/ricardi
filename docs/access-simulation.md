# Access simulation: the multi-name design

Run it: `.venv/Scripts/python.exe scripts/access_simulation.py`
(or `python scripts/access_simulation.py` in an env with the package on the path)

## What this is

21 scenarios run as real `Request` objects through the real `RulesPolicy`. Not a
sketch — the policy engine on disk makes every decision, and each row carries
what the *design* intends. A row where the engine disagrees is either a **hole**
(allowed, shouldn't be) or a **friction** (denied, legitimately needed).

The modelled policy is deliberately not a strawman: it is tonight's live
`policy.toml` extended to a second name the obvious way. That is what a careful
person writes first, which is the thing worth testing.

Personas: `owner`, `writer` (invited, write), `reader` (invited, read),
`lurker` (**added to the Slack channel, never granted anything by Ricardi**),
`outsider`. Two names, `n1` and `n2`, in two channels.

### Status after the H1 and H2 fixes (2026-10-28)

**18 as intended, 1 hole, 2 frictions**, up from 15/4/2. Both H1 rows now refuse
with `no roster entry`; all three H2 rows refuse with `outside the agent's
roots`. `n1 reads /etc/shadow` also improved — it was falling through to
`(default deny)` and now names the rule that stopped it, which is a better audit
line for the same refusal.

What remains:

- **H3 (pushing) is a defect in the *modelled* policy, not in what ships.**
  `policy.example.toml:158` already carries a `*git push*` deny; the simulated
  policy here omitted it, which is what the row reports. The finding stands only
  as "a hand-written policy can forget this", and the shipped example does not.
- **The two F1/F2 rows now model an approach the design has rejected.** They
  exercise a forced `-ro` on a read-tier person, which §8 of the design replaced
  with a tier expressed as a policy rule. The row that matters is
  `PROPOSED FIX: reader runs pytest`, which passes. Keep the two as a record of
  why the forced-flag design was abandoned, not as live defects.
- H4 and H5 are unchanged: they are facts about what a command allowlist can
  bound, not bugs to close.

The original result, for reference, was **15 as intended, 4 holes, 2
frictions.** The 15 matter as much as the 6 —
they confirm the fail-closed properties hold: an undesigned verb denies, an
unbounded `Grep` denies, a cross-name *write* denies, a transcript read denies, a
wrong-channel ask denies. A simulation that only finds holes is not trustworthy.

**Model note.** The channel-to-name relation has since been settled at **1:1**
(see `goals.md`). The matrix is unaffected — two names in two channels is exactly
that — but two readings change:

- `owner asks n2 from n1's channel` denying is now correct **by design** rather
  than as a side effect of how the rule happened to be written.
- **H1 matters more, not less.** Under 1:1, channel membership is the join to the
  session, so an unscoped allow rule turns "whoever can add a Slack member" into
  "whoever can confer capability".

## Holes — executed, reproducible

### H1. Channel membership silently confers capability

`ask-n1` names `channels` but not `users`. Unnamed dimensions match all, so
**every member of that channel is permitted** — including `lurker`, who is not
even in the roster. Decided by `ask-n1`; same for `list-catalogue`, which
discloses the repo catalogue.

This is the most serious finding, because it directly contradicts the product.
The whole point is "I invite a person or two." Here, *anyone who can add a member
to the Slack channel confers capability without Ricardi being told* — and in a
**public** channel, anyone in the workspace can add themselves.

Three fixes, cheapest first:

1. **Require `principal.known` for any allow.** The field already exists, is
   already computed by `RulesPolicy.principal`, and **nothing currently reads
   it.** One condition in `Rule.matches` closes the lurker case entirely.
2. Have the loader refuse an allow rule that names no `users` — same precedent as
   refusing `allowed_tools` in `agents.toml`, where a setting that looks like it
   grants something is rejected rather than ignored.
3. The designed answer: grants in the DB keyed to `(person, name)`, so channel
   membership stops conferring anything at all.

Also a practice, not a code fix: **shared names belong in private channels.**

### H2. A name can read every other name's tree (the path half of issue #23)

`read-workspace` has `path_under = "/workspace"` and names no `agents`, so `n1`
reads `/workspace/project2/secrets.py`. Latent today because only one name
exists; live the moment there are two.

This breaks "scope lives on the name" at the first opportunity, and with N names
there are N-squared pairs to remember in a hand-written file. The fix should
therefore not be a rule anyone has to write: **the engine should intersect every
tool rule with the asking name's own `cwd` implicitly**, so a forgotten
`path_under` cannot widen anything. A `path_under = "agent_cwd"` sentinel is the
smaller change; the implicit intersection is the safer one.

### H3. Pushing is permitted by policy

`n1-shell` allows any command, including `git push origin main`. Mitigated
*today* by the first line of defence — no push credentials in the checkout,
exactly as `practices.md` prescribes, which is why this is a hole in policy and
not in the deployment. But the provisioning plan adds a git identity, and the
moment it does, this is live. A `command_matches` deny belongs there regardless:
policy is the second line, and the second line should exist.

### H4. A command allowlist is defeated by a semicolon — and so is the fix for F1/F2

Worth reading closely, because it is the hole in my own proposed fix.

`command_matches` globs the flattened command string. As a **deny** ("does this
look bad") that is sound, and it is how tonight's policy uses it. As an **allow**
("is this safe") it is unsound, because a prefix match says nothing about what
follows. Both proven:

| Command | Rule | Result |
| :- | :- | :- |
| `pytest -q` then a chained `rm -rf` on the project | allow `pytest*` | **allowed** |
| `ls` with a command substitution piping curl into sh | allow `ls*` | **allowed** |

A metacharacter deny scoped to the tier closes both while `pytest -q` still
passes (verified — those two rows flip to `reader-no-chaining`). But it has to be
remembered for every tier, which is the same fragility as H2. The sound fix is
structural: either the loader refuses `command_matches` on an allow rule, or the
engine parses the command and requires a single simple command before an allow
can match.

**But see H5 — that structural fix is also insufficient.**

### H5. An allowlist of command *names* is not a boundary at all — demonstrated

A read-only operation through a shell often requires *writing* something: a
script, a temp file, an inline program. That indirect write is model-generated
from a transcript a reader can influence, so it is prompt-injection surface.

It is worse than that, because the commands that look safest already execute
code the repository controls. Demonstrated in a scratch directory:

```
conftest.py  ->  pathlib.Path("INDIRECT_WRITE.txt").write_text("conftest.py ran")
$ pytest -q
1 passed in 0.02s
$ ls
INDIRECT_WRITE.txt   conftest.py   test_x.py
```

`pytest -q` wrote a file. No semicolon, no substitution, no metacharacter — the
command string is exactly what an allowlist would permit. `pytest` collects and
executes `conftest.py`, so **allowing `pytest` is allowing arbitrary code
execution and arbitrary writes.** The same holds for `git log` (which honours
`core.pager` and aliases from `.git/config`), and for `make`, `npm test`,
`cargo test` and every other runner that executes project configuration.

So the metacharacter deny verified under H4 is cosmetic against this. A command
allowlist bounds the *string*, never the *behaviour*.

## Frictions — executed, reproducible

### F1 / F2. The read tier cannot run tests, or `git log`

I proposed mapping GitHub `read` to a forced `-ro`. **That proposal is wrong**,
and the repo already said so before I made it.

`-ro` permits only `Read`, `Grep`, `Glob`, `NotebookRead`. So a reader cannot run
`pytest`, cannot run `git log`, cannot run `ls`. And `practices.md` states
plainly: *"Summarise what this repo does sounds read-only and needs ls, cat and
git log."* It then draws the conclusion I missed — *"Permission belongs in
policy, where it can depend on who is asking"* — which is to say **a capability
tier must be a policy rule, not a forced flag.**

That correction stands on its own. The catch is that expressing the tier as a
rule needs a command allowlist, which is H4. So the honest position is:

> With the engine as it is today, there is no *sound* way to give a reader shell
> access. The choice is to accept the friction (a reader gets no shell at all), or
> to add structural command parsing to the engine.

This is the decision the simulation exists to surface, and it is not cosmetic:
"can I run the tests" is the single most common legitimate read-only request, and
a reader who cannot do it will ask to be made a writer. That is anti-goal 5.

## Layers of defence for shell access

Given H5, no single mechanism works. Ordered strongest to weakest:

| Layer | Mechanism | Holds against |
| :- | :- | :- |
| **L0 — don't need it** | `Read`, `Grep`, `Glob` already cover most listing and finding (ripgrep, glob). The residual gap is narrow: `git log` and test output. | everything, by not running |
| **L1 — the filesystem** | a read-only mount. An indirect write becomes *impossible*, not forbidden. `compose.yaml` already says to mount a repo you would mind losing read-only and let the filesystem say no. | an adversary |
| **L2 — policy** | deny on argv *shape*, not string prefix: metacharacters, interpreters with inline code (`-c`, `-e`), heredocs. Porous by construction — see H5. | mistakes and known-bad shapes |
| **L3 — the hold** | announce, wait, proceed only if nobody objects. The only layer that addresses prompt injection, because no static rule separates a legitimate `python -c` from an injected one, while a human reading it often can. Roadmap, "Possible". | injection, when someone is watching |
| **L4 — containment** | budget and turn ceilings, no push credentials, append-only audit, a checkout you can afford to lose. | assumes the above failed |

### The structural limit

**L1 cannot be applied per person within a shared session.** One session is one
process with one filesystem view, so a reader and a writer on the same name see
the same mount. Per-request filesystem permissions do not exist.

So a read tier inside a shared session is necessarily an **L2** boundary — the
porous one. Which forces a question that should be answered on purpose rather
than by accident:

> Is the reader tier a **safety** boundary that must hold against someone
> adversarial, or a **courtesy** boundary that prevents accidents among
> colleagues who were deliberately invited?

- **Courtesy** — then give readers shell, say plainly that it is not airtight,
  and lean on L1 (a checkout you can afford to lose), L3 and L4. F1 and F2 stop
  being problems. For "invite a person or two", this is probably the honest
  answer.
- **Safety** — then the only sound construction is a **separate name over a
  read-only mount**, with its own session. That costs the shared transcript,
  which is the product. The trade is real and unavoidable.

## Can this be dialled to paranoid?

The default is a courtesy boundary. An operator who needs a higher bar should be
able to write policy for it. Mostly they can — these already work with no code
change:

- Mount a name's tree read-only. The filesystem then refuses every indirect
  write, whatever the transcript says. `compose.yaml` already prescribes this.
- Give a name no shell rule at all. Deny-by-default does the rest.
- Deny argv shapes with `command_matches` — sound as a deny, per H4.
- Separate reader and writer into two names over two mounts of the same repo
  (one `:ro`), which is the only *filesystem-enforced* read tier. Expressible
  today; it costs the shared transcript.
- Per-name `max_budget_usd` and `max_turns`.
- Replace the engine entirely: `PolicyPoint` is a `Protocol` so an external PDP
  can take over. The HTTP shape exists; a worked example against a real OPA does
  not.

Four things do stand in the way:

**1. The hold (L3) is not built, and cannot be configured at any setting.** It is
the only layer that addresses prompt injection, and it is the one a paranoid
operator would want most. Currently roadmap "Possible".

**2. There is no OS-level isolation between names.** One container, one uid
(1000, per the Dockerfile). Two names on a box are separated by `path_under` and
nothing else — no separate user, no separate container, no separate mount
namespace. Paranoid separation tops out at a path comparison, and H2 shows how
easily that is got wrong.

**3. "The asker must be known" is not expressible as a rule.** `Principal.known`
is computed and never read. An operator can avoid the H1 lurker hole only by
never once forgetting `users` on an allow rule — they cannot *require* it.

**4. Budget ceilings are enforced in-process.** A paranoid operator wants the cap
outside Ricardi, in the Anthropic console, which needs the per-name
`ClaudeAccount` edge that does not exist yet.

### The real gap: a dial with no readout

Every item above is *"you can write it correctly"* rather than *"it cannot be
wrong"* — which is the exact inverse of the product's own principle, prefer
impossible over forbidden. The paranoid setting is **achievable but not
verifiable**.

The cheap fix is a posture report — `ricardi check --strict` over the loaded
config, asserting properties rather than describing rules:

- every allow rule names `users`
- every tool allow is bounded by the asking name's own `cwd`
- no allow rule uses `command_matches`
- shell is denied, or denied by argv shape
- each name's mount is read-only
- each name has a budget and a turn ceiling
- every rule is named

That turns paranoia from a practice into a state you can see, which is the
transparency goal applied to the configuration itself. It reads config that
already exists, so it is cheap, and it is the natural home for the H1 and H2
regressions once they are fixed.

## The hold is a later addition — and there are two timeouts, not one

Checked against the installed SDK (`claude_agent_sdk` 0.2.163) rather than
guessed. `types.py:605-619`:

```python
@dataclass
class HookMatcher:
    matcher: str | None = None
    hooks: list[HookCallback] = field(default_factory=list)
    # Timeout in seconds for all hooks in this matcher (default: 60)
    timeout: float | None = None
```

So the hold needs **no architectural change**: the gate is already `async`,
already receives `tool_use_id` so a pending hold can be keyed to one call,
already announces through `caller.notice`, and already denies with a reason the
model reads — so a veto is feedback, not a crash. The hold window is
`HookMatcher(hooks=[...], timeout=<window + margin>)`. There is a first-class
knob for exactly this.

### The finding that matters more

`sdk.py:187` is `HookMatcher(hooks=[self._gate(live)])` — **no timeout passed,
so the gate runs on the 60-second default, which Ricardi never chose.**

`policy.py` promises that *"an error, a timeout, a missing attribute and an
unmatched request all deny."* That promise covers Ricardi's own timeout handling.
It says nothing about what the `claude` CLI does when the **hook itself** exceeds
its 60 seconds — allow or deny. Ricardi controls one of the two timeouts and has
never established the behaviour of the other.

Latent today, because the in-process rules engine answers in microseconds. It
goes live with the thing the roadmap already plans — *"An external PDP in
practice ... with timeouts and fail-closed proven rather than asserted"* — since
an HTTP policy call that hangs is precisely the 60-second case.

**Still unverified:** whether hook-timeout expiry fails open or closed. The
answer cannot be read from the Python package (`_bundled/` is empty, so the CLI
enforces it), and determining it needs a live session, i.e. a credential. Until
it is established, pass an explicit `timeout` on the matcher and treat
fail-closed as unproven rather than assumed.

## Reasoned, not executed

These need parts that do not exist yet. Listed because they are the risks the
*product* creates, not the ones the current code has.

**R1. Cross-tier transcript injection — the new attack surface.** A reader shares
the session's memory with a writer. The reader can put instructions into the
transcript; the agent may act on them when the *writer* next asks. The reader's
tools are gated; the writer's are not. So a reader's capability becomes a
writer's capability, one turn later. There is no clean mitigation — but note that
the roadmap's most-requested unbuilt feature, *"a real hold, not just an
announcement"*, is exactly it: announce-before-run already means the writer sees
the command, and a hold turns seeing into vetoing. **The product's core new risk
and the roadmap's top request are the same item.**

**R2. A reader can exhaust the owner's budget.** Per-name budget is shared by
every member. Read does not mean harmless once money is involved.

**R3. Public channels make H1 workspace-wide.** Anyone in a Slack workspace can
join a public channel.

**R4. Resolve GitHub permission at invite, not per request.** My earlier TTL-cache
design was the wrong shape: it buys revocation lag for no benefit. Resolve the
GitHub permission **once, when the invite is made**, write the resulting grant to
the DB, and re-check only on demand. No cache, no lag, and the grant is auditable
— which the live lookup never was.

**R5. Provisioning half-states.** The sequence spans mint key, create or clone
repo, lay out disk, register name, create channel, invite people. Specific bad
states: a name registered with no repo; a channel of invitees whose name does not
resolve; a key minted and attached to nothing; a repo created with collaborators
not yet added. Needs idempotent steps and a reconcile command, not a function.

**R6. Detach does not forget.** Detaching a name from a channel leaves the
transcript intact, so re-attaching later re-exposes it. `reset` (forget the
session, keep the name) and `destroy` (remove the name) must be separate verbs
with separate grants.

**R7. An owner could widen their own name.** If `scope` is delegable to owners,
an owner can widen `cwd` toward `/workspace` — which contains every project,
since the Docker mount is the only hard ceiling. A name's path ceiling must come
from admin and be narrowable but never widenable by its owner.

## How this becomes tests

Every executed row is already a test case: same `Request`, same engine, an
assertion instead of a printed verdict. The matrix should move into
`tests/test_access_matrix.py` as the design lands, so a future widening that
reopens H1 or H2 fails CI rather than being rediscovered by a simulation.
