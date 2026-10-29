# Grant Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move "who may address which name" out of `policy.toml` into an append-only SQLite store in `state/`, seeded from the current roster so landing it is not a flag day.

**Architecture:** A new `src/ricardi/grants.py` owns a single-table, insert-only SQLite database. `RulesPolicy` consults it as a further *subtraction* after the deny pass and the `principal.known` check, so a missing grant can only ever refuse — never widen what `policy.toml` already allowed. A seed command writes the live roster in as rows before the first deploy that enforces them.

**Tech Stack:** Python 3.11+, `sqlite3` from the standard library. No new dependency, no service, no port, no credential.

**Spec:** [`docs/superpowers/specs/2026-10-28-shared-sessions-design.md`](../specs/2026-10-28-shared-sessions-design.md) — §6 (where configuration lives), §13 step 2 (build order), §15.4 (seed migration), §15.5 (bootstrap).

## Global Constraints

- **Append-only.** Never `UPDATE` or `DELETE` a grant row; insert a superseding row. History *is* the storage model.
- **Fail closed on corruption**, unlike the rest of `state/`. `ThreadBindings` and `SessionIds` deliberately shrug off a corrupt file; a corrupt grant store must refuse to start, as `PolicyError` does.
- **No cache.** In-process SQLite is microseconds; the decision point reads it per call.
- **Absence is not corruption.** No row means no access (§15.4).
- **Subtract only.** A grant is intersected with the ceiling. There is no code path where a grant row produces `allow=True` that `policy.toml` did not already allow.
- **The DB never bootstraps itself** (§15.5). The first owner is written into `policy.toml` by admin; there is no self-service path to becoming one.
- Stdlib only. Standard library `sqlite3`, no ORM.
- Emails are the person key, **lowercased on write and on read**, because the roster and a human typing a grant will disagree on case.

## Review Focus

Five things the spec implies that no task's happy path exercises. Each has a test added to the task that owns the code.

1. **A grant row for an email not in the roster** — inert, not a crash. `principal.known` is already false for them; the grant must not resurrect them. (Task 4)
2. **Ordering by insertion, not by timestamp** — a superseding row written during clock skew must still win. Order by `rowid`, never by `at`. (Task 1)
3. **A missing database file versus an empty one** — missing is created; present-but-empty is a valid store with no grants, which denies everything rather than failing. (Task 1)
4. **A second writer while Ricardi is running** — the `grants` CLI runs beside a live container against the same file. Must not raise `database is locked` at the decision point. (Task 1)
5. **Case and whitespace in emails** — `Gevik@Example.com ` and `gevik@example.com` are the same person, or the seed silently grants nobody. (Task 1)

---

### Task 1: The store

**Files:**
- Create: `src/ricardi/grants.py`
- Test: `tests/test_grants.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class GrantError(Exception)`
  - `@dataclass(frozen=True) class Grant` with fields `person: str`, `name: str`, `verb: str`, `granted: bool`, `by: str`, `at: str`
  - `class GrantStore` with `GrantStore.open(path: str | os.PathLike[str]) -> GrantStore`, `.add(*, person: str, name: str, verb: str, by: str, granted: bool = True, at: str = "") -> None` (empty `at` means UTC now, and the parameter exists so the skew test can pin it), `.holds(person: str, name: str, verb: str) -> bool`, `.current() -> tuple[Grant, ...]`, `.history() -> tuple[Grant, ...]`, `.close() -> None`
  - `def normalise(email: str) -> str` — strip and lowercase

- [ ] **Step 1: Write the failing tests**

