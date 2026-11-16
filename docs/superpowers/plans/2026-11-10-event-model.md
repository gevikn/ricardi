# Event Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ricardi emits a typed event stream for a whole turn — tool calls,
outcomes, streamed text and refusals — and Slack becomes one renderer of it
rather than the only thing that exists.

**Architecture:** A new `src/ricardi/events.py` owns the vocabulary; neither
seam owns it and both depend on it. `Caller` carries an `EventSink` into the
backend, so `sdk.py` emits as the turn unfolds while `ask()` still returns the
final answer. `Transport` gains `emit(event)` and loses `send` from its
protocol; `slack.py` renders events to text and degrades what it cannot show.
No client, no HTTP transport, no persistence — those are later plans.

**Tech Stack:** Python 3.11, stdlib only (`dataclasses`, `json`, `uuid`). No
new runtime dependency: `pyproject.toml` has `dependencies = []` and keeps it.

**Spec:** [`docs/superpowers/specs/2026-10-29-own-interfaces-design.md`](../specs/2026-10-29-own-interfaces-design.md)
— approved, committed, amended 2026-11-08 (§18). This plan is build-order
§14 steps 1–3.

## Global Constraints

- **The vocabulary is defined in neither seam.** CLAUDE.md: a change that makes
  either seam aware of the other is "going the wrong way" (§2). `events.py`
  imports from neither `transports/` nor `backends/`.
- **Tool events carry `summarise()`-grade input. Raw arguments never enter the
  stream** (§7). CLAUDE.md states it more strongly: "Do not log raw tool input.
  A log that records every argument eventually records a password."
- **The deliverable is a documented JSON wire schema with tests of its own**,
  not Python dataclasses whose serialisation is whatever `asdict` produced
  (§18). The Python types are one binding of it.
- **Streams are partitioned per name** (§5). Every event carries `name`.
- **Reserve the answerable kind and a correlation id now** (§7). Retrofitting
  it later is the both-clients-rewritten failure.
- **Nothing may auto-approve a tool.** No `allowed_tools`, no
  `permission_mode`, no `setting_sources` (CLAUDE.md). This plan touches the
  gate only to make a denial *visible*; it never changes what is decided.
- **Never weaken a policy test to make it pass** (CLAUDE.md).
- `RICARDI_LIVE=1 pytest -m live` before claiming any task that touches
  `backends/sdk.py`, `policy.py` or `audit.py` is done (CLAUDE.md). Tasks 2, 4
  and 5 carry it.
- Comments explain why, not what (CLAUDE.md).

## Out of scope, deliberately

- `transports/http.py`, both clients, Access, Web Push — §14 steps 4–8.
- **Persistence and replay** (§7). The stream is live-only here; `state/`
  untouched.
- **Emitting diffs.** The `Diffed` kind is defined and round-tripped in Task 2
  so the model does not foreclose it, but nothing emits one: deriving a real
  diff needs the file before and after, which a `ToolResultBlock` does not
  carry, and Slack cannot render a diff anyway (§13). The browser plan owns it.
- Shared-sessions step 4 (channel 1:1 name). It gates `http.py`'s addressing,
  not this.

## Review Focus

Five things the spec implies, no task's happy path exercises, and a person
would meet. Each has a test in the task that owns the code.

1. **A turn that fails or times out must still close.** `_one_turn` can raise
   `BackendError` or time out; a client that never receives `TurnEnded` shows a
   turn spinning forever. Task 4.
2. **A sink that raises must not break the turn.** The existing `notice` path
   swallows its own failure (`sdk.py:281`); an event sink is no more entitled
   to kill a turn than an announcement is. Task 3.
3. **A tool whose input has no summary key** summarises to the bare tool name
   (`policy.summarise` returns `tool`). The event must still identify the call.
   Task 2.
4. **An agent that says nothing at all.** `assemble` returns "(finished without
   saying anything)"; the stream must show a turn that started and ended with
   no `Said`, not an empty turn indistinguishable from a lost one. Task 4.
