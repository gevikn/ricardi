# Roadmap

What is planned, what is deliberately not, and the constraints behind each.

## Now

Slack, over Socket Mode. Agents owned by Ricardi through the Claude Agent SDK.
Policy at two enforcement points with an append-only audit log.

## Other chat platforms

The transport seam takes any of these — three methods and an `Inbound`. What
separates a weekend from a month is one question: **can the bot dial out, or
does it need an inbound HTTPS endpoint?** Slack's Socket Mode dials out, which
is why the box needs no public hostname, no open port and no signature
verification. Everything below is sorted by that.

### Dial-out, like Socket Mode

These cost about what Slack cost.

| Platform | How events arrive | Notes |
| :- | :- | :- |
| **Zulip** | long-polling events API | Its **topic** model is a first-class, named thread — the closest fit to agent binding of anything here. Open source, self-hostable. |
| **Mattermost** | WebSocket | Open source, self-hostable; common where Slack is not allowed. |
| **Rocket.Chat** | WebSocket (Realtime API) | Open source, self-hostable. |
| **Matrix / Element** | long-polling `/sync` | Federated, self-hostable, strongest privacy story. |
| **Google Chat** | **Cloud Pub/Sub**, or HTTPS | Pub/Sub lets the app subscribe and pull, so Workspace *does* have a dial-out path. Teams does not. |
| **Discord** | Gateway WebSocket | See the note below before building this. |
| **Telegram** | long polling | Likewise. |
| **IRC / XMPP** | native | Trivially outbound; still real in some communities. |

### Inbound endpoint required

Webex, Zoom Team Chat, Twist, Basecamp, and the smaller Slack-alikes — Flock,
Ryver, Chanty, Pumble. All carry the same cost as Teams: a public hostname and
certificates, or a relay. None are planned.

### Regional

**Lark/Feishu**, **DingTalk** and **WeCom** are large with capable bot APIs, and
worth knowing about if the audience calls for it. **Workplace from Meta** is
being wound down and is not worth targeting.

### Already solved elsewhere — do not build

**Discord and Telegram have official Claude Code channel plugins.** A channel is
an MCP server that pushes events into a running session and takes a reply tool,
which is most of what a Ricardi transport does. Building these transports would
duplicate something Anthropic ships. If someone wants Discord, the honest answer
is "use the channel plugin".

### Why the self-hosted ones are the interesting end state

Mattermost, Zulip, Rocket.Chat and Matrix all offer a dial-out event stream
*and* can run inside your own network. An agent on a box talking to a chat
server on the same LAN means the conversation never crosses the internet: no
public endpoint, no third party holding tokens, no relay for the chat hop.

For a project whose premise is "agents on a machine you control", that is a
coherent end state in a way Slack and Teams structurally cannot be. It is the
strongest argument for the transport seam existing at all.

### If a second transport gets built

**Zulip.** Outbound events, and its topic model fits thread binding well enough
that it would likely expose where Slack's `thread_ts` is carrying more weight
than it should — which is the kind of pressure a seam needs to prove it works.
**Google Chat over Pub/Sub** is the pragmatic alternative for Workspace shops.

## Microsoft Teams

Top of the list because it is what gets asked for, not because it is the
easiest. The transport seam takes it — `transports/teams.py` implementing the
same three methods is most of the work. The honest headline is that **Teams
reintroduces the inbound endpoint that Slack let us delete.**

Through Azure Bot Service and the Bot Framework there is no dial-out path. A bot
receives activities as HTTPS POSTs to a registered messaging endpoint, which
means one of:

- a public HTTPS endpoint on the box, with certificates and signature
  verification, which is the exposure we avoided; or
- a relay — Azure Relay, a dev tunnel, or a small cloud function forwarding to
  the box — which reintroduces a hop and a thing to operate; or
- a cloud-hosted Ricardi that reaches the box some other way, at which point the
  agents are not local and the premise changes.

None of these is fatal, and the second is probably right. But it is a different
security posture to the Slack path, so Teams support should ship with its own
section in `SECURITY.md` rather than a line in a feature list.

Also different: identity is an Entra object ID rather than a Slack user ID, so
the roster grows a notion of which directory an ID belongs to. Threads work
differently enough that the binding model needs checking rather than assuming.

No date. Wanted, not started.

## Likely

**An external PDP in practice.** The `PolicyPoint` interface and the HTTP shape
exist; what is missing is a worked example against a real OPA or PolicyArc, with
timeouts and fail-closed proven rather than asserted.

## Possible

**A real hold, not just an announcement.** Announce a command, wait, and proceed
only if nobody objects within N seconds. This turns the record into a checkpoint
and is the most-requested thing this design does not do. It needs care: who may
veto, what happens on timeout, and whether a held turn blocks the agent's lock.

**Other agent backends.** Channels, for sessions a human started with
`--channels`. The peer-messaging socket, for sessions started without one. Both
fit behind `AgentBackend`; both are less supported than the SDK path.

**Thread binding eviction.** `threads.json` only grows.

## Not planned

**Streaming an agent's working output.** Demos well, noise in a real channel.

**A richer rule language.** If rules need loops or arithmetic, that is the
external PDP's job. A DSL that grows conditionals is a program nobody can review,
and reviewability is the point.

**Letting `agents.toml` grant permissions again.** One reviewed file grants
everything, deliberately.

**Auto-discovering people from the Slack directory.** It would need
`users:read.email`, a reinstall, a per-message API call and a cache — to replace
a list of two lines that cannot change without a reviewed diff. Revisit at a few
dozen people, not before.

**Attaching to Claude Code sessions a human started in a terminal.** The SDK
cannot do it; `resume` starts a second process replaying the transcript, which
gives two agents diverging in one working tree. The channels and socket backends
above are the real answer to that want.