```python
def test_a_grant_is_held_once_added(tmp_path):
    s = GrantStore.open(tmp_path / "grants.db")
    s.add(person="a@b.com", name="shared", verb="ask", by="owner@b.com")
    assert s.holds("a@b.com", "shared", "ask") is True

def test_nothing_is_held_in_an_empty_store(tmp_path):
    s = GrantStore.open(tmp_path / "grants.db")
    assert s.holds("a@b.com", "shared", "ask") is False

def test_a_revocation_supersedes_without_deleting(tmp_path):
    s = GrantStore.open(tmp_path / "grants.db")
    s.add(person="a@b.com", name="shared", verb="ask", by="o@b.com")
    s.add(person="a@b.com", name="shared", verb="ask", by="o@b.com", granted=False)
    assert s.holds("a@b.com", "shared", "ask") is False
    assert len(s.history()) == 2          # both rows survive
    assert s.current() == ()              # nothing currently granted

def test_the_latest_row_wins_by_insertion_not_by_timestamp(tmp_path):
    # Review Focus 2: clock skew must not resurrect a revoked grant.
    s = GrantStore.open(tmp_path / "grants.db")
    s.add(person="a@b.com", name="shared", verb="ask", by="o@b.com", at="2099-01-01T00:00:00Z")
    s.add(person="a@b.com", name="shared", verb="ask", by="o@b.com", granted=False, at="2000-01-01T00:00:00Z")
    assert s.holds("a@b.com", "shared", "ask") is False

def test_email_case_and_whitespace_do_not_make_a_second_person(tmp_path):
    # Review Focus 5.
    s = GrantStore.open(tmp_path / "grants.db")
    s.add(person="  Gevik@Example.COM ", name="shared", verb="ask", by="o@b.com")
    assert s.holds("gevik@example.com", "shared", "ask") is True

def test_a_missing_file_is_created_and_an_empty_one_is_valid(tmp_path):
    # Review Focus 3.
    path = tmp_path / "nested" / "grants.db"
    s = GrantStore.open(path)
    assert path.exists()
    assert s.current() == ()

def test_a_corrupt_store_refuses_to_open(tmp_path):
    path = tmp_path / "grants.db"
    path.write_bytes(b"this is not a database")
    with pytest.raises(GrantError):
        GrantStore.open(path)

def test_a_second_connection_can_read_while_another_holds_one(tmp_path):
    # Review Focus 4: the CLI runs beside a live container.
    path = tmp_path / "grants.db"
    a = GrantStore.open(path)
    a.add(person="a@b.com", name="shared", verb="ask", by="o@b.com")
    b = GrantStore.open(path)
    assert b.holds("a@b.com", "shared", "ask") is True
```

`add` takes `at` as a keyword with a UTC-now default so the skew test can pin it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_grants.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ricardi.grants'`

- [ ] **Step 3: Implement `grants.py`**

Schema, created on open if absent:

```sql
CREATE TABLE IF NOT EXISTS grant_log (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  person  TEXT NOT NULL,
  name    TEXT NOT NULL,
  verb    TEXT NOT NULL,
  granted INTEGER NOT NULL,
  by      TEXT NOT NULL,
  at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS grant_lookup ON grant_log (person, name, verb, id);
```