5. **One enormous tool output.** A single `ToolOutput` of megabytes would be
   held in memory and later written to a browser. Capped per event, with the
   cap stated in the wire schema. Task 2.

---

## File Structure

| File | Responsibility |
| :- | :- |
| `src/ricardi/events.py` (new) | The vocabulary: envelope, one body per kind, `to_wire`/`from_wire`, `EventSink`, `Recorder`, `Stream`. Imports nothing from `transports/` or `backends/`. |
| `src/ricardi/render.py` (new) | Events → Slack-grade text. Owns the truncation that `assemble` owns today. |
| `docs/events.md` (new) | The wire contract: every kind, every field, the cap, the version, and the strict-server/lenient-client rule. |
| `src/ricardi/backends/base.py` | `Caller` gains `events`. |
| `src/ricardi/backends/sdk.py` | `_one_turn` and `_gate` emit. |
| `src/ricardi/policy.py` | `Decision` gains `tier`. |
| `src/ricardi/transports/base.py` | `Transport.emit(event)`; `send` leaves the protocol. |
| `src/ricardi/transports/slack.py` | Renders events; keeps `send`/`send_private` as its own detail. |
| `src/ricardi/transports/local.py` | Renders events; keeps the stdlib-only HTTP shape. |
| `src/ricardi/app.py` | Builds a `Stream` per turn and emits instead of saying. |

---

## Task 1: The envelope, the text kinds, and the wire contract

**Files:**
- Create: `src/ricardi/events.py`
- Create: `docs/events.md`
- Test: `tests/test_events.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SCHEMA_VERSION: int = 1`; `Envelope(seq: int, at: str, name: str,
  turn: str, actor: str)`; body classes `TurnStarted(text: str)`,
  `Said(text: str)`, `Thought(text: str)`,
  `TurnEnded(outcome: str, answer: str)`, each with
  `KIND: ClassVar[str]`; `Unknown(kind: str, fields: dict[str, object])`;
  `Event(envelope: Envelope, body: object)`;
  `to_wire(event: Event) -> dict[str, object]`;
  `from_wire(data: dict[str, object], *, strict: bool = True) -> Event`;
  `now() -> str` (RFC 3339, UTC, seconds precision);
  `ACTORS = ("owner", "person", "agent", "ricardi")`;
  `OUTCOMES = ("done", "cut_short", "failed", "timed_out")`.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_event_round_trips_through_the_wire_form():
    event = Event(Envelope(seq=1, at="2026-11-10T15:04:05Z", name="ricardi",
                           turn="t1", actor="agent"), Said(text="hello"))
    assert from_wire(to_wire(event)) == event

def test_the_wire_form_is_flat_and_names_its_kind_and_version():
    wire = to_wire(Event(Envelope(seq=7, at="2026-11-10T15:04:05Z",
                                  name="ricardi", turn="t1", actor="agent"),
                         Said(text="hi")))
    assert wire == {"v": 1, "seq": 7, "at": "2026-11-10T15:04:05Z",
                    "name": "ricardi", "turn": "t1", "actor": "agent",
                    "kind": "said", "text": "hi"}

def test_a_kind_this_version_does_not_know_is_refused_server_side():
    with pytest.raises(ValueError):
        from_wire({"v": 1, "seq": 1, "at": "2026-11-10T15:04:05Z",
                   "name": "n", "turn": "t", "actor": "agent",
                   "kind": "screencast_frame", "data": "..."})

def test_a_lenient_reader_keeps_an_unknown_kind_instead_of_failing():
    event = from_wire({"v": 1, "seq": 1, "at": "2026-11-10T15:04:05Z",
                       "name": "n", "turn": "t", "actor": "agent",
                       "kind": "screencast_frame", "data": "x"}, strict=False)
    assert isinstance(event.body, Unknown)
    assert event.body.kind == "screencast_frame"
    assert event.body.fields == {"data": "x"}

