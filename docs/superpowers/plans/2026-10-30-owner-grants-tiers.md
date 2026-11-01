# Owner, Grants and Tiers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner of a name invite a person at a tier at or below their own, enforced inside `policy.decide` and recorded in the append-only grant store.

**Architecture:** Plane-2 verbs (`grant`, `revoke`, `reset`, `scope`) become new `Request.action` values at the existing decision point, so the vocabulary lands inert — an unmatched request already denies. `Request` and `Rule` gain the three dimensions a conferral needs (the asker's tier, the target person, the tier being conferred), because without them `grant bob read` and `grant bob exec` are the same request and "at or below your own tier" cannot be expressed in policy at all. Tiers are stored as verbs in the existing `grant_log` schema (`read`, `write` beside today's `ask`), so there is no migration.

**Tech Stack:** Python 3.11+, stdlib only (`sqlite3`, `dataclasses`, `argparse`), pytest with `asyncio_mode = "auto"`.

**Spec:** `docs/superpowers/specs/2026-10-28-shared-sessions-design.md` — §4 (the model), §6 (where configuration lives), §7 (verbs), §8 (tiers), §13 step 3 (build order), §15.5 (bootstrap), §15.6 (`reset`).

## Global Constraints

- **The mutable layer can only ever subtract from an immutable layer the process cannot write** (§6). Every new branch in `decide` must have no reachable `allow=True`.
- **`decide` is the only decision point.** A permission decided in application code is one the audit log cannot explain (§7, goal 2).
- **Fail closed.** A `GrantError` must propagate, never be folded into a refusal reason — `audit.py` records `rule="policy error"` with the text and re-raises.
- **Append-only.** Never `UPDATE` or `DELETE` a grant row; insert a superseding row (§6).
- **No cache.** The decision point reads the store per call (§6).
- **Three person-tiers, not four: `none`, `read`, `write`** (§8). `exec` is the *name's* scope, not a person tier — §8 explicitly corrects §7's four-verb table. Tier ordering is total: `none < read < write`.
- **`read` is enforced by tool name, never by a command string.** A command allowlist bounds the string, never the behaviour (§8, H4/H5 proven).
- **The read tier gets shell and is not airtight.** A reader cannot write directly and can cause a write indirectly through the shell. This is documented, not engineered around (§8, "The decision").
- **Admin writes the owner into `policy.toml`. The DB never bootstraps itself, and there is no self-service path to becoming an owner** (§15.5).
- **No new dependencies.** `pyproject.toml` ships with `dependencies = []`.
- **Tests must pass under `uv run pytest`, bare `pytest`, and `python -m pytest`.** These differ in whether the working directory is on `sys.path`; a cross-test import that works under one and fails under another has already cost this project a red CI run.
- `policy.toml` stays mounted `:ro`; `agents.toml` is read once at startup and never re-read, so name registration stays host-side (§7).

## Review Focus

1. **A `grant` naming someone not on the roster.** The target resolves to no email, so a row would be written for a person no decision can ever match. Expected: refused, nothing written. *(Task 5)*
2. **Case and whitespace in the target or the name.** The store normalises what it writes; an authority check that compares raw strings passes on a different key than the row it then writes, which is how "I granted it and they are still refused" happens. Expected: both sides normalised identically. *(Task 5)*
3. **Granting yourself a higher tier.** The asker is also the target. Expected: refused as a widening, by the same rule that bounds any delegate. *(Task 5)*
4. **An owner of one name granting on another name.** Ownership is per name (§4, N:1). Expected: refused. *(Task 5)*
5. **A tier value that is not a tier (`admin`, `exec`, `""`).** An unrecognised conferral must refuse rather than fall through to a rule that does not name `confers`. Expected: refused. *(Task 1 for the vocabulary, Task 5 for the decision)*

---

### Task 1: The vocabulary and the data model