`holds` selects the single highest `id` for the triple and returns its `granted`. Open with `check_same_thread=False` and set `journal_mode=WAL` so a reader and a writer coexist (Review Focus 4). Wrap `sqlite3.DatabaseError` from open and from the schema statement in `GrantError` (fail closed); let a genuinely absent file be created, which `sqlite3.connect` does.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_grants.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add src/ricardi/grants.py tests/test_grants.py
git commit -m "feat(grants): an append-only SQLite grant store that fails closed"
```

---

### Task 2: Seeding from the live roster

**Files:**
- Modify: `src/ricardi/grants.py`
- Test: `tests/test_grants_seed.py`

**Interfaces:**
- Consumes: `GrantStore` from Task 1; `RulesPolicy` (`.roster: dict[str, str]`, `.rules: tuple[Rule, ...]`) from `ricardi.policy`.
- Produces: `def seed_from_policy(store: GrantStore, policy: RulesPolicy, *, by: str) -> int` returning the number of rows written.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_roster_member_is_granted_ask_on_the_names_their_rules_allow(tmp_path):
    policy = RulesPolicy(
        rules=[Rule(name="r", effect="allow", actions=("ask",), agents=("shared",))],
        roster={"U1": "a@b.com", "U2": "c@d.com"},
    )
    s = GrantStore.open(tmp_path / "g.db")
    assert seed_from_policy(s, policy, by="admin@b.com") == 2
    assert s.holds("a@b.com", "shared", "ask") is True
    assert s.holds("c@d.com", "shared", "ask") is True

def test_seeding_is_idempotent(tmp_path):
    # Running the deploy step twice must not double the history or change holds.
    ...
    assert seed_from_policy(s, policy, by="admin@b.com") == 0
    assert len(s.history()) == 2

def test_a_name_no_rule_allows_is_not_granted(tmp_path):
    ...

def test_seeding_an_empty_roster_writes_nothing_and_says_so(tmp_path):
    assert seed_from_policy(s, RulesPolicy(), by="admin@b.com") == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_grants_seed.py -v`
Expected: FAIL — `ImportError: cannot import name 'seed_from_policy'`

- [ ] **Step 3: Implement `seed_from_policy`**

For each roster email, for each distinct agent name named by an `allow` rule whose `actions` include `"ask"`, insert a grant if `holds` is not already true. A rule naming no `agents` matches every name — collect candidate names from `agents` tuples across the allow rules and skip the unnamed case rather than granting `*`, since granting a wildcard would widen rather than subtract. Return the count inserted.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_grants_seed.py -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add src/ricardi/grants.py tests/test_grants_seed.py
git commit -m "feat(grants): seed the store from the policy roster, idempotently"
```

---

### Task 3: The CLI surface

**Files:**
- Modify: `src/ricardi/cli.py`
- Test: `tests/test_grants_cli.py`

**Interfaces:**
- Consumes: `GrantStore`, `seed_from_policy`, and `ricardi.policy.load`.
- Produces: subcommands `ricardi grants list`, `ricardi grants seed`, `ricardi grants add --person --name --verb --by`, `ricardi grants revoke --person --name --verb --by`. All resolve the database at `<state_dir>/grants.db`.

- [ ] **Step 1: Write the failing tests**

```python
def test_grants_list_prints_current_grants_not_history(tmp_path, capsys):
    # One granted row and one revoked pair: only the live grant is printed.
    assert "a@b.com" in capsys.readouterr().out
    assert "c@d.com" not in capsys.readouterr().out

def test_grants_seed_reports_how_many_rows_it_wrote(tmp_path, capsys):
    assert "2" in capsys.readouterr().out

def test_grants_revoke_leaves_the_history_intact(tmp_path):
    # After revoke: holds() is False, history() still has both rows.
    assert GrantStore.open(db).holds("a@b.com", "shared", "ask") is False
    assert len(GrantStore.open(db).history()) == 2

def test_grants_commands_fail_closed_on_a_corrupt_store(tmp_path, capsys):
    # Matches how PolicyError is already surfaced at startup.
    assert main(["grants", "list", "--state-dir", str(tmp_path)]) != 0
    assert "grants.db" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_grants_cli.py -v`
Expected: FAIL — unrecognised arguments `grants`

- [ ] **Step 3: Add the `grants` subparser to `cli.py`**

Follow the existing argument-parser style in `main`. Reuse the `--state-dir`/settings resolution already used for `audit.jsonl` and `sessions.json` rather than adding a second way to find `state/`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_grants_cli.py -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add src/ricardi/cli.py tests/test_grants_cli.py
git commit -m "feat(cli): ricardi grants list, seed, add and revoke"
```

---

### Task 4: Enforcement at the decision point

**Files:**
- Modify: `src/ricardi/policy.py` — `RulesPolicy.__init__`, `RulesPolicy.decide`
- Test: `tests/test_grants_policy.py`