def test_every_wire_form_is_json_serialisable():
    for body in (TurnStarted(text="go"), Said(text="hi"),
                 Thought(text="hmm"), TurnEnded(outcome="done", answer="hi")):
        json.dumps(to_wire(Event(Envelope(1, now(), "n", "t", "agent"), body)))

def test_an_actor_or_outcome_outside_the_vocabulary_is_refused():
    with pytest.raises(ValueError):
        Event(Envelope(1, now(), "n", "t", "nobody"), Said(text="hi"))
    with pytest.raises(ValueError):
        Event(Envelope(1, now(), "n", "t", "agent"),
              TurnEnded(outcome="exploded", answer=""))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_events.py -v`
Expected: FAIL on `ModuleNotFoundError: ricardi.events`.

- [ ] **Step 3: Implement `events.py`**

A frozen dataclass per body with `KIND` as a `ClassVar[str]`; a module-level
`_BODIES: dict[str, type]` built from them, which is also what `from_wire`
dispatches on and what `docs/events.md` is checked against in Task 2.
`Envelope.__post_init__` validates `actor` against `ACTORS` — on the envelope,
because that is the field's home; `TurnEnded.__post_init__` validates `outcome`
against `OUTCOMES`. `to_wire` merges `{"v": SCHEMA_VERSION}`, the
envelope's fields, `{"kind": body.KIND}` and the body's fields into one flat
dict — flat because a client reads `kind` and then its own fields, and nesting
buys nothing a browser wants.

- [ ] **Step 4: Run them to verify they pass**

Run: `pytest tests/test_events.py -v` → PASS.

- [ ] **Step 5: Write `docs/events.md`**

The contract, not a tutorial: the version, the envelope's five fields, one
section per kind listing its fields and types, the per-event output cap from
Task 2, and the rule that **the server is strict and a client is lenient** — a
client ignores a kind it does not know, which is what lets a screencast frame
(§11) arrive later without reissuing every client.

- [ ] **Step 6: Commit**

```bash
git add src/ricardi/events.py docs/events.md tests/test_events.py
git commit -m "feat(events): the envelope, the text kinds, and a wire contract"
```

---

## Task 2: Tool, refusal, diff and answerable kinds

**Files:**
- Modify: `src/ricardi/events.py`
- Modify: `src/ricardi/policy.py` (`Decision`, and the `replace(request, tier=tier)` site at `policy.py:521`)
- Modify: `docs/events.md`
- Test: `tests/test_events.py`, `tests/test_policy.py`

**Interfaces:**
- Consumes: Task 1's `Event`, `Envelope`, `to_wire`, `from_wire`, `_BODIES`.
- Produces: `MAX_OUTPUT: int = 8000`;
  `ToolStarted(tool: str, summary: str, tool_use_id: str)`;
  `ToolOutput(tool_use_id: str, text: str, truncated: bool)`;
  `ToolFinished(tool_use_id: str, tool: str, ok: bool, ms: int)`;
  `Refused(rule: str, reason: str, tier: str, tool: str, target: str)`;
  `Diffed(path: str, hunks: tuple[str, ...])`;
  `Pending(correlation_id: str, question: str, options: tuple[str, ...])`;
  `tool_started(tool: str, tool_input: dict, tool_use_id: str) -> ToolStarted`;
  `tool_output(tool_use_id: str, text: str) -> ToolOutput`;
  `Decision.tier: str = ""`.

- [ ] **Step 1: Write the failing tests**

```python
def test_no_body_can_hold_a_mapping():
    # Structural, not behavioural: "raw arguments never enter the stream" (§7)
    # is a property of the vocabulary, not a promise each caller remembers.
    for body in _BODIES.values():
        for f in dataclasses.fields(body):
            assert "dict" not in str(f.type).lower(), (body.__name__, f.name)

def test_a_tool_event_carries_only_the_summary_of_its_input():
    started = tool_started("Bash", {"command": "psql -U admin -W hunter2"}, "tu_1")
    assert started.summary == summarise("Bash", {"command": "psql -U admin -W hunter2"})
    assert "hunter2" not in to_wire(
        Event(Envelope(1, now(), "n", "t", "agent"), started)).__repr__()