**Files:**
- Modify: `src/ricardi/policy.py` — `Request`, `Rule`, `Rule.matches`, `load`
- Test: `tests/test_policy_tiers.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Request.tier: str`, `Request.target: str`, `Request.confers: str`; `Rule.tiers: tuple[str, ...]`, `Rule.targets: tuple[str, ...]`, `Rule.confers: tuple[str, ...]`; `TIERS: tuple[str, ...] = ("read", "write")`; `PLANE2_VERBS: frozenset[str]`; `tier_rank(tier: str) -> int`; `at_most(tier: str, ceiling: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
def test_tier_ordering_is_total_and_absence_is_the_floor():
    assert tier_rank("") == 0 and tier_rank("read") == 1 and tier_rank("write") == 2
    assert at_most("read", "write") and at_most("read", "read")
    assert not at_most("write", "read")

def test_an_unknown_tier_ranks_below_everything_rather_than_above():
    # "admin" must not out-rank write by sorting later; it is not a tier.
    assert tier_rank("admin") == 0
    assert not at_most("admin", "write")

def test_a_rule_naming_confers_distinguishes_one_conferral_from_another():
    rule = Rule(effect="allow", actions=("grant",), confers=("read",))
    assert rule.matches(Request(action="grant", confers="read", principal=KNOWN))
    assert not rule.matches(Request(action="grant", confers="write", principal=KNOWN))

def test_a_rule_naming_no_new_dimension_still_matches_every_request():
    # The existing semantics: every dimension a rule names must match, and the
    # ones it does not name match all. A rule written before tiers existed must
    # not stop matching now.
    rule = Rule(effect="allow", actions=("ask",))
    assert rule.matches(Request(action="ask", tier="write", principal=KNOWN))

def test_tiers_and_targets_match_like_users_and_confers_like_channels():
    assert Rule(effect="allow", tiers=("write",)).matches(Request(action="tool", tier="write", principal=KNOWN))
    assert Rule(effect="allow", targets=("*@example.com",)).matches(Request(action="grant", target="bob@example.com", principal=KNOWN))
    assert not Rule(effect="allow", confers=("rea*",)).matches(Request(action="grant", confers="read", principal=KNOWN))

def test_the_new_rule_keys_load_from_toml():
    policy = load(written_toml('[[rule]]\neffect="allow"\nactions=["grant"]\nconfers=["read"]\ntargets=["*@example.com"]\ntiers=["write"]\n'))
    assert policy.rules[0].confers == ("read",)
    assert policy.rules[0].targets == ("*@example.com",)
    assert policy.rules[0].tiers == ("write",)

def test_an_undesigned_plane_two_verb_denies_by_default():
    policy = load(written_toml(ONE_ASK_ALLOW))
    assert (await policy.decide(Request(action="scope", principal=KNOWN, agent="n1"))).allow is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_policy_tiers.py -v`
Expected: FAIL on `ImportError` for `tier_rank` / `at_most`, and on unexpected keyword arguments to `Request` and `Rule`.

- [ ] **Step 3: Add the fields**

`Request` gains `tier: str = ""`, `target: str = ""`, `confers: str = ""`, each documented in the style of the existing `flags` comment. `tier` is filled in by `decide` and never by a caller — say so in the comment, because a caller-supplied tier is a widening path.

`Rule` gains `tiers`, `targets`, `confers`, all `tuple[str, ...] = ()`.

- [ ] **Step 4: Extend `Rule.matches`**

`tiers` and `targets` glob like `users` (via `_any_glob`); `confers` is exact membership like `channels`, because a glob on a conferral is how `rea*` silently covers a tier nobody meant. Keep the existing shape: every dimension the rule names must match, unnamed ones match all.

- [ ] **Step 5: Add `TIERS`, `PLANE2_VERBS`, `tier_rank`, `at_most`**

`TIERS = ("read", "write")` in ascending order; `PLANE2_VERBS = frozenset({"grant", "revoke", "reset", "scope"})`. `tier_rank` returns `TIERS.index(tier) + 1` for a known tier and `0` for anything else — an unrecognised tier must rank below every real one, not sort after them.

- [ ] **Step 6: Parse the new keys in `load`**

Alongside the existing `_tuple(entry.get(...))` calls.

- [ ] **Step 7: Run the whole suite three ways**

Run: `uv run pytest -q` then `python -m pytest -q` then `.venv/Scripts/pytest.exe -q` (bare)
Expected: PASS in all three, with no pre-existing test modified.

- [ ] **Step 8: Commit**