**Interfaces:**
- Consumes: `GrantStore.holds` from Task 1.
- Produces: `RulesPolicy(rules=..., roster=..., grants: GrantStore | None = None)`. When `grants` is `None` the class behaves exactly as today, so every existing test stays green.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_ask_without_a_grant_row_is_refused(tmp_path):
    # Absence is not corruption, and no row means no access (spec 15.4).
    policy = RulesPolicy(rules=[ALLOW_ASK], roster={"U1": "a@b.com"},
                         grants=GrantStore.open(tmp_path / "g.db"))
    d = await policy.decide(Request(action="ask", principal=policy.principal("U1"), agent="shared"))
    assert d.allow is False
    assert d.rule == "no grant"

def test_an_ask_with_a_grant_row_is_allowed(tmp_path): ...

def test_a_grant_cannot_widen_what_policy_denies(tmp_path):
    # Subtract only: a deny rule plus a grant row is still a refusal, and the
    # audit records the named deny rather than the grant check.
    ...
    assert d.rule == "the named deny"

def test_a_grant_for_someone_not_on_the_roster_does_nothing(tmp_path):
    # Review Focus 1: principal.known is false, so the unknown-asker refusal
    # still fires first and the grant never resurrects them.
    ...
    assert d.rule == "no roster entry"

def test_a_tool_call_is_unaffected_by_grants(tmp_path):
    # Step 2 grants cover the "ask" action only; plane-1 tool rules are step 3.
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_grants_policy.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'grants'`

- [ ] **Step 3: Implement the check in `decide`**

Place it **after** the deny pass and **after** the existing `principal.known` refusal, and **before** the allow pass — the same position and for the same reason as the H1 fix: a request that trips a named deny must still be audited as that rule, and an unknown asker as `"no roster entry"`. Applies to `request.action == "ask"` only. On a missing grant return `Decision(allow=False, reason=self._default_reason(request), rule="no grant")`.

- [ ] **Step 4: Run the whole suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS — the new tests plus every existing test, because `grants=None` preserves today's behaviour.

- [ ] **Step 5: Commit**

```bash
git add src/ricardi/policy.py tests/test_grants_policy.py
git commit -m "feat(policy): require a grant row to address a name"
```

---

### Task 5: Wiring, and the deploy that is not a flag day

**Files:**
- Modify: `src/ricardi/cli.py` — open the store at startup and pass it to the policy
- Modify: `docs/deploying.md` — the one-time seed step
- Test: `tests/test_grants_startup.py`

**Interfaces:**
- Consumes: everything above.
- Produces: no new public surface. `ricardi run` opens `<state_dir>/grants.db` and constructs `RulesPolicy(..., grants=store)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_startup_refuses_on_a_corrupt_grant_store(tmp_path, capsys):
    # state/ shrugs off corrupt threads.json and sessions.json; this one must not.
    ...
def test_startup_succeeds_with_no_grant_file_at_all(tmp_path):
    # Absence is not corruption -- it starts, and denies every ask until seeded.
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_grants_startup.py -v`
Expected: FAIL — startup ignores the corrupt file

- [ ] **Step 3: Open the store in `cli.py` beside `audit_path` and `SessionIds`, and document the deploy**

Let `GrantError` propagate as `PolicyError` already does, so a corrupt store stops the process with a readable message rather than serving with no grants — which would deny everyone silently and look identical to a policy bug.

In `docs/deploying.md`, add to **What the deploy job actually does** that the first deploy carrying this needs `ricardi grants seed` run once on the box **before** the container restarts, and say plainly what happens otherwise: every `ask` is refused until it is. Note it is idempotent, so running it again is safe.

- [ ] **Step 4: Run the whole suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ricardi/cli.py docs/deploying.md tests/test_grants_startup.py
git commit -m "feat(grants): open the store at startup, and document the seed"
```