def test_a_tool_with_no_summarisable_input_is_still_identified():
    started = tool_started("TodoWrite", {"todos": [{"a": 1}]}, "tu_2")
    assert started.tool == "TodoWrite"
    assert started.summary == "TodoWrite"

def test_output_is_capped_and_says_that_it_was():
    out = tool_output("tu_1", "x" * (MAX_OUTPUT + 500))
    assert len(out.text) == MAX_OUTPUT and out.truncated is True
    assert tool_output("tu_1", "short").truncated is False

def test_the_new_kinds_round_trip():
    for body in (ToolStarted("Bash", "Bash: ls", "tu_1"),
                 ToolOutput("tu_1", "ok", False),
                 ToolFinished("tu_1", "Bash", True, 12),
                 Refused("tier", "You hold read on ricardi.", "read", "Edit", ""),
                 Diffed("src/a.py", ("@@ -1 +1 @@", "-a", "+b")),
                 Pending("c1", "Run `git push`?", ("yes", "no"))):
        event = Event(Envelope(1, now(), "n", "t", "agent"), body)
        assert from_wire(to_wire(event)) == event

def test_every_kind_in_the_module_is_documented():
    text = (ROOT / "docs" / "events.md").read_text(encoding="utf-8")
    for kind in _BODIES:
        assert f"`{kind}`" in text, kind

# tests/test_policy.py
async def test_a_decision_carries_the_tier_it_was_decided_against():
    # The tier is resolved inside `decide` (policy.py:521) and the caller's
    # Request never sees it, so without this the refusal event cannot say
    # which tier the person actually held.
    decision = await policy.decide(Request(action="tool", principal=reader,
                                           agent="ricardi", tool="Edit"))
    assert decision.allow is False
    assert decision.tier == "read"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_events.py tests/test_policy.py -v`
Expected: FAIL on the new names, and on `Decision` having no `tier`.

- [ ] **Step 3: Add the kinds, the factories and `Decision.tier`**

`tool_started` calls `ricardi.policy.summarise` — the vocabulary may depend on
the policy module, which is neither seam; what it may not do is let a caller
hand it a dict. `ToolStarted` has no field that can hold one, which is what the
structural test pins. `Decision.tier` is filled wherever `decide` resolved a
tier; everywhere else it keeps its default.

**Why `Diffed` carries content when `ToolStarted` must not.** §7's redaction
rule is about tool *input*: a password lives in an argument. A diff is the file
the agent changed, and §10 is explicit that "a watcher sees everything the
session prints. No tier governs that." The asymmetry is deliberate, so it is
written down rather than rediscovered.

- [ ] **Step 4: Run them to verify they pass**

Run: `pytest tests/test_events.py tests/test_policy.py -v` → PASS.

- [ ] **Step 5: Run the whole suite and the live suite**

Run: `pytest` → all pass (`policy.py` changed).
Run: `RICARDI_LIVE=1 pytest -m live` → all pass. CLAUDE.md requires it for
`policy.py`; record the result in the task report either way.

- [ ] **Step 6: Document the new kinds in `docs/events.md`, including `MAX_OUTPUT`**

- [ ] **Step 7: Commit**

```bash
git add src/ricardi/events.py src/ricardi/policy.py docs/events.md tests/test_events.py tests/test_policy.py
git commit -m "feat(events): tool, refusal, diff and answerable kinds"
```

---

## Task 3: The sink, the recorder and the stamping stream

**Files:**
- Modify: `src/ricardi/events.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Consumes: Task 1–2's `Event`, `Envelope`, bodies.
- Produces: `EventSink` protocol with `async def emit(self, event: Event) -> None`;
  `Recorder` with `events: list[Event]` and that same `emit`;
  `Stream(name: str, sink: EventSink, clock: Callable[[], str] = now)` with
  `async def emit(self, actor: str, body: object, *, turn: str = "") -> Event`;
  `new_turn_id() -> str` (12 hex characters).

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_stream_stamps_sequence_and_time_and_name():
    stream = Stream("ricardi", recorder, clock=lambda: "2026-11-10T15:04:05Z")
    await stream.emit("agent", Said(text="one"), turn="t1")
    await stream.emit("agent", Said(text="two"), turn="t1")
    assert [e.envelope.seq for e in recorder.events] == [1, 2]
    assert {e.envelope.name for e in recorder.events} == {"ricardi"}