```bash
git add src/ricardi/policy.py tests/test_policy_tiers.py
git commit -m "feat(policy): the plane-2 vocabulary and the three dimensions a conferral needs"
```

---

### Task 2: The owner node in policy.toml

**Files:**
- Modify: `src/ricardi/policy.py` — `RulesPolicy.__init__`, `load`
- Modify: `policy.example.toml`
- Test: `tests/test_policy_owners.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `RulesPolicy.owners: dict[str, tuple[str, ...]]` (agent name → owner emails, both normalised); `RulesPolicy.owns(email: str, agent: str) -> bool`.

The file shape, which the spec fixes as a reviewed-file concern (§6, §15.5):

```toml
[[owner]]
agent = "ricardi"
email = "ghnalbandian@gmail.com"
```

- [ ] **Step 1: Write the failing tests**

```python
def test_an_owner_entry_binds_one_person_to_one_name():
    p = load(written_toml(ONE_ASK_ALLOW + '\n[[owner]]\nagent="n1"\nemail="Owner@Example.COM"\n'))
    assert p.owns("owner@example.com", "n1") is True
    assert p.owns("owner@example.com", "n2") is False

def test_ownership_is_normalised_on_both_sides():
    p = load(written_toml(ONE_ASK_ALLOW + '\n[[owner]]\nagent="N1"\nemail=" owner@example.com "\n'))
    assert p.owns("OWNER@EXAMPLE.COM", "n1") is True

def test_two_owners_of_one_name_are_both_owners():
    ...  # two [[owner]] entries, same agent, different emails

def test_an_owner_entry_missing_a_key_is_a_policy_error():
    for toml in ('[[owner]]\nagent="n1"\n', '[[owner]]\nemail="a@b.com"\n'):
        with pytest.raises(PolicyError, match="owner"):
            load(written_toml(ONE_ASK_ALLOW + "\n" + toml))

def test_a_policy_with_no_owner_entries_still_loads():
    assert load(written_toml(ONE_ASK_ALLOW)).owners == {}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_policy_owners.py -v`
Expected: FAIL with `AttributeError: 'RulesPolicy' object has no attribute 'owns'`.

- [ ] **Step 3: Parse `[[owner]]` in `load` and implement `owns`**

Normalise with `grants.normalise` for the email and `grants.normalise_name` for the agent, so ownership is keyed exactly as the rows are. A missing or blank key raises `PolicyError` naming the entry, in the style of the existing rule validation. No `tier` field on the entry: an owner's conferrable maximum is the name's ceiling, which the rules already express (ruling recorded below).

- [ ] **Step 4: Document the entry in `policy.example.toml`**

With the two sentences the spec fixes: admin writes the owner, and there is no self-service path to becoming one.

- [ ] **Step 5: Run the suite three ways, then commit**

```bash
git add src/ricardi/policy.py policy.example.toml tests/test_policy_owners.py
git commit -m "feat(policy): the owner node, which the model was missing"
```

---

### Task 3: The asker's tier, read once and put on the request

**Files:**
- Modify: `src/ricardi/grants.py` — `GrantStore`
- Modify: `src/ricardi/policy.py` — `RulesPolicy.decide`
- Test: `tests/test_grants_tiers.py` (create)

**Interfaces:**
- Consumes: `Request.tier` (Task 1).
- Produces: `GrantStore.tier(person: str, name: str) -> str` — `"write"`, `"read"`, or `""`; `decide` fills `request.tier` before any rule is matched.

Tiers are rows in the existing schema: verb `"read"` or `"write"` beside today's `"ask"`. No migration, no new table.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_tier_is_the_highest_verb_held(tmp_path):
    s = GrantStore.open(tmp_path / "g.db")
    s.add(person="a@b.com", name="n1", verb="read", by="owner")
    assert s.tier("a@b.com", "n1") == "read"
    s.add(person="a@b.com", name="n1", verb="write", by="owner")
    assert s.tier("a@b.com", "n1") == "write"

def test_a_revoked_write_falls_back_to_read_not_to_nothing(tmp_path):
    # Both rows exist; the write is superseded. Append-only means the read
    # grant is still in force, and a tier that fell to "" would silently
    # revoke more than the admin asked for.
    ...
    assert s.tier("a@b.com", "n1") == "read"

def test_no_rows_is_no_tier(tmp_path):
    assert GrantStore.open(tmp_path / "g.db").tier("a@b.com", "n1") == ""

def test_the_tier_is_per_name(tmp_path):
    ...  # write on n1 does not confer write on n2

def test_decide_puts_the_askers_tier_on_the_request(tmp_path):
    seen = []
    policy = load(written_toml(ONE_ASK_ALLOW))
    policy.grants = store_with(("a@b.com", "n1", "write"))
    policy.rules = (Recorder(seen),) + tuple(policy.rules)   # a rule that records what it was matched against
    await policy.decide(Request(action="ask", principal=KNOWN_A, agent="n1", resource=CH))
    assert seen[0].tier == "write"

def test_a_corrupt_store_raises_rather_than_reporting_no_tier(tmp_path):
    # Fail closed: a GrantError must reach audit.py, which records the text.
    with pytest.raises(GrantError):
        await policy_with_corrupt_store().decide(Request(action="ask", principal=KNOWN_A, agent="n1"))

async def test_the_tier_is_never_written_to_the_thread():
    # §8's correction: thread flags are sticky and union-only, so a per-person
    # restriction stored on the thread would leave it narrowed for the *next*
    # person -- one reader poisoning the channel for everyone. The tier is
    # computed per request from the principal and stored nowhere.
    before = bindings.snapshot()
    await app.handle(inbound("tell n1 hello", user=READER))
    assert bindings.snapshot() == before
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_grants_tiers.py -v`
Expected: FAIL with `AttributeError: 'GrantStore' object has no attribute 'tier'`.

