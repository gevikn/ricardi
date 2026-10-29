# Ricardi's own interfaces

**Status:** design, not built. **Sequenced after**
[`2026-10-28-shared-sessions-design.md`](2026-10-28-shared-sessions-design.md)
steps 1–4 — tiers and grants land first, so the write path here is built on real
tiers rather than retrofitted onto today's `policy.toml` rules. That document's
transport assumptions are superseded; everything it says about policy, tiers,
grants and provisioning stands.

Revised after a builder check, which found that the first version
under-specified almost everything where the event model meets existing code.
§16 records what changed.

---

## 1. Intent

Ricardi exists because of one moment: you are in a Claude Code session and you
want one other person in it with you. Today you are the wire between them. You
read what came back, say it out loud, listen, translate, type, wait, read it
again. They cannot see it and they cannot touch it.

Slack was the right first answer for a reason weaker than it looked: *that is
where teams already are*. The observation that undoes it is the owner's own —
**these same people happily switch to Claude Code or Claude Desktop to do their
Claude work.** They context-switch constantly. The requirement was never Slack.
It was **not a terminal**.

### Why Slack is the wrong shape

Three failures, none configurable:

1. **A session is not a chat log.** A turn is tool calls, long output and diffs.
   Slack renders a flat stream with no collapsing, no diff view, no "show me the
   file it touched". A 21-line test failure is 21 lines of monospace everyone
   scrolls past, forever, in every thread.
2. **Refusals are invisible to everyone but the asker.** For the teaching case
   in [`pair-programming.md`](../../pair-programming.md) this is backwards:
   watching someone get refused and reading *why* is the lesson.
3. **Watching requires membership.** Observing means joining a private channel,
   which grants posting rights and the whole history. The read-only participant
   cannot be expressed.

A spike on 2026-10-29 confirmed all three invert in a rendered page: output
collapsed to one line, the diff read as a diff, the refusal was visible to the
watcher.

---

## 2. Where the events come from — the correction that shaped this document

The spike added one file and modified nothing else. **That is true and it is
misleading as a basis for planning**, and the first version of this document
built a build order on it.

The spike renders `Transport.send(channel, thread, text)` — strings Ricardi
*already said*. Everything §6 needs lives **upstream of the transport seam**:

- `backends/sdk.py::_one_turn` keeps only `TextBlock` and discards
  `ToolUseBlock` and `ToolResultBlock` entirely.
- `assemble()` truncates at `MAX_ANSWER = 3500`, deliberately, because "a wall
  of text in a channel is not an answer".
- `AgentBackend.ask(...) -> str` has no streaming surface; `app.py::_relay`
  awaits one string.
- A denied tool call reaches `log.info` and the model. No transport ever sees
  it.
- `app.py::_refuse` receives `decision.reason` as a bare string; the `Decision`
  itself is discarded in `_permit`, so rule and tier cannot reach a transport.

**So "Slack stops being the ceiling" is false while the real ceiling sits in the
backend.** This is not a transport feature. It widens `backends/base.py`,
replaces `Transport.send(text)` with `emit(event)`, and therefore rewrites
`slack.py` and `local.py` in step 1 rather than step 8. It carries the live-run
obligation CLAUDE.md places on any `sdk.py` change.

**Constraint from CLAUDE.md:** a change that makes either seam aware of the
other is "going the wrong way". The event vocabulary is therefore defined in
neither — it is its own module both seams depend on.

---

## 3. First principle: the event model is the product

**A TUI is not a second transport.** Both interfaces consume the same stream;
the browser renders HTML, the terminal renders to a screen. The artefact that
deserves care is neither UI but **the event model they share**. Get it wrong and
both clients are rewritten — the anti-goal below.

---

## 4. Goals, non-goals, anti-goals

### Goals, ranked

1. An invited person can **see the session properly** — collapsed output, diffs
   as diffs, refusals visible.
2. They can **act in it**, bounded by their tier, when the owner is absent.
3. **Onboarding is a link**, not a workspace invitation plus a channel
   invitation plus a roster edit.
4. **Watching costs nothing** — read-only, no ability gained.
5. The owner reaches always-on sessions **from a terminal** without SSH.
6. Notifications reach someone who is not looking.

### Non-goals

