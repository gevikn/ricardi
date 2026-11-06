# Requests for a shared session

One file per session, named for the session: `project1.toml` asks for a session
called `project1`.

## Why a pull request and not a form

You cannot ask for a session in Slack, because what a request produces *is* the
channel. And the half of provisioning that mints a key, creates a repository and
registers a name must not be reachable from a channel at all — a remotely
reachable path to those is the thing keeping them out of Slack was meant to
avoid.

So a request is a file somebody reviews. That is the same shape as `policy.toml`:
the thing that grants is a file two people read, and the escalation step is a
human pressing merge. It also needs no inbound port, which is most of the reason
this deployment is simple — see the note at the top of `compose.yaml`.

## How to ask

1. Copy `provision.example.toml` to `requests/<your-session>.toml` and edit it.
   The comments in that file explain every field, including the ones that are
   about trust rather than syntax.
2. Check it before anyone else has to:

   ```
   ricardi check-request requests/<your-session>.toml
   ```

   Or `ricardi check-request requests` for the lot. It applies nothing and
   reaches nothing: it reads your file, and reads `agents.toml` only for the
   names already taken. On a clone with no `agents.toml` it says so and checks
   everything else — only the collision with an existing session goes
   unchecked there.

3. Open a pull request. CI runs that same command over this directory, so
   syntax, tiers, duplicate people, reserved names, collisions within the pull
   request and missing git usernames all come back to you before a human reads
   it.
4. An admin reviews and merges. What CI cannot check is whether you should have
   it — that is what the review is for.

## What you will be asked for, and why

Both a Slack id **and** a git username for everyone. Only you can supply that
pairing: Slack and GitHub do not know about each other, and GitHub will not
resolve someone by email unless they have made it public.

A budget and a turn ceiling, both above zero. `agents.toml` lets `0` mean "no
ceiling" deliberately; a request may not, because an unbounded session should be
something somebody chose rather than a field left blank.

A tier for each invitee — `read`, `write` or `none`. There is no `exec` tier:
what a shell may do is the session's scope, not something a person carries.

## Before you invite anyone

A read tier stops a colleague changing something by accident. It does not stop a
colleague who is trying, and it cannot: everyone in the channel sees everything
the session says, and a shell can cause a write indirectly whatever the tier.
**Do not invite people you do not trust.**

## What is not built yet

This directory and its checker are the owner-to-admin contract. Applying a
merged request — laying out disk, cloning, registering the name, creating the
channel, writing grants — is not implemented, because several of its decisions
are still open. See
`docs/superpowers/specs/2026-10-28-shared-sessions-design.md`, sections 11
and 14. Until then a merged request is an instruction to an admin, not a
trigger.