- [ ] **Step 3: Implement `GrantStore.tier`**

Highest held of `TIERS` in descending order, via the existing `holds`. Reuse `holds` rather than a new query: it already handles normalisation and the latest-row-wins semantics, and two code paths deciding "is this in force" is how they drift apart.

- [ ] **Step 4: Fill `request.tier` at the top of `decide`**

Before `_flag_refusal`, so every later branch and every rule sees it; `dataclasses.replace`, since `Request` is frozen. Only when `self.grants is not None` and `request.principal.known` — an unknown principal has no tier to look up and is refused below anyway. One SQLite read per decision, which §6 fixes as the design ("no cache").

- [ ] **Step 5: Run the suite three ways, then commit**

```bash
git add src/ricardi/grants.py src/ricardi/policy.py tests/test_grants_tiers.py
git commit -m "feat(grants): the asker's tier, read at the decision point"
```

---

### Task 4: `read` denies the writing tools

**Files:**
- Modify: `src/ricardi/policy.py` — new `WRITING_TOOLS`, `_tier_refusal`, called from `decide`
- Modify: `src/ricardi/grants.py` — `seed_from_policy`
- Modify: `docs/access-simulation.md`
- Test: `tests/test_policy_tiers.py` (extend), `tests/test_grants_seed.py` (extend)

**Interfaces:**
- Consumes: `Request.tier` (Task 3).
- Produces: `WRITING_TOOLS: frozenset[str]`; `_tier_refusal(request) -> Decision | None`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_reader_may_not_edit_or_write():
    for tool in ("Write", "Edit", "NotebookEdit"):
        d = await policy.decide(Request(action="tool", principal=KNOWN_A, agent="n1", tier="read", tool=tool, tool_input={"file_path": "/workspace/n1/a.py"}))
        assert d.allow is False
        assert "read" in d.reason
        assert d.rule == "tier"

async def test_a_reader_still_gets_the_reading_tools_and_the_shell():
    for tool in ("Read", "Grep", "Glob", "Bash"):
        assert (await policy.decide(tool_request(tool, tier="read"))).allow is True

async def test_a_writer_may_write():
    assert (await policy.decide(tool_request("Write", tier="write"))).allow is True

async def test_no_tier_row_leaves_the_rules_to_decide():
    # Backward compatibility, and deliberate: absence of a tier row means
    # "unconstrained by tier", not "none". Enforcing none belongs with the
    # channel-to-name binding, where an invitee without a tier exists.
    assert (await policy.decide(tool_request("Write", tier=""))).allow is True