async def test_two_streams_for_two_names_do_not_share_a_sequence():
    # §5: a single global transcript on a box serving two names is a
    # cross-project leak, which is what 1:1 exists to prevent.
    await Stream("a", recorder).emit("agent", Said(text="x"))
    await Stream("b", recorder).emit("agent", Said(text="y"))
    assert [(e.envelope.name, e.envelope.seq) for e in recorder.events] == [("a", 1), ("b", 1)]

async def test_a_sink_that_raises_does_not_stop_the_stream():
    stream = Stream("ricardi", ExplodingSink())
    await stream.emit("agent", Said(text="one"))      # must not raise
    await stream.emit("agent", Said(text="two"))

async def test_a_sink_that_raises_is_logged_once_per_event(caplog):
    ...  # assert the failure is visible in the log, not silent
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_events.py -v -k "stream or sink"` → FAIL on `Stream`.

- [ ] **Step 3: Implement `EventSink`, `Recorder` and `Stream`**

`Stream.emit` builds the envelope, hands it to the sink inside a
`try/except Exception` that logs and returns the event anyway. The precedent is
`sdk.py:281`, where a failed announcement does not fail the tool: a transport
that cannot be written to is not a reason to abandon a turn the agent is
already running.

- [ ] **Step 4: Run them to verify they pass**

Run: `pytest tests/test_events.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ricardi/events.py tests/test_events.py
git commit -m "feat(events): a sink, a recorder and a stream that stamps"
```

---

## Task 4: The backend emits a turn

**Files:**
- Modify: `src/ricardi/backends/base.py` (`Caller`, ~line 26)
- Modify: `src/ricardi/backends/sdk.py` (`_one_turn`, 287–319)
- Test: `tests/test_sdk_backend.py`

**Interfaces:**
- Consumes: `Stream`, `Recorder`, and the bodies from Tasks 1–3.
- Produces: `Caller.events: Stream | None = None`; `_one_turn` unchanged in
  signature and return — it still returns `assemble(said)`.

- [ ] **Step 1: Write the failing tests**

Drive `_one_turn` with a fake client yielding the SDK's message shapes, as the
existing backend tests already do.

```python
async def test_a_turn_emits_text_tool_calls_and_an_outcome():
    kinds = [type(e.body).__name__ for e in recorder.events]
    assert kinds == ["TurnStarted", "ToolStarted", "ToolOutput",
                     "ToolFinished", "Said", "TurnEnded"]

async def test_a_tool_block_emits_the_summary_and_never_the_arguments():
    started = next(e for e in recorder.events if isinstance(e.body, ToolStarted))
    assert started.tool_use_id == "tu_1"
    assert "hunter2" not in json.dumps([to_wire(e) for e in recorder.events])

async def test_an_agent_that_says_nothing_still_opens_and_closes_the_turn():
    # Review Focus 4: an empty turn must be distinguishable from a lost one.
    assert [type(e.body).__name__ for e in recorder.events] == ["TurnStarted", "TurnEnded"]
    assert recorder.events[-1].body.outcome == "done"

async def test_a_turn_that_raises_still_closes_with_failed():
    # Review Focus 1.
    with pytest.raises(BackendError):
        await backend.ask("ricardi", "go", caller)
    assert recorder.events[-1].body.outcome == "failed"

async def test_a_turn_that_times_out_closes_with_timed_out():
    assert recorder.events[-1].body.outcome == "timed_out"

async def test_a_cut_short_turn_says_so_in_the_outcome():
    # ResultMessage.subtype in _CUT_SHORT (budget or step limit).
    assert recorder.events[-1].body.outcome == "cut_short"

