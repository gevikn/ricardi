# Which one do you actually want?

This is not a competitive comparison. Anthropic ships **Channels**, a
first-party way to talk to a running Claude Code session from a messaging app,
and for a lot of people it is the right answer and Ricardi is not. This page
exists so you can tell which case you are in before you spend an evening on the
wrong one.

The short version: **if what you want is to reach your own session from your own
phone, use Channels.** It is about fifteen minutes of setup, it is maintained by
the people who make Claude Code, and it works. Ricardi earns its keep when the
session needs to be somewhere other than your laptop and the person you are
inviting is not you.

## What exists today

| Transport | Official Channels plugin | Ricardi | Notes |
|---|---|---|---|
| **Telegram** | yes | no | Bot token from BotFather; pairing-code flow. |
| **Discord** | yes | no | Server invite plus allowlist. |
| **iMessage** | yes | no | Apple platforms only. |
| **Slack** | **no** | **yes** | The gap Ricardi fills. |
| **WhatsApp** | no | no | Nobody has, and there is a reason — see below. |

Channels is bidirectional: messages from the chat reach the running session and
Claude replies in the same chat, with the session's full filesystem, MCP and git
access. Anything saying otherwise, including earlier drafts of this project's
own notes, was wrong.

## Where they actually differ

| | Ricardi | Channels |
|---|---|---|
| Where the session lives | a box you run, as a service | the session you launched |
| Up when your laptop is shut | **yes** | no |
| Enabled by | running the service | a `--channels` flag at launch |
| Who may speak | roster, resolved to a person | allowlist / pairing by account ID |
| **What their words may do** | **tiers, path roots, decided per tool call** | **not modelled** |
| More than one project at once | named sessions, separate scopes | one session per launch |
| Watch-only participants | yes, by not being on the roster | not a concept |
| Who maintains it | one person, this repository | Anthropic |
| Install from a marketplace | no — clone and run | `/plugin install` |

Two rows carry most of the weight.

**"What their words may do" is the one Channels does not have**, and it is not an
oversight — Channels assumes you are sitting in front of the session. Its access
model is good at deciding *who may send*: numeric ID allowlists, per-group opt-in,
even restricting which members of a group can trigger it. But once someone is
through that gate, their message runs in your session with your filesystem and
your git. That is entirely reasonable when you are watching. It is a different
proposition at 2am.

**"Up when your laptop is shut" is the other.** It is also *why* the first row
matters. When you are at the keyboard you are the supervision; when you are
asleep the policy is. The two features are not independent — an always-on
session without tiers is reckless, and tiers without always-on are mostly
ceremony.

## Per-transport notes

### Slack

Where work teams already are, which for this use case is the whole argument. A
CTO and a CPO are not pairing in Discord.

Ricardi uses **Socket Mode**: it dials out, nothing listens, the box needs no
inbound port and no public hostname. That property is load-bearing for the rest
of the design. It also closes one door permanently — Slack does not allow Socket
Mode apps in the public Marketplace, and since July 2026 a submission additionally
needs ten installs on active workspaces held through the whole review. So
distribution is clone-and-run, deliberately.

### Telegram and Discord

Both first-party, both solid, both free of Ricardi's setup cost. Telegram in
particular is the fastest path from nothing to a second person in your session:
a BotFather token, a plugin install, a relaunch with `--channels`, and a
six-character pairing code.

The question is not whether they work. It is whether the person you want to
invite is there. For a side project with a friend, yes. For the colleague who
needs to see why the migration failed, probably not.

### iMessage

Apple-only on both ends, which makes it excellent for one specific person and
unusable as a team surface.

### WhatsApp

The interesting gap. It is the default messenger for most of the world outside
the US, and nothing official reaches it. A third-party project claims support;
it is not Anthropic's.

The reason it is empty is structural rather than accidental. Telegram hands you
a bot token from a chat with BotFather in about ninety seconds. WhatsApp has no
equivalent: you go through the WhatsApp Business Platform, which wants a Meta
business account, a verified number, and has its own rules about which messages
you may originate and when. That is a business-onboarding problem rather than an
engineering one, which is exactly the kind of obstacle that leaves a gap sitting
open for a long time.

## When **not** to use Ricardi

- You want to reach **your own** session from your phone. Channels, every time.
- The people you would invite live in Discord or Telegram already.
- You do not have a machine that stays on, and do not want one.
- You want something first-party, supported, and updated by Anthropic.
- You are working alone.

## When Ricardi

- The person you want to invite is in **Slack**, because that is where work is.
- You want the session reachable when your laptop is **shut**.
- You want to invite someone you trust **partially** — a junior engineer, a
  technical sales engineer, a product lead — and bound what they can do rather
  than deciding between all and nothing.
- You want **several** projects running as separate named sessions with separate
  scopes.
- You want people who can **watch and learn** without being able to act.

See [`pair-programming.md`](pair-programming.md) for how to actually run it, and
[`SECURITY.md`](../SECURITY.md) for what the guards do and do not stop.