async def test_the_tier_refusal_cannot_allow_anything():
    # Structural, like _flag_refusal: there is no allow=True to reach.
    assert "allow=True" not in inspect.getsource(_tier_refusal)

def test_the_seeder_writes_the_tier_the_rules_imply(tmp_path):
    # §15.5: seeded at the tier their existing rules imply. A roster member
    # whose rules permit Write seeds "write"; otherwise "read".
    ...
    assert store.tier("a@b.com", "n1") == "write"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_policy_tiers.py -k tier -v`
Expected: FAIL — a reader's `Write` is allowed.

- [ ] **Step 3: Add `WRITING_TOOLS` and `_tier_refusal`**

`WRITING_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})`. `NotebookEdit` is included although §8's table names only `Write` and `Edit`: `READING_TOOLS` already includes `NotebookRead`, so leaving its counterpart out is an oversight rather than a decision, and including it only ever subtracts. Record it as a builder's call in the docstring.

`_tier_refusal` refuses when `request.action == "tool"`, `request.tier == "read"`, and `request.tool in WRITING_TOOLS`. Refuse-only, with `rule="tier"` so the audit line names it.

- [ ] **Step 4: Call it from `decide`, beside `_flag_refusal`**

After the tier is filled (Task 3) and before the rules, for the reason the flags comment already gives: a branch placed before the rules with no reachable `allow=True` is what makes the invariant structural rather than remembered.

- [ ] **Step 5: Extend `seed_from_policy` to write the implied tier**

For each row it already writes, also write `"write"` when the matched allow rule's `tools` reach a writing tool or name no tools at all, and `"read"` otherwise. Returns the same count semantics (rows written). Idempotency unchanged: any history for a triple is left alone.

- [ ] **Step 6: Document the caveat in `docs/access-simulation.md`**

That the read tier gets shell, that a reader can cause a write indirectly, and that the alternative for an adversarial case is a separate name over a read-only mount. The spec is explicit that this is documented rather than engineered around.

- [ ] **Step 7: Run the suite three ways, then commit**

```bash
git add src/ricardi/policy.py src/ricardi/grants.py docs/access-simulation.md tests/test_policy_tiers.py tests/test_grants_seed.py
git commit -m "feat(policy): the read tier, enforced by tool name"
```

---

### Task 5: The escalation check for `grant` and `revoke`

**Files:**
- Modify: `src/ricardi/policy.py` — `_conferral_refusal`, called from `decide`
- Test: `tests/test_policy_conferral.py` (create)

**Interfaces:**
- Consumes: `RulesPolicy.owns` (Task 2), `Request.tier` (Task 3), `at_most`/`TIERS` (Task 1).
- Produces: `_conferral_refusal(policy, request) -> Decision | None`.

The authority, in one sentence: **an owner of that name may confer any tier the name's ceiling rules permit; a delegate holding the `grant` verb may confer at or below their own tier; nobody else may confer at all.**

- [ ] **Step 1: Write the failing tests**

```python
async def test_an_owner_may_confer_write():
    assert (await policy.decide(grant_request(by=OWNER, target="bob@example.com", confers="write", agent="n1"))).allow is True

async def test_a_delegate_may_confer_at_or_below_their_own_tier():
    # holds ("grant") and tier read
    assert (await policy.decide(grant_request(by=DELEGATE_READ, target="bob@example.com", confers="read"))).allow is True
    d = await policy.decide(grant_request(by=DELEGATE_READ, target="bob@example.com", confers="write"))
    assert d.allow is False and d.rule == "above the grantor"

async def test_someone_with_neither_ownership_nor_the_grant_verb_may_not_confer():
    d = await policy.decide(grant_request(by=KNOWN_A, target="bob@example.com", confers="read"))
    assert d.allow is False and d.rule == "not a grantor"

async def test_an_owner_of_one_name_may_not_confer_on_another():
    d = await policy.decide(grant_request(by=OWNER_OF_N1, agent="n2", target="bob@example.com", confers="read"))
    assert d.allow is False and d.rule == "not a grantor"

async def test_nobody_may_raise_their_own_tier():
    d = await policy.decide(grant_request(by=DELEGATE_READ, target=DELEGATE_READ.email, confers="write"))
    assert d.allow is False

