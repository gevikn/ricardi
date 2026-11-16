# The event wire contract

A turn is a stream of events. Each is one JSON object, flat, with no nesting
the client has to unwrap. The Python types in `src/ricardi/events.py` are one
binding of this document; a browser or terminal client reads the JSON.

## Version

`v` is `1`. It changes only when an existing field changes meaning. A new kind
is not a version change. A reader that sees a `v` it does not speak refuses the
event, strict or lenient, because leniency covers additions, not changed
meanings.

## Envelope

Every event carries these, then `kind`, then the kind's own fields.

| field   | type    | meaning |
|---------|---------|---------|
| `v`     | integer | the schema version above |
| `seq`   | integer | orders events within one `name`'s stream **for as long as the server runs**: it starts at 1, strictly increases, and never repeats *within one process*. It is not persisted, so a restart begins the same name's stream again at 1 — see "Turn lifecycle". A gap means an event could not be delivered server-side; it is not corruption, and a client must not resync or drop the connection because of one |
| `at`    | string  | RFC 3339, UTC, whole seconds, e.g. `2026-11-10T15:04:05Z` |
| `name`  | string  | the agent the event belongs to; streams are partitioned per name so one project's transcript never reaches a client watching another. The empty string is a real name: it is the stream for a conversation not yet bound to any agent, and every such conversation in every channel shares it (see "The unbound stream") |
| `turn`  | string  | the turn this event belongs to: one id per message answered, carried by the agent's events, the gate's refusals and anything Ricardi says about that message. A message Ricardi answers herself -- help, a listing, a refusal -- has no `turn_started` and usually one event, though a refusal can carry a `refused` and a `said` under the same id |
| `actor` | string  | who caused it: `owner`, `person`, `agent` or `ricardi` |
| `kind`  | string  | which of the kinds below this is |

Example:

```json
{"v": 1, "seq": 7, "at": "2026-11-10T15:04:05Z", "name": "ricardi",
 "turn": "t1", "actor": "agent", "kind": "said", "text": "hi"}
```

## Turn lifecycle

The first thing a client needs is a rule for when a turn is over, and whether
it can be told twice.

**Exactly one `turn_ended` per agent turn, and it is that turn's last event.**
Not two, and never none. The backend emits it from a `finally` that every way
out of a turn passes through — a result, an exception, its own deadline, a
cancellation from somewhere else — so a turn left open with the client spinning
is impossible rather than merely unlikely. Its `outcome` says which of those it
was: `done`, `cut_short` (a ceiling was reached, or somebody stopped it),
`failed` (something broke) or `timed_out` (the turn ran past its deadline). A
client may treat `turn_ended` as final for that `turn` id and release whatever
it was holding.

**`turn_started` comes first when it comes at all.** A turn that runs an agent
opens with one. A message Ricardi answers herself — help, an agent listing, a
refusal — has no `turn_started`: it still carries a `turn` id, and it still
ends, but there is no agent turn to open. A client must therefore not wait for
a `turn_started` before rendering, and must not treat its absence as a lost
event. There is also one narrow case where a turn ends without having started:
the opening emit can be cancelled mid-flight, and the turn is closed anyway
rather than left open. A `turn_ended` for a turn the client never saw begin is
to be handled, not resynced over.

**`seq` restarts at 1 when Ricardi restarts.** The counter lives on an
in-memory `Stream` per name, rebuilt when the process starts; nothing persists
it, and persistence is out of scope for this version. So a client that watches
across a deploy will see a name's `seq` fall back to 1 and climb again. That is
a restart, not corruption and not a replay: a client must not resync, drop the
connection or reorder on it, any more than it does for a gap. A client that
needs to tell one run from another should key on the `turn` ids it sees, which
are random per turn and not reused.

## The unbound stream

`name` is `""` for a conversation that has not been bound to an agent yet —
someone said hello, or asked for help, before naming one. These share a single
stream, across every channel and every thread, so `""` is not a partition in
the sense the other names are: a client subscribing to it would see every
unbound conversation on the box, not one. Treat it as "Ricardi, speaking for
herself, about nothing in particular" rather than as an agent. Once a thread is
bound, its events carry that agent's name.

## Kinds

### `turn_started`

| field  | type   | meaning |
|--------|--------|---------|
| `text` | string | the message that began the turn |

