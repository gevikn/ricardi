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
| `seq`   | integer | orders events within one `name`'s stream: it starts at 1, strictly increases, and never repeats. A gap means an event could not be delivered server-side; it is not corruption, and a client must not resync or drop the connection because of one |
| `at`    | string  | RFC 3339, UTC, whole seconds, e.g. `2026-11-10T15:04:05Z` |
| `name`  | string  | the agent the event belongs to; streams are partitioned per name so one project's transcript never reaches a client watching another |
| `turn`  | string  | the turn this event belongs to: one id per message answered, carried by the agent's events, the gate's refusals and anything Ricardi says about that message. A message she answers herself -- help, a listing -- is a turn of one event, with no `turn_started` |
| `actor` | string  | who caused it: `owner`, `person`, `agent` or `ricardi` |
| `kind`  | string  | which of the kinds below this is |

Example:

```json
{"v": 1, "seq": 7, "at": "2026-11-10T15:04:05Z", "name": "ricardi",
 "turn": "t1", "actor": "agent", "kind": "said", "text": "hi"}
```

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
| `summary`     | string | a one-line description of the call, including the text of its main argument (a command, a path), capped at 300 characters |
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

Unlike `tool_started`, this carries content. The redaction rule is about tool
input, where a password lives; a diff is the file the agent changed, and a
watcher sees everything the session prints. No tier governs that.

### `pending`

| field            | type            | meaning |
|------------------|-----------------|---------|
| `correlation_id` | string          | how an answer finds this question |
| `question`       | string          | what is being asked |
| `options`        | array of string | the answers offered, empty when free text |

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