async def test_a_target_who_is_not_on_the_roster_is_refused():
    d = await policy.decide(grant_request(by=OWNER, target="stranger@example.com", confers="read"))
    assert d.allow is False and d.rule == "target not on the roster"

async def test_the_target_and_the_name_are_normalised_before_the_check():
    assert (await policy.decide(grant_request(by=OWNER, target=" BOB@Example.com ", confers="read", agent="N1"))).allow is True

async def test_a_conferral_that_is_not_a_tier_is_refused():
    for value in ("admin", "exec", "", "READ "):
        assert (await policy.decide(grant_request(by=OWNER, target="bob@example.com", confers=value))).allow is False

async def test_revoke_is_not_bounded_by_the_grantors_tier():
    # Revoking only ever subtracts, so a reader may revoke a writer. The
    # authority to act on the name is still required.
    assert (await policy.decide(revoke_request(by=DELEGATE_READ, target="bob@example.com"))).allow is True

async def test_the_ceiling_still_decides_after_the_authority_check():
    # No rule names action="grant" at all -> refused by the default, with the
    # authority check having allowed nothing on its own.
    assert (await policy.decide(grant_request(by=OWNER, target="bob@example.com", confers="read", policy=no_grant_rule_policy))).allow is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_policy_conferral.py -v`
Expected: FAIL — conferrals are decided by the rules alone, so an owner's `grant` and a stranger's `grant` are indistinguishable.

- [ ] **Step 3: Implement `_conferral_refusal`**

Refuse-only, for `request.action in ("grant", "revoke")`, in this order so each refusal is audited as itself: target not on the roster → `rule="target not on the roster"`; not an owner and not a holder of the `grant` verb on that name → `rule="not a grantor"`; for `grant` only, an unrecognised conferral or one above the grantor's own tier where the grantor is a delegate → `rule="above the grantor"`. Normalise the target and the name with `grants.normalise` / `normalise_name` before every comparison, including the roster lookup.

- [ ] **Step 4: Call it from `decide`, after the roster refusal and before the allow pass**

Same placement argument as the existing grant check: a named deny must be recorded as that rule, and the branch must sit before the allows so it can only ever add a requirement.

- [ ] **Step 5: Run the suite three ways, then commit**

```bash
git add src/ricardi/policy.py tests/test_policy_conferral.py
git commit -m "feat(policy): who may confer what, bounded by the ceiling and the grantor"
```

---

### Task 6: `grant` and `revoke` from Slack

**Files:**
- Modify: `src/ricardi/app.py` — command parsing, a `_confer` handler, the store on the app
- Modify: `src/ricardi/cli.py` — pass the store to the app
- Modify: `docs/running.md` — the two commands and the help text
- Test: `tests/test_app_conferral.py` (create)

**Interfaces:**
- Consumes: everything above; `GrantStore.add` (existing).
- Produces: the Slack surface. `App.__init__` gains `grants: GrantStore | None = None`.

Command shape, matching the existing `tell <agent> <message>` style:

```
grant @bob read on ricardi
revoke @bob on ricardi
```

- [ ] **Step 1: Write the failing tests**

```python
async def test_grant_writes_a_row_and_confirms_in_the_channel():
    await app.handle(inbound("grant <@U2> read on n1", user="U1"))
    assert store.tier("bob@example.com", "n1") == "read"
    assert "read" in said() and "n1" in said()

async def test_a_refused_grant_writes_nothing():
    await app.handle(inbound("grant <@U2> write on n1", user="U3"))
    assert store.history() == ()

async def test_revoke_supersedes_rather_than_deleting():
    ...
    assert len(store.history()) == 2 and store.tier("bob@example.com", "n1") == ""

async def test_an_unparseable_grant_explains_the_shape():
    await app.handle(inbound("grant bob everything", user="U1"))
    assert "grant @person" in said()

async def test_a_slack_id_that_is_not_on_the_roster_is_named_as_the_problem():
    await app.handle(inbound("grant <@U9> read on n1", user="U1"))
    assert "roster" in said() and store.history() == ()

async def test_the_decision_is_audited_whether_or_not_it_allowed():
    # Both paths go through policy.decide, so AuditedPolicy sees them.
    ...

