# Pairing with Ricardi

The problem this solves is not "my team should use Claude Code". They already
do. It is that when you want a second person in a session you are running, you
become the wire between them. You read what came back, you say it out loud, you
listen to what they think, you translate it, you type it, you wait, you read it
again. You are a protocol, and a slow one, and you are the only one who can be
it.

Two things are wrong at once, and fixing either alone is not enough. They cannot
**see** it — they get your summary rather than the thing. And they cannot
**touch** it — every correction they would make goes through you retyping it.
Screen-sharing fixes the first and leaves the second, which is why it has never
felt like enough.

## The shape

One session, one name, one channel. **Two people with hands on it; three at the
outside.** That is not a limit being apologised for — it is what pairing is.
Past three, see [Watchers](#watchers-when-you-want-more-than-three) below,
because what you want then is a different thing with a different answer.

## What always-on changes

A session attached to your laptop makes your pair a guest while you drive. You
have stopped reciting, which is most of the win, but you are still present and
still the dependency.

A session that lives on a box removes the second half. Your pair can move the
work forward **when you are not there at all** — which is what a CTO and a CPO
actually look like. They do not sit down for a two-hour block; they poke at the
same thing across a day and a night.

That is also the moment the tiers stop being decoration:

> When you are at the keyboard, **you** are the supervision. You see what they
> typed and you can stop it. When you are asleep, the tier is the only thing
> between their 11pm question and your repository.

## Setting up a pair

**1. Decide the tier before you invite, not after.**

| Tier | For |
|---|---|
| `write` | Someone you would hand the keyboard to. They can change things. |
| `read` | Someone who should be able to ask and understand, but not change. |
| `none` | On the roster, granted nothing. Explicit, and auditable as such. |

There is deliberately no `exec` tier. What a shell may do is the scope of the
*session*, not a property of the person asking — a tier that pretended otherwise
would be lying about what it controls.

**2. Use a private channel.** Channel membership is how people reach the
session. In a public channel anyone in the workspace can add themselves, and
while policy will refuse them a decision, they will still read everything the
session prints.

**3. Work on a branch.** Always. A session that cannot reach `main` is a session
whose worst night produces something you read in the morning.

**4. Protect `main`.** This is the part that is not Ricardi's job at all, and
that is exactly why it is reassuring — it is standard GitHub machinery that
nobody has to take your word for.

## Protecting the branch

Two layers that do not overlap. Ricardi's policy bounds what the tool may do on
disk: which paths, read or write, which commands, decided at the tool call.
Branch protection bounds what can reach `main` **regardless of what the tool
did**. The second layer is boring, standard, and understood by everyone you
would pitch this to.

A ruleset that matches the advice above:

```bash
gh api -X POST repos/<owner>/<repo>/rulesets --input ruleset.json
```

```json
{
  "name": "main",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "required_linear_history" },
    { "type": "pull_request",
      "parameters": { "required_approving_review_count": 0 } },
    { "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "required_status_checks": [ { "context": "your test job name" } ]
      } }
  ]
}
```

Three choices in there are worth making deliberately:

- **`bypass_actors: []`** — nobody, including you. The one night it matters is
  the night you are tired.
- **`required_approving_review_count`** — `0` if you are the only human, because
  you cannot approve your own pull request and a `1` would wedge `main` shut
  forever. Set it to `1` the moment there are genuinely two of you: then the
  pair reviews the pair, which is the whole point.
- **Required checks must be checks that actually report.** A required check that
  never runs blocks every merge permanently. Add one, watch it report on a
  docs-only pull request, then add the next.

## Spikes

Spikes fit this better than feature work does. A spike is throwaway by
definition, lives on a branch by definition, and is judged by what it taught you
rather than by whether it is correct. Handing one to a teammate with a bounded
tier and reading the branch in the morning costs you very little if it goes
nowhere — which is the normal outcome of a spike and not a failure of it.

Keep the throwaway throwaway. Deciding to keep what a spike produced is a new
decision, made awake, in daylight.

## Watchers: when you want more than three

Past three people with hands on it, you no longer have a pair. You have a
broadcast — and that turns out to be a thing worth wanting on purpose.

Think of it the way people watch an expert play on Twitch. The value is not that
the audience participates. It is that watching someone good work through a real
problem, including the wrong turns, teaches things no write-up does. A session
transcript is that, for your codebase, for free.

**A watcher is someone in the channel who is not on the roster.** They read
everything; they decide nothing. This is structural rather than conventional:
`Principal.known` is false for anyone the roster does not resolve, and every
allow path in the policy engine refuses an unknown asker before it considers
anything else. A watcher who types gets a refusal, and the refusal is audited
rather than silent.

So the arrangement is:

| | On the roster | In the channel | Can act |
|---|---|---|---|
| Pair | yes, `write` | yes | yes |
| Reader | yes, `read` | yes | asks, cannot change |
| Watcher | **no** | yes | no |

Note the difference between a **reader** and a **watcher**. A reader is on the
roster and can ask the session questions, getting read-only tool use. A watcher
cannot ask anything at all — they see the exchange between other people. For
teaching, the watcher is usually what you want: the lesson is someone else's
session, not a dozen people interrupting it.

### The honest part

**A watcher sees everything the session prints.** File contents, command output,
stack traces, environment that leaked into an error message. The tier system has
nothing to say about this, because none of it is a decision — it is a message in
a channel they are a member of. If the work would print something you would not
paste into that channel yourself, do not have watchers in it.

**A tier is not a sandbox.** A `read` tier stops a colleague changing something
by mistake. It does not stop a colleague who means to, because a shell can cause
a write indirectly whatever the rules say. This is the same thing
[`SECURITY.md`](../SECURITY.md) says and it is worth saying twice: the guards
here are built for accidents, not for adversaries.

Which is the right tool for the actual job. The junior engineer you invite is
not attacking you. They are acting in good faith at 11pm and might still remove
the wrong directory — and bounding that is precisely what makes it reasonable to
invite them at all. **The tiers do not shrink what Ricardi does; they grow who
you can invite.** Without them, the only person you can hand an always-on
session to is someone you would hand root, which is your co-founder and nobody
else.