- A team chat: no channel list, no DMs, no cross-session search, no presence.
- Replacing Claude Code. The owner's primary session stays where it is.
- Mobile-first. Mobile reads and replies briefly; it does not drive.
- **Email notification** (decided 2026-10-29, see §8).
- An adversary boundary. The accident-not-adversary principle is unchanged.

### Anti-goals — failure even if it works

- **Rebuilding Slack.** Channels, reactions and a sidebar mean it failed.
- **An interface only the owner can install.** If the invitee must run
  something, the conduit problem is relocated, not solved.
- **Owning auth.** See §6.
- **Two divergent clients**, which means the event model was wrong.
- **An archive of other people's conversations** nobody decided to keep (§7).

---

## 5. Architecture

```
                              ┌─ browser page  (HTML + SSE)
Claude Code ─► backends/ ─► app.py ─► transports/http.py ─┼─ TUI client    (same stream)
                 emit(event)    emit(event)               └─ slack.py      (lossy renderer)
```

One server-side transport, N clients. Both seams widen to carry events; the
vocabulary lives in a module neither seam owns.

### Addressing, and the exact-match trap

`Rule.channels` is matched by **exact `in` test**, not a glob — unlike `users`
and `agents`, which use `_any_glob`. The live `policy.toml` names a literal
Slack channel id. A web request that invents a `channel_id` per connection
therefore matches no rule and **denies everything, including the owner's**.

**Decided:** an HTTP session is addressed **by name**, one stream and one URL
per name, consistent with the 1:1 channel↔name model. A web room is a real,
stable, rule-nameable identifier — not minted per request.

**Decided:** the stream is **partitioned per name**. A single global transcript
on a box serving two names is a cross-project leak, which is the precise thing
1:1 exists to prevent. The spike has exactly this bug.

---

## 6. Identity and invitation

**Cloudflare Access authenticates. Ricardi never does.** Access identifies by
email, which is what the roster already keys on — `Principal.known` is
`bool(email)`. Identity is a new source for an existing field, not new work.

**Decided: a second hostname and a second Access application.** The existing
one fronts `ssh://localhost:22`. Adding an invitee to *that* application would
grant them **SSH to the box**. The web application is separate, with its own
policy, and provisioning records which.

**Decided: verify `Cf-Access-Jwt-Assertion`** — signature against the team's
JWKS, plus `aud` and issuer. The `Cf-Access-Authenticated-User-Email` header is
trusted only as a convenience *after* the JWT validates. Trusting the bare
header is an authentication bypass of the entire roster the moment anything but
cloudflared can reach the socket.

**Decided: cloudflared joins the compose network** rather than the container
publishing a port. `compose.yaml` publishes nothing today — "most of the reason
this deployment is simple" — and `local.py`'s own `--host` help already records
that loopback is unreachable from a port mapping.

---

## 7. The event model

The spike emitted `{seq, kind, text}`: enough for a demo, nowhere near enough.

| Concern | Why text cannot carry it |
|---|---|
| Who spoke | a watcher must distinguish owner, invitee, agent |
| Turn boundaries | collapsing needs to know where a turn ends |
| Tool call | name, arguments, duration, outcome — collapsible as a unit |
| Diff | path and hunks, rendered as a diff |
| Command output | large, collapsible, irrelevant once green |
| Refusal | rule, reason, tier |
| Streaming | a turn arrives in pieces |
| Frames | a screencast must ride the same stream later (§11) |

### Redaction is part of the model, not of a renderer

CLAUDE.md: **"Do not log raw tool input. A log that records every argument
eventually records a password."** `audit.py` summarises at 300 characters for
exactly this reason, and a watcher is governed by no tier (§10).

**Decided:** tool-call events carry `summarise()`-grade input. Raw arguments
never enter the stream. A `Write` input is an entire file; the stream is not the
place for it.

### Refusals, and who sees which part

`policy.py` deliberately tells humans what to do without naming the rule, and
keeps the rule in the audit log. The teaching case wants the rule.

**Decided:** the event carries the full `Decision` — rule, reason, tier — and
the **renderer** decides. The refused person sees the reason, as today. The
owner's and watcher's views may show the rule. This requires `_permit` to stop
discarding the `Decision` and `_gate` to emit on deny, which today tells nobody
but the model.

### An answerable event, reserved now

The hold (L3 in the sibling design) announces, waits, and proceeds only if
nobody objects. The browser is its obvious home. That needs an event that is
**pending**, keyed to a `tool_use_id`, resolved within the `HookMatcher`
timeout — a request *for* a decision inside a model otherwise append-only and
one-way.