async def test_a_store_error_refuses_rather_than_claiming_success():
    # GrantError on add -> the channel is told it failed.
    assert "couldn't" in said().lower()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_app_conferral.py -v`
Expected: FAIL — the command is unrecognised and falls through to "I don't know who that's for".

- [ ] **Step 3: Parse the two commands**

`grant <@USERID> <tier> on <name>` and `revoke <@USERID> on <name>`. Resolve the Slack id to an email through the roster the same way `_principal` does. On a shape that does not parse, reply with the shape rather than a generic refusal.

- [ ] **Step 4: Decide, then write**

Build `Request(action="grant"|"revoke", principal=..., resource=..., agent=<name>, target=<email>, confers=<tier or "">)`, pass it through `self.policy.decide` via the existing `_permit` path so the audit line and the fail-closed behaviour come for free, and only then call `store.add(..., granted=(action == "grant"))`. A `GrantError` is reported to the channel as a failure; it must never read as success.

- [ ] **Step 5: Plumb the store from `cli._serve` into the app**

It is already opened there for the policy; pass the same instance, and keep the local reference that the existing `finally` closes.

- [ ] **Step 6: Document both commands in `docs/running.md`**

Two sentences this must carry. First: a `grant` records a tier and does **not** add anyone to the channel, so the owner adds the person in Slack first — presence and capability are separate on purpose (§4), and a grant that looks like it did nothing is otherwise indistinguishable from a broken one. Second: once a person is in the channel without a grant, they have presence and nothing more.

`reset` and `scope` are vocabulary only in this plan — recognised as actions, denied by default, wired later — so §15.6's "the agent has forgotten, not the record is gone" note goes in with `reset`, not here.

- [ ] **Step 7: Run the suite three ways, then commit**

```bash
git add src/ricardi/app.py src/ricardi/cli.py docs/running.md tests/test_app_conferral.py
git commit -m "feat(app): invite and disinvite a person from the channel"
```

---

## Rulings recorded here, so a reviewer can disagree with them deliberately

- **Three tiers, not four.** §8 explicitly corrects §7's table: `exec` is the name's scope, not a person tier. The grantable vocabulary is `read` and `write`, and absence is `none`.
- **Tiers are verbs in the existing schema.** `read` and `write` rows beside today's `ask`, so there is no migration and `holds` keeps being the one place that decides what is in force.
- **Absence of a tier row means "unconstrained by tier", not "none".** Enforcing `none` requires an invitee who has presence without a tier, which arrives with the channel-to-name binding in step 4. Treating absence as `none` today would refuse every tool call for everyone the step-2 seed wrote, which is a flag day on the live deployment.
- **An owner's conferrable maximum is the name's ceiling, not a field.** No `tier` key on `[[owner]]`: the rules already express what the name permits, and a second place to say it is a second place to get it wrong. A delegate is bounded by their own tier instead.
- **`NotebookEdit` joins `Write` and `Edit`.** §8 names two; `READING_TOOLS` already names `NotebookRead`, so excluding its counterpart is an oversight. It only ever subtracts.
- **`revoke` is not bounded by the grantor's tier.** It only subtracts. Authority over the name is still required.
- **`reset` and `scope` land as vocabulary only.** Recognised, denied by default, no flag day when they are wired. `scope` needs per-name scope storage that overlaps step 4, and `reset` needs the session-id semantics of §15.6.
- **R7 — "an owner must not be able to widen their own name" — is satisfied by absence here, not by a check.** Nothing in this plan can change a name's roots: `agents.toml` is read once at startup and `path_under` lives in the reviewed file. The check becomes necessary in the task that wires `scope`, and that task owns it.
- **`grant` records a tier; it does not add anyone to the Slack channel.** §4 says an owner inviting does both in one act, and this plan delivers only the second half. The Slack `conversations.invite` scope change is step 5's (§11, "Slack scopes"), so until then the owner adds the person to the channel by hand and then grants. Worth saying in `docs/running.md`, because a `grant` that appears to do nothing is otherwise indistinguishable from a broken one: someone added by anyone else gets presence and nothing more, which is exactly what keeps H1 closed.