async def test_a_turn_with_no_sink_behaves_exactly_as_before():
    assert await backend.ask("ricardi", "go", Caller()) == "hello"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_sdk_backend.py -v` → FAIL: `Caller` has no `events`.

- [ ] **Step 3: Add `Caller.events` and emit from `_one_turn`**

`_one_turn` keeps the `TextBlock` loop and `assemble`; it additionally emits.
`ToolUseBlock` → `tool_started(block.name, block.input, block.id)`;
`ToolResultBlock` → `tool_output(...)` then `ToolFinished(..., ok=not
block.is_error, ms=…)` timed from the matching `ToolStarted`. `TurnEnded` is
emitted in a `finally`, which is what makes Review Focus 1 and 4 hold: the only
paths out of a turn are a result, an exception and a timeout, and all three pass
through it. `ask` gains nothing new — the timeout and `BackendError` handling
stay exactly where they are.

- [ ] **Step 4: Run them to verify they pass**

Run: `pytest tests/test_sdk_backend.py -v` → PASS.

- [ ] **Step 5: Run the whole suite and the live suite**

Run: `pytest` → all pass.
Run: `RICARDI_LIVE=1 pytest -m live` → all pass. Mandatory for `sdk.py`; this
is the task where a real turn proves the emission order against the real SDK
rather than a fake.

- [ ] **Step 6: Commit**

```bash
git add src/ricardi/backends/base.py src/ricardi/backends/sdk.py tests/test_sdk_backend.py
git commit -m "feat(backends): a turn emits its tool calls, text and outcome"
```

---

## Task 5: A denied tool becomes visible

**Files:**
- Modify: `src/ricardi/backends/sdk.py` (`_gate`, 227–285)
- Test: `tests/test_sdk_gate.py`

**Interfaces:**
- Consumes: `Refused`, `ToolStarted`, `Stream`, `Decision.tier`.
- Produces: no new names. The gate's return value is unchanged.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_denied_tool_emits_a_refusal_carrying_the_rule_and_tier():
    # Today the denial reaches log.info and the model, and no transport ever
    # sees it (§2). The teaching case in pair-programming.md wants the rule.
    refused = recorder.events[-1].body
    assert (refused.rule, refused.tier, refused.tool) == ("tier", "read", "Edit")
    assert refused.reason == decision.reason

async def test_the_gate_still_denies_exactly_what_it_denied_before():
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == decision.reason

async def test_a_policy_that_errors_emits_a_refusal_and_still_denies():
    assert recorder.events[-1].body.rule == "policy error"

async def test_an_announced_tool_emits_rather_than_only_posting_a_notice():
    assert isinstance(recorder.events[-1].body, ToolStarted)

async def test_a_gate_with_no_sink_decides_identically():
    # The sink is observation. Removing it must not change one decision.
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_sdk_gate.py -v` → FAIL: no refusal recorded.

- [ ] **Step 3: Emit from the gate**

On deny, emit `Refused(rule=decision.rule, reason=decision.reason,
tier=decision.tier, tool=tool, target="")` before returning the unchanged
`decided(False, decision.reason)`. The `announce` obligation emits a
`ToolStarted` through the sink *in addition to* calling `caller.notice`, which
stays until Task 8 moves the channel onto events.

**What must not change:** every `return decided(...)` keeps its value and its
order. The gate is the one check that enforces everything; this task makes it
legible, not different.

- [ ] **Step 4: Run them to verify they pass**

Run: `pytest tests/test_sdk_gate.py -v` → PASS.

- [ ] **Step 5: Run the whole suite and the live suite**

Run: `pytest`, then `RICARDI_LIVE=1 pytest -m live` → all pass.

- [ ] **Step 6: Commit**

```bash
git add src/ricardi/backends/sdk.py tests/test_sdk_gate.py
git commit -m "feat(backends): a denied tool call reaches the stream, with its rule"
```

---

## Task 6: An inbound refusal stops being invisible