**Decided:** reserve the answerable event kind and a correlation id now.
Retrofitting it later is precisely the both-clients-rewritten failure. The veto
is handled in `app.py` as its own command and never relayed to the backend, or
it deadlocks on `live.lock`.

### Persistence and retention

**Decided:** events persist in `state/` — the box's own disk, beside
`audit.jsonl` — **bounded to the current session and capped**. Replay is from
session start, not process start, and survives a restart.

The reasoning, because it reverses a tempting default: `audit.jsonl` is kept
forever *and is summarised*. A verbatim, permanent transcript would silently
undo that care for everything except the part that was careful. Bounded also
makes `reset` ("forget the session, keep the name") mean something.

Checked on 2026-10-29: the restic set covers the owner's laptop
(`~/.claude`, `~/projects`, `~/.ssh`), **not** `/opt/ricardi`. Transcripts on
the box reach no backup today. If that changes, this section changes with it.

---

## 8. Notification

**Decided: Web Push only. No email.**

Email would mean SMTP credentials or a provider key living in the container,
and the deployment's simplicity rests on holding as little as possible.

**Known limitation, recorded rather than papered over:** Web Push requires the
browser to be running, and on iOS it works only if the page is installed to the
home screen as a PWA. An iOS user who does not install it **will miss
notifications**. Accepted.

**Left to the builder:** what is worth interrupting someone for. A finished
turn, probably. Every streamed chunk, certainly not. A refusal addressed to
them, probably.

---

## 9. The two clients

### Browser

The primary surface, and what the non-terminal half of the pair uses. The spike
establishes the shape: a transcript rather than a chat, output collapsed behind
a summary, diffs coloured, refusals inline and visible, theme following the
reader.

### TUI

For the owner, terminal-native, reaching an always-on session without SSH.

**Decided: it authenticates with `cloudflared access login`** — browser-based
SSO that caches a short-lived token, the same path the SSH access already uses.
Explicitly **not** an Access service token, which is the long-lived bearer token
the sibling design rejected in §11.

Built second, because its value is proving the model generalises. If the TUI is
hard, the event model is wrong — cheaper to learn second than first.

**Left to the builder:** language, framework, and whether it ships inside the
Python package or beside it.

---

## 10. Watching

A watcher reads everything and decides nothing.

