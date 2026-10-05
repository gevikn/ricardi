# Why Ricardi

*Vitreledonella richardi* is the glass octopus: a deep-sea animal that is almost
entirely transparent. Pass a light through one and you see the gut, the optic
nerve and little else.

Two things about it describe this project better than anything literal would.

## Eight arms, mostly independent

An octopus does not run its arms the way a hand runs fingers. The majority of
its neurons are **in the arms**, which solve problems locally and report back.
The central brain decides what should happen, not how each arm does it. Sever
the connection and an arm keeps working on the task in front of it.

That is the architecture. Ricardi is small and does almost no thinking: it works
out who is asking, which agent they mean, and whether they are allowed. The
work happens in the agents, each in its own directory, each holding its own
conversation, none of them aware of the others. The centre routes. The arms
think.

It is also why the per-agent lock exists. An arm does one thing at a time.

## Transparent

The second property is the one that matters for a program that stands between
people and something powerful.

A proxy you cannot see through is a proxy you have to trust. This one is built
so you do not have to: every command an agent runs is announced in the channel
before it runs, every decision — including the refusals that never reach Slack —
is written to an append-only log, and the rules that produced those decisions
are a file two people can read and diff.

A glass octopus hides nothing because it has no pigment to hide behind. Pleasant
ambition for a thing holding the keys to your machine.

## The spelling

The species is *richardi*, with an `h`, named after Jules Richard, the
oceanographer. This project is **Ricardi**, without one — a coinage, not a
binomial. If you came here from taxonomy, that is why it looks wrong.

## What it is not named after

Not a person. Not an assistant. Not a Claude. The thing in the middle of this
system is infrastructure, and naming it after a helpful human invites you to
think of it as one — which is exactly the wrong instinct for a component whose
job is deciding what is permitted.