**Files:**
- Modify: `src/ricardi/app.py` (`_permit`, 273–302)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `Refused`, `Stream`.
- Produces: `_permit` returns `tuple[bool, Decision]` instead of `bool`; every
  call site (`app.py:109`, `:151`, `:173`, `:188`) updated.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_refused_request_emits_the_decision_not_just_the_reason():
    # _permit discards the Decision today, so rule and tier cannot reach a
    # transport (§2). The refused person still sees only the reason (§7).
    refused = recorder.events[-1].body
    assert refused.rule and refused.tier == ""
    assert refused.target == ""

async def test_a_refused_grant_carries_who_it_was_aimed_at():
    assert recorder.events[-1].body.target == "someone@example.com"

async def test_the_person_still_sees_the_reason_and_not_the_rule():
    assert transport.private[-1].text == decision.reason
    assert decision.rule not in transport.private[-1].text

async def test_a_policy_failure_emits_a_refusal_and_still_refuses():
    assert recorder.events[-1].body.rule == "policy error"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_app.py -v -k refus` → FAIL.

- [ ] **Step 3: Widen `_permit` and emit**

Return the `Decision` alongside the boolean so the caller can emit with the
`target` it passed in. Who sees the rule is the **renderer's** decision (§7):
the stream carries it, `_refuse` keeps sending the reason alone.

- [ ] **Step 4: Run them to verify they pass**

Run: `pytest tests/test_app.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ricardi/app.py tests/test_app.py
git commit -m "feat(app): an inbound refusal carries its decision to the stream"
```

---

## Task 7: Transports render events

**Files:**
- Create: `src/ricardi/render.py`
- Modify: `src/ricardi/transports/base.py`
- Modify: `src/ricardi/transports/slack.py` (`send` stays, as its own detail)
- Modify: `src/ricardi/transports/local.py`
- Test: `tests/test_render.py`, `tests/test_slack_transport.py`, `tests/test_local_transport.py`

**Interfaces:**
- Consumes: `Event` and every body.
- Produces: `render(event: Event) -> str` returning `""` for an event this
  renderer chooses not to show; `MAX_ANSWER: int = 3500`, the same value
  `sdk.py:32` holds today; `Transport.emit(self, event: Event) -> None` added to
  the protocol and `send` removed from it.

**Expect two copies of `MAX_ANSWER` for exactly one task.** This task adds it
here; Task 8 removes `sdk.py`'s, because the backend cannot stop truncating
until the app stops asking it for a string. A reviewer of this task should not
treat the duplication as the defect it would otherwise be.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_said_event_renders_as_the_agents_answer():
    assert render(event(Said(text="hello"), name="ricardi")) == "*ricardi*\nhello"

def test_a_long_answer_is_truncated_here_rather_than_in_the_backend():
    # The stream carries the whole thing; only this renderer has a 3500-char
    # channel to fit it into.
    out = render(event(Said(text="x" * 5000)))
    assert len(out) < 5000 and "truncated" in out

def test_a_tool_call_renders_as_one_line_and_never_its_arguments():
    assert render(event(ToolStarted("Bash", "Bash: ls -la", "tu_1"))) == "_ricardi_ `Bash: ls -la`"

def test_a_diff_degrades_to_a_sentence_rather_than_vanishing():
    # §13: slack.py degrades to text what it cannot show.
    assert "src/a.py" in render(event(Diffed("src/a.py", ("@@", "-a", "+b"))))

def test_the_kinds_this_renderer_stays_quiet_about_render_empty():
    for body in (Thought(text="hmm"), ToolOutput("tu_1", "ok", False)):
        assert render(event(body)) == ""

def test_every_kind_has_a_decided_rendering():
    # A kind added later must not fall through to a silent default.
    for kind in _BODIES:
        assert kind in RENDERERS

async def test_the_slack_transport_posts_what_render_returns():
    ...

async def test_an_event_rendering_to_nothing_posts_nothing():
    assert transport.posted == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_render.py -v` → FAIL: `ricardi.render` missing.

- [ ] **Step 3: Implement `render.py` and `emit` on both transports**

