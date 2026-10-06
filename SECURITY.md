# Security

Ricardi lets people in a Slack channel run a Claude Code agent on a machine you
own. Read this before you point it at anything you care about.

## What this is, plainly

**Ricardi grants remote code execution to the people on its roster.** That is
not a flaw in it, that is what it does. Everything below is about bounding the
blast radius of a capability you are deliberately handing out.

If that sentence makes you uncomfortable, the right response is a smaller `cwd`
and a shorter policy, not a decision to trust the defaults.

## The boundary is structural. Policy is the second line.

The only durable boundary is what the agent's process can reach:

- Give each agent **its own git worktree** with **no push credentials**. Then
  "do not push to main" is not a rule anyone enforces, it is a thing that
  cannot happen.
- Point `cwd` at a directory you would not mind losing, and nowhere near a
  `.env`, an SSH key, or a cloud credential file.
- Use the Agent SDK's `sandbox` option rather than inventing one.
- Run Ricardi as a user with the access you intend the agent to have, and no
  more. The agent inherits everything that user can do.

Policy sits inside that boundary and catches honest mistakes. It is not the
boundary.

## What command matching does not do

`command_matches` will not stop someone who is trying. No list of shell
patterns survives `$( )`, pipes, `;`, `&&`, environment indirection, a base64
payload, or simply spelling the command differently — `git -C /repo push` does
not contain the string `git push`. The tests in `tests/test_policy.py` include
cases that **document which evasions get through**, deliberately, rather than
implying a completeness that is not there.

Treat command rules as guardrails against a capable, literal, non-adversarial
agent. That is a real threat model and the one these rules suit. It is not the
same as defending against a person who wants out.

## Things that silently disable enforcement

Learned the hard way, each one verified against a live agent:

- **`allowed_tools` shadows `can_use_tool`.** The SDK auto-approves anything an
  `allowed_tools` entry covers *before* the callback is consulted, so a gate
  built on that callback never runs for exactly the tools you listed. It looks
  like it is working. Ricardi uses a `PreToolUse` hook, which fires
  unconditionally, and passes no `allowed_tools` and no `permission_mode`.
- **Settings files can shadow it too.** Ricardi passes `setting_sources = []`
  so `~/.claude/settings.json` cannot add allow rules behind your back.
- **The shell tool is named per platform** — `Bash` on macOS, Linux and WSL,
  `PowerShell` on Windows. A **deny** rule naming only one covers nothing on
  the other, which is a silent hole rather than a visible failure. Write
  `tools = ["shell"]` and policy expands it to every platform's shell; deny
  rules in the shipped example name no tool at all and match on the command,
  which is broader still.

- **Matching must not depend on the platform.** `fnmatch.fnmatch` applies
  `os.path.normcase`, which lowercases on Windows and does nothing on POSIX, so
  rules matched through it were case-sensitive on Linux only — `GIT PUSH` evaded
  a `*git push*` deny on one of two identical deployments. Policy matches with
  `fnmatchcase` over explicitly lowered strings. CI runs the suite on Ubuntu and
  Windows for this reason; a green run on one platform proves little.

- **A permitted tool may not exist.** The session's tool set varies by platform
  and version: a Linux session had no `Glob` or `Grep` where a Windows one did.
  Policy is unaffected — a rule for a tool that never runs simply never matches
  — but an agent can end up far more limited than its policy suggests. That is
  the safe direction, and still worth knowing before you conclude a rule is
  working.

If you fork this and change how the agent is configured, re-read this list.

## What fails closed, and what does not

Denies: no policy, an unparseable policy, a policy that raises, a condition
that cannot be evaluated, an unknown Slack user, and any request no rule
allows.

**Does not fail closed:** a failed audit write. If the log cannot be written the
decision still proceeds, loudly logged, with a counter. A full disk taking the
whole system offline was judged worse than a gap in the record. If that trade
is wrong for you, change it in `audit.py` — it is one branch.

## What Ricardi does not protect against

- **Anything the agent reads can reach Slack.** An agent that can read a file
  can quote it into a channel. Scope `cwd` accordingly.
- **People on the roster, entirely.** `[limits]` in `policy.toml` caps how many
  turns one person may start, and `max_budget_usd` caps what one turn may cost,
  so a loop is bounded rather than unbounded. But the counts are in memory: a
  restart clears them, and someone on the roster can ask you to restart it. The
  per-turn budget is the ceiling that survives.
- **Prompt injection.** An agent reading a repository is reading text that can
  contain instructions. Policy constrains what it may then *do*, which is the
  mitigation — but it does not stop the agent being persuaded to try.
- **Slack itself.** Anyone who can post as a roster member can act as them.

## Credentials

`.env` holds the Slack tokens and is gitignored. The bot token is
workspace-scoped; treat it as a password. Rotating it means reinstalling the
app. The app-level token is separate and unaffected.

The roster in `policy.toml` holds email addresses and Slack user IDs. Those are
not secrets, but they are personal data — think before committing a real one to
a public repository.

## Reporting a vulnerability

Open a GitHub issue for anything already public or low severity. For anything
you would rather not post, use GitHub's private vulnerability reporting on this
repository.

Please include what you expected the boundary to be and what actually crossed
it. "Policy said deny and the tool ran anyway" is the most valuable report you
can send.
