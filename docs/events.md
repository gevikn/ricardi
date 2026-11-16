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
| `seq`   | integer | orders events within one `name`'s stream |
| `at`    | string  | RFC 3339, UTC, whole seconds, e.g. `2026-11-10T15:04:05Z` |
| `name`  | string  | the agent the event belongs to; streams are partitioned per name so one project's transcript never reaches a client watching another |
| `turn`  | string  | the turn this event belongs to |
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

A new *kind* is additive and safe for a lenient client. A new *envelope field*
is a schema-version change: a strict reader refuses any key it does not know,
so every strict reader would refuse the new events. That is intended. A body
field may not share a name with an envelope field, `v` or `kind`; the module
refuses it at import.

## Output cap

Free text is capped per event; the cap and what a capped event looks like are
defined alongside the events that carry output, and are added to this document
with them.

## The server is strict, a client is lenient

The server refuses an event whose kind it does not know, whose fields do not
match its kind, or whose `actor` or `outcome` is outside the vocabulary above.

A client does the opposite about kinds: it ignores one it does not know, and
ignores fields it does not know on one it does. That is what lets a new kind,
a screencast frame for instance, arrive later without reissuing every client.
In the Python binding a lenient read keeps such an event as `Unknown`, with its
kind and its remaining fields unparsed; it is a reader's placeholder, never
something a writer produces, so it is not a kind and has no section here.