### `said`

| field  | type   | meaning |
|--------|--------|---------|
| `text` | string | text the agent addressed to the reader |

### `thought`

| field  | type   | meaning |
|--------|--------|---------|
| `text` | string | the agent's reasoning, shown apart from what it said |

### `turn_ended`

| field     | type   | meaning |
|-----------|--------|---------|
| `outcome` | string | `done`, `cut_short`, `failed` or `timed_out` |
| `answer`  | string | the final answer, empty when there is none |

### `tool_started`

| field         | type   | meaning |
|---------------|--------|---------|
| `tool`        | string | the tool's name |
| `summary`     | string | a one-line description of the call: the tool's name, then the text of its main argument (a command, a path). The *argument text* is capped at 300 characters and an ellipsis marks a cut; the field also carries the tool's name and a `: ` separator, so `summary` itself can run a little past 300 and has no exact bound |
| `tool_use_id` | string | ties this to its `tool_output` and `tool_finished` |

`summary` carries argument *text*, as the Slack channel already shows it: a
person approving a command has to see the command. A secret passed on a command
line will therefore appear here. What never enters the stream is the argument
*structure*: no kind has a field that can hold a mapping or a list of
arguments, so a client never receives the tool input object.

### `tool_output`

| field         | type    | meaning |
|---------------|---------|---------|
| `tool_use_id` | string  | the call this output belongs to |
| `text`        | string  | the output, capped (see Output cap) |
| `truncated`   | boolean | true when `text` is shorter than what the tool produced |

### `tool_finished`

| field         | type    | meaning |
|---------------|---------|---------|
| `tool_use_id` | string  | the call that ended |
| `tool`        | string  | the tool's name |
| `ok`          | boolean | whether the call succeeded |
| `ms`          | integer | how long it took, in milliseconds |

### `refused`

| field    | type   | meaning |
|----------|--------|---------|
| `rule`   | string | which rule decided; empty when the default did |
| `reason` | string | what to tell the person, without naming the rule |
| `tier`   | string | the tier the person held, empty if none |
| `tool`   | string | the tool refused, empty when not a tool call |
| `target` | string | what the request was aimed at, empty if nothing |

### `diffed`

| field   | type            | meaning |
|---------|-----------------|---------|
| `path`  | string          | the file the agent changed |
| `hunks` | array of string | unified-diff hunks |

**Reserved: no version emits this yet.** It is in the vocabulary and in the
wire contract so that a client can be written for it, and so that the rule
below about content is settled before anything produces one.

Unlike `tool_started`, this carries content. The redaction rule is about tool
input, where a password lives; a diff is the file the agent changed, and a
watcher sees everything the session prints. No tier governs that.

### `pending`

| field            | type            | meaning |
|------------------|-----------------|---------|
| `correlation_id` | string          | how an answer finds this question |
| `question`       | string          | what is being asked |
| `options`        | array of string | the answers offered, empty when free text |

**Reserved: no version emits this yet.** Nothing in Ricardi asks a person a
question mid-turn — a tool decision is made by policy, not by a prompt — so a
client will not receive one from this version. The shape is fixed here for the
hold that would produce it.

A new *kind* is additive and safe for a lenient client. A new *envelope field*
is a schema-version change: a strict reader refuses any key it does not know,
so every strict reader would refuse the new events. That is intended. A body
field may not share a name with an envelope field, `v` or `kind`; the module
refuses it at import.

## Output cap

`tool_output.text` is capped at `MAX_OUTPUT`, 8000 characters. Longer output
is cut to that length and `truncated` is `true`; output of exactly 8000 is not
truncated. The cap applies per event, so a client never has to size a frame for
an unbounded log. Other free text (`said`, `thought`, `diffed`) is not capped
here.

## The server is strict, a client is lenient

The server refuses an event whose kind it does not know, whose fields do not
match its kind, or whose `actor` or `outcome` is outside the vocabulary above.

A client does the opposite about kinds: it ignores one it does not know, and
ignores fields it does not know on one it does. That is what lets a new kind,
a screencast frame for instance, arrive later without reissuing every client.
In the Python binding a lenient read keeps such an event as `Unknown`, with its
kind and its remaining fields unparsed; it is a reader's placeholder, never
something a writer produces, so it is not a kind and has no section here.