`RENDERERS: dict[str, Callable[[Event], str]]` keyed by kind, so a kind with no
entry is a failing test rather than silence. `SlackTransport.emit` renders and,
when the result is non-empty, posts through its own `send`. `LocalTransport`
keeps its `/replies` shape and appends the rendered line, so the stdlib-only
contract in its docstring still holds.

- [ ] **Step 4: Run them to verify they pass**

Run: `pytest tests/test_render.py tests/test_slack_transport.py tests/test_local_transport.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ricardi/render.py src/ricardi/transports tests/test_render.py tests/test_slack_transport.py tests/test_local_transport.py
git commit -m "feat(transports): render events, and degrade what Slack cannot show"
```

---

## Task 8: Ricardi speaks in events

**Files:**
- Modify: `src/ricardi/app.py` (`_say` 395–396, `_relay` 351–383, and every `_say` caller)
- Modify: `src/ricardi/backends/sdk.py` (drop `MAX_ANSWER`/`assemble` truncation, now Task 7's job)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `Stream`, `render`, `Transport.emit`, `Caller.events`.
- Produces: `Ricardi._say(inbound, text)` replaced by
  `Ricardi._emit(inbound, actor, body, turn="")`; one `Stream` per name, held
  on the app so `seq` is continuous across turns in that name.

- [ ] **Step 1: Write the failing tests**

```python
async def test_everything_ricardi_says_goes_out_as_an_event():
    assert all(isinstance(c, Event) for c in transport.emitted)

async def test_a_relayed_turn_shares_one_turn_id_end_to_end():
    ids = {e.envelope.turn for e in transport.emitted}
    assert len(ids) == 1 and ids != {""}

async def test_sequence_numbers_continue_across_two_turns_in_one_name():
    assert [e.envelope.seq for e in transport.emitted] == [1, 2, 3, 4]

async def test_the_help_text_still_reaches_slack_unchanged():
    assert router.HELP in "".join(render(e) for e in transport.emitted)

async def test_the_still_working_notice_is_an_event_too():
    assert any("Still working" in render(e) for e in transport.emitted)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_app.py -v` → FAIL.

- [ ] **Step 3: Route every reply through `_emit`**

A `dict[str, Stream]` keyed by name, created on demand, so §5's per-name
partition is a property of the app rather than a convention. `_relay` mints a
`turn` with `new_turn_id()`, passes `Caller(events=stream)` so the backend's
emissions share it, and emits its own `Said` for the final answer.

**The `assemble` change, stated because it is the one behaviour change a user
could notice:** `MAX_ANSWER` truncation leaves the backend and lives in the
Slack renderer (Task 7). The stream carries the whole answer; the channel still
sees at most 3500 characters. `assemble`'s joining of every block stays exactly
as it is — that part is not a ceiling, it is the fix for an agent that ends on
"Done."

- [ ] **Step 4: Run them to verify they pass**

Run: `pytest tests/test_app.py -v` → PASS.

- [ ] **Step 5: Run the whole suite and the live suite**

Run: `pytest` → all pass.
Run: `RICARDI_LIVE=1 pytest -m live` → all pass (`sdk.py` changed again).

- [ ] **Step 6: Update `docs/running.md` and `README.md` only where they describe what a turn prints**

Not the Slack framing — §13 decided the README and `comparison.md` stay as they
are until a browser client ships.

- [ ] **Step 7: Commit**

```bash
git add src/ricardi/app.py src/ricardi/backends/sdk.py docs/running.md tests/test_app.py
git commit -m "feat(app): every reply is an event, and Slack is one renderer of them"
```

---

## Done when

- `pytest` and `RICARDI_LIVE=1 pytest -m live` both pass.
- `ricardi check --strict` still passes: no policy behaviour changed.
- Nothing in `transports/` imports from `backends/` or vice versa.
- `docs/events.md` documents every kind in `_BODIES`, held by a test.
- A `Recorder` attached to one turn shows: `TurnStarted`, the tool calls with
  summaries only, any `Refused` with its rule, and exactly one `TurnEnded`.
