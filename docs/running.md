# Keeping it running

Ricardi holds its agents as child processes, so when Ricardi stops they stop.
A terminal you leave open is fine while you are shaking bugs out. It is not
fine once anyone depends on it: a policy enforced only while someone is logged
in is not much of a policy, and the first reboot ends every conversation.

What survives a restart: thread bindings (`state/threads.json`), each agent's
session id (`state/sessions.json`), and the audit log. What does not: anything
an agent was mid-way through.

Nothing listens, in any of these. Socket Mode dials out to Slack, so there is
no port to publish and none to defend.

This file is about the process. [`remote-access.md`](remote-access.md) is about
the machine underneath it: bringing a VM up with nobody logged in, and reaching
it from somewhere else without opening anything.

## Docker

The shortest path on any host, and the only one where the agent's reach is
enforced by something other than a configuration file. `SECURITY.md` argues the
real boundary is structural and policy is the second line behind it — a
container is that boundary. The agent sees the one directory you mounted and
nothing else on the machine, whatever a rule says.

```bash
cp .env.example .env                        # the two Slack tokens
cp docker/agents.example.toml agents.toml
cp docker/policy.example.toml policy.toml
mkdir -p state claude-config workspace

docker compose up -d --build
docker compose logs -f
```

Both example files use **container** paths, so no host path appears in either
and the same config works on any machine. `/workspace` is whatever you mount
there; point it at the one worktree you mean, not a home directory.

**Credentials.** The Agent SDK does not call an API — it spawns the `claude`
CLI, which needs a credential. There are two ways to give it one, and which you
pick matters more on a box nobody logs into than it first appears.

*An API key, for a box left running.* Put a Console API key in `.env` as
`ANTHROPIC_API_KEY` and run nothing else. It is the only option here with no
scheduled failure built in: it ends when you rotate it, not on a clock you
cannot see. `claude-config/` stays empty. The trade is billing — per-token API
spend rather than a subscription — which is the more honest arrangement anyway
for a bot that several people in a Slack workspace can invoke.

*A stored login, for a box you sit at.* `docker compose run --rm login` signs
Claude Code in once into `./claude-config`, mounted at `/config/claude`. A
directory of its own rather than your host `~/.claude`, so the container holds
one account's credentials and this project's transcripts, not everything you
have ever done with Claude Code. Subscription billing, but the session expires
without warning, and the only symptom is a mention that gets no reply.

**Pick one.** A stored credential can take precedence over the environment, so
an `ANTHROPIC_API_KEY` sitting beside a populated `claude-config/` may be
decoration — and worse, a long-running process can keep using an *expired
stored session* while a perfectly good key goes unused. On an unattended box:
no `login` run, and `claude-config/` empty.

Nothing is baked into the image either way.

**File ownership.** The container runs as a non-root user, uid 1000 by default.
If your host user differs, build with yours or the bind mounts will be
read-only to it:

```bash
UID=$(id -u) GID=$(id -g) docker compose up -d --build
```

**What is mounted, and why each one.** `state/` outside the container so a
rebuild does not lose the conversations; `agents.toml` and `policy.toml`
read-only, since the container has no business editing who may ask for what.

## Linux and macOS — systemd

Without a container. Run `./scripts/install-service.sh` (`--dry-run` first),
which writes a **user** unit, not a system one: it runs as you, with your
`~/.claude`, and with no more access than you have.

It also runs `loginctl enable-linger`, without which the unit stops the moment
you log out of the box you sshed into.

```bash
./scripts/install-service.sh --dry-run
./scripts/install-service.sh
journalctl --user -u ricardi -f
```

`--uninstall` removes it and leaves `state/` alone.

## Windows

Least likely to be what you want, and the most apparatus. Use Docker unless you
specifically need a native Windows host — which you might, since the shell tool
is `PowerShell` there and `Bash` everywhere else, and a policy that behaves
identically on both is worth proving.

```powershell
.\scripts\install-service.ps1 -Scheduler -DryRun
.\scripts\install-service.ps1 -Scheduler
Start-ScheduledTask -TaskName Ricardi
```

`-Scheduler` registers a **logon task**: runs as you, no stored credential, no
elevation. It starts when you log in and stops when you log out.

A task has no console and captures nothing, so the installer passes
`--log-file state\service.log` and Ricardi writes its own log, rotating at
10MB. Without it the task runs perfectly and leaves nothing to read, which
looks exactly like being down. Docker and systemd capture stdout themselves,
so neither needs the flag.

For something that must stay up with nobody signed in, drop `-Scheduler` and it
installs an [NSSM](https://nssm.cc) service instead — which needs `nssm` on
PATH, an elevated prompt, and the account's **password**, set through `nssm
edit` and never on the command line, where it would land in your shell history
and the process list.

Three things that catch people out:

**The account.** A service runs as LocalSystem unless told otherwise, whose
home directory is `C:\Windows\system32\config\systemprofile` — so `~\.claude`
is not there, the agents cannot authenticate, and Ricardi refuses to start with
a message naming the wrong path. A *virtual service account* has the same
problem plus no read access to your repos. Only "This account" works.

**A PIN is not a password.** Windows Hello is device-local and cannot be used
for service logon. If you sign in with a PIN, use `-Scheduler`.

**The environment.** A service does not inherit your shell. `.env` is read from
the working directory, which is fine; anything else from your profile is not
there.

## Check it is actually working

Restarting cleanly is something to find out on a Tuesday afternoon, not during
an incident. After setting it up:

1. Have a conversation in Slack — enough that the agent knows something.
2. Restart it (`docker compose restart`, `systemctl --user restart ricardi`,
   or, on Windows, `Stop-ScheduledTask -TaskName Ricardi` and then
   `Start-ScheduledTask -TaskName Ricardi` — there is no
   `Restart-ScheduledTask`, whatever the symmetry suggests).
3. In the same thread, ask the agent about what you discussed, **without
   repeating it**. If it answers, both halves survived.
4. `python -m ricardi.report` — decisions should still be landing.

If step 3 answers but has forgotten, the thread binding survived and the session
id did not. Check that `state/sessions.json` exists and is being written, and
that the working directory is where you think it is. A bot that replies and has
forgotten the last hour looks exactly like one that is working.

## Watching it

- `python -m ricardi.report` — every decision, counted, refusals grouped by the
  rule that caused them. Reads rotated generations too.
- `docker compose logs -f`, `journalctl --user -u ricardi -f`, or
  `Get-Content -Wait state\service.log` — connection, agents, policy errors.
  The first line names the transport, the agents, and how many rules loaded,
  which is the line you want when reading back weeks later.
- Slack itself: if Ricardi is down, a mention simply gets no reply. There is no
  notification. Worth adding to whatever already watches your other services.

## Upgrading

Docker: `git pull && docker compose up -d --build`.

Otherwise: stop it, `git pull`, `uv pip install -e ".[slack,sdk]"`, run
`pytest`, start it. The state files are forward-compatible; nothing in `state/`
needs clearing between versions.