**Decided: watching is an audited decision.** `Principal.known` governs
*asking*; a watcher never reaches that decision point, so without this there is
no audit row for who watched — a documented exception to the sibling design's
central rule ("every decision, including the refusals nobody sees, in an
append-only log"). So `watch` is a plane-2 action, a grant-store row, and an
enforcement call in `transports/http.py`.

**This answers the first version's open question** in favour of Access over
unauthenticated capability URLs. A capability URL has no revocation story —
rotating it evicts every watcher at once — and no identity in the log.

**Unchanged and load-bearing:** a watcher sees everything the session prints.
No tier governs that.

---

## 11. Browser projection — deferred, ladder recorded

**An iframe cannot do this.** It loads the URL fresh in the *viewer's* browser
with the viewer's cookies or none — a logged-out page, no cursor, no actions —
and most real sites refuse to frame at all via `X-Frame-Options` or
`frame-ancestors`. Recorded because the instinct is natural and the mechanism
cannot carry it.

What can, cheapest first:

1. **Screenshots on the existing stream**, as another event kind.
2. **CDP screencast** — `Page.startScreencast`, 10–20fps one-way video.
3. **Browser-in-the-container over VNC/WebRTC** — the only route to shared
   *control*, and real infrastructure.

**Decided:** out of scope for the first version; the event model must not
foreclose (1) or (2).

---

## 12. The gap a browser opens, which blocks §11

**A browser defeats the tier model.**

Every control built so far bounds the **filesystem** and the **shell**: path
roots, `path_under`, `command_matches`, read and write tiers. A browser is
outside all of it. An agent with a browser can log in, send mail, post, approve
and pay, and the policy engine sees none of it — none of it is a file write or a
command string.

A `read` tier would mean the person cannot edit a file while their words drive a
browser that can empty a shopping cart.

Larger than H1 or H2 were. It belongs in
[`access-simulation.md`](../../access-simulation.md) as **H5**.

**Decided:** no browser capability ships to a non-owner tier until the tier
model has a dimension for it. §11 is **gated** on this, not merely sequenced
after it.

---

## 13. What happens to Slack

Demoted, not removed. `slack.py` becomes a renderer of the event model and
degrades to text what it cannot show.

**Consequence for the public repository, stated so it is deliberate:** the
README opens with "Ricardi puts the session in a Slack channel", and
[`comparison.md`](../../comparison.md) argues Slack is where teammates already
are. Both are *true today* and describe a direction being left.

**Decided:** leave both unchanged until the interfaces exist. Replacing an
accurate description of what ships with a description of something unbuilt is
worse than slightly stale. They are revisited with the first release carrying a
browser client — which is also when the announcement, deferred for exactly this
reason, becomes writable.

---

## 14. Build order

**Prerequisite:** shared-sessions design steps 1–4 — H1/H2 fixes, the grant
store, owner/grants/tiers, channel 1:1 name. `Request.tier` is a step-1 data
model change *there*, not here.

Then:

1. **The event vocabulary**, in its own module depended on by both seams.
   Nothing else starts first.
2. **Widen `backends/base.py` and `sdk.py`** to emit tool calls, outcomes,
   diffs and streamed text. Live-run obligation applies.
3. **Replace `Transport.send(text)` with `emit(event)`**, rewriting `slack.py`
   and `local.py` as renderers.
4. **`transports/http.py`** — per-name streams, bounded replay, input endpoint.
5. **The browser client, read-only.** Watch mode is the smallest useful thing.
6. **Access in front of it** — second hostname, second application, JWT
   verified; `watch` as an audited action.
7. **The write path**, tier-checked against the real tiers from the
   prerequisite.
8. **Web Push.**
9. **The TUI**, as proof the model generalises.

Browser projection (§11) does not enter this list until §12 is answered.

---

## 15. Calls made in writing this, not agreed in conversation

- Access authenticates rather than Ricardi, on a **second** application (§6).
- The Access JWT is verified; the email header alone is never trusted (§6).
- cloudflared joins the compose network; no published port (§6).
- Streams are partitioned per name and addressed by name (§5).
- Tool events carry summarised input only; raw arguments never (§7).
- The event carries the whole `Decision`; the renderer decides who sees the
  rule (§7).
- An answerable event kind and correlation id are reserved now (§7).
- Transcripts are bounded to the current session (§7).
- Watching is an audited `watch` decision (§10).
- The TUI uses `cloudflared access login`, never a service token (§9).
- The browser client is built before the TUI (§9).
- Browser projection is **gated** on the tier gap, not sequenced after it (§12).
- README and `comparison.md` are left stale rather than rewritten now (§13).

---

## 16. Resolutions from the builder check

The check raised ten questions; three went to the owner, the rest are resolved
above. What materially changed:

1. **The ceiling is in the backend, not the transport** (§2). The first version
   planned a transport feature. It is a change to both seams, and `slack.py`
   moved from step 8 to step 3.
2. **`Rule.channels` is an exact match** (§5). A per-connection `channel_id`
   would have denied every request including the owner's, on day one.
3. **Sequencing** — owner's answer: shared-sessions first, so the write path is
   built on real tiers rather than retrofitted (§14).
4. **The Access application** (§6). Inviting a watcher into the existing one
   would have granted SSH to the box. The first version said "an Access policy
   entry" and would have produced exactly that.
5. **Watching was unaudited** (§10), a silent exception to the other design's
   central rule.
6. **Redaction** (§7). The first version asked for tool-call arguments, which
   CLAUDE.md forbids in the strongest terms it uses anywhere.
7. **The hold needs an answerable event** (§7), unmentioned in the first
   version and expensive to retrofit.
8. **Transcript durability** (§7) was neither decided nor delegated. Owner's
   answer: bounded.
9. **Email** — owner's answer: no (§8).
10. **TUI authentication** (§9) was not mentioned; it nearly reintroduced the
    long-lived token both designs reject.

---

## 17. Open questions

1. **Does the first browser *release* write, or only watch?** The build order
   watches first; whether v1 ships writing is separate.
2. **What is a turn, exactly?** (§7) The model's hardest question and the most
   expensive to get wrong.
3. **Does `watch` as a plane-2 action belong in this design or the sibling's
   grant store?** It is a grant, and the grant store is a prerequisite.
