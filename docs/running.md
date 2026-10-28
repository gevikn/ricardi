# Keeping it running

Ricardi holds its agents as child processes, so when Ricardi stops they stop.
A terminal you leave open is fine while you are shaking bugs out. It is not
fine once anyone depends on it: a policy enforced only while someone is logged
in is not much of a policy, and the first reboot ends every conversation.

The bar is not "it starts". The bar is **it survives you logging out**, because
the machine this ends up on is a box in the corner that nobody is sitting at.
Most of what follows is about that one requirement, which every operating
system answers differently and none of them answer for free.

What survives a restart: thread bindings (`state/threads.json`), each agent's
session id (`state/sessions.json`), and the audit log. What does not: anything
an agent was mid-way through.

Nothing listens, in any of these. Socket Mode dials out to Slack, so there is
no port to publish and none to defend.

This file is about the process. [`remote-access.md`](remote-access.md) is about
the machine underneath it: bringing a VM up with nobody logged in, and reaching
it from somewhere else without opening anything.

## Does it survive logging out?

| Platform | How | Logout | Admin to install | Runs as |
|---|---|---|---|---|
| Linux | Docker, `restart: unless-stopped` | **yes** | docker group | container user |
| Linux | systemd user unit + lingering | **yes** | sudo once, for lingering | you |
| macOS | launchd LaunchDaemon | **yes** (unverified) | root | you |
| macOS | Docker Desktop | no | — | — |
| Windows | logon task (`-Scheduler`) | no | none | you |
| Windows | Docker Desktop | no | — | — |
| Windows | NSSM service, "This account" | **yes** | admin + a **password** | you |

Three things that table is trying to tell you.

**Admin to install, unprivileged to run.** Every route that survives a logout
needs a privileged install, because that is what it means to run with nobody
signed in. None of them should *run* privileged. A deployment that ends with an
agent executing shell commands as root or `SYSTEM` has inverted the trade — and
a `SYSTEM` scheduled task is the easy, wrong way to get a passwordless Windows
service, because `SECURITY.md` argues the real boundary is structural and that
undoes it.

**Docker Desktop is not a service.** On Linux the Docker daemon is a system
service, so a container really does outlive your session. On Windows and macOS
Docker Desktop runs inside your login session, so the container dies with it.
Same `compose.yaml`, different answer, entirely because of where the daemon
lives.

**If you sign in with a PIN**, Windows Hello is device-local and cannot be used
for service logon, so the only Windows row that survives logout is one you
cannot fill in. Run a Linux VM on that box instead — a Hyper-V VM set to start
automatically runs at the host level, so it comes up before anyone logs in and
stays up after they leave.

## Credentials: pick one, and never sign in on the box

The Agent SDK does not call an API. It spawns the `claude` CLI, which has to be
signed in -- so "how does it authenticate" is really "which credential is in
the environment", and the answer has to be **exactly one**. Two at once is the
most confusing failure this project has, because the wrong one wins quietly
and the right one sits there unused.

| Credential | Comes from | Expires | Survives you using Claude Code elsewhere |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | the Anthropic Console | when you rotate it | **yes** -- no session to revoke |
| `CLAUDE_CODE_OAUTH_TOKEN` | `claude setup-token` | undocumented, and silently | mostly |
| a stored login | `claude`, or `/login` | whenever a refresh fails | **no** -- this is the one that prompts you |

**An always-on host uses one of the first two and never the third.**

### Never run `claude` on the always-on host

A stored login takes precedence over the environment. So if you sign in on the
box -- or a helpful future you does, to "just check something" -- that login
shadows the credential in `.env`. Everything keeps working, until the stored
login's refresh fails some weeks later and Ricardi goes quiet with a perfectly
good token two feet away. It is the hardest version of this to diagnose,
because the thing you would go and check looks correct.

So, on the always-on host:

- never run `claude` interactively, never `/login`, never `/logout`
- `~/.claude/.credentials.json` must not exist; under Docker, `claude-config/`
  stays empty
- `install-service.sh` refuses when it finds both, so you hear about it at
  install time instead of a month later

**What is safe:** `/login` on your own workstation is a different machine with
its own credential store, and does not reach the service. What *does* reach
across machines is anything that revokes sessions account-wide -- a password
change, signing out of claude.ai, an administrator ending sessions. An API key
is immune to all three, which is why it is the first row of the table.

### Which of the two

**`ANTHROPIC_API_KEY` -- lights out.** No scheduled expiry, and nothing an
interactive login anywhere can disturb. It bills per token to a Console
account rather than drawing on a personal subscription, which is also the
honest answer once more than one person can mention the bot.

**`CLAUDE_CODE_OAUTH_TOKEN` -- stays on your subscription.**

```bash
claude setup-token          # needs a browser once, on any machine
```

Its lifetime is not documented anywhere official -- third-party guides claim
anything from a few weeks to a year -- and it does not refresh itself. Treat
the expiry as *unknown* rather than annual: you will hear about it from a
monitor, not from a calendar. An expired credential and a dead host produce
the identical symptom, which is no reply at all. See "Watching it" below.

### Either way, it lives in `.env`

`chmod 600 .env`. Ricardi loads `.env` into its own environment and the SDK
hands its whole environment to the CLI it spawns, so that is the entire wiring.

**Not in a plist, a unit file, or a service command line.**
`/Library/LaunchDaemons` is world-readable, unit files usually are, and a
command line shows up in the process list.

## Docker

The shortest path on Linux, and the only one where the agent's reach is
enforced by something other than a configuration file. `SECURITY.md` argues the
real boundary is structural and policy is the second line behind it — a
container is that boundary. The agent sees the one directory you mounted and
nothing else on the machine, whatever a rule says.

```bash
cp .env.example .env                        # the two Slack tokens
cp docker/agents.example.toml agents.toml
cp docker/policy.example.toml policy.toml
mkdir -p state claude-config workspace
```

Then authenticate **one** of the two ways -- doing both is the trap described
under "Credentials" above:

```bash
# Always-on: a credential in .env, nothing stored, nothing to sign into.
docker compose up -d --build

# Trying it out at a keyboard: sign in once, no credential in .env.
docker compose run --rm login
docker compose up -d --build

docker compose logs -f
```

`login` writes a stored credential into `claude-config/`, and a stored
credential beats the environment. Do both and the credential in `.env` is
decoration until the day that stored login stops refreshing. On an always-on
box, skip it regardless: the interactive login wants a TTY to paste into,
which a service manager does not give you.

Both example files use **container** paths, so no host path appears in either
and the same config works on any machine. `/workspace` is whatever you mount
there; point it at the one worktree you mean, not a home directory.

**Credentials here.** *Which* credential to use is settled under
[Credentials: pick one](#credentials-pick-one-and-never-sign-in-on-the-box)
above. This is only where Docker keeps it. `.env` is handed to the container;
`docker compose run --rm login`, for a box you sit at, stores a login in
`./claude-config`, mounted at `/config/claude`. A directory of its own rather
than your host `~/.claude`, so the container holds one account's credentials
and this project's transcripts, not everything you have ever done with Claude
Code. On an always-on box it stays empty.

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

## Linux — systemd

Without a container. `./scripts/install-service.sh` (`--dry-run` first) writes
a **user** unit, not a system one: it runs as you, with your `~/.claude`, and
with no more access than you have.

It also runs `loginctl enable-linger`, without which the unit stops the moment
you log out of the box you sshed into. That one command is the difference
between this row surviving a logout and not.

```bash
./scripts/install-service.sh --dry-run
./scripts/install-service.sh
journalctl --user -u ricardi -f
```

No `--log-file` here: journald already captures stdout, so a second rotating
log would only be a file to forget about.

`--uninstall` removes it and leaves `state/` alone.

## macOS — launchd

**Unverified.** Nobody on this project owns a Mac. The script is written, and
the plist it generates is checked on every commit — `tests/test_install_posix.py`
parses it and asserts it does not run as root and carries no credential — but
nothing has watched launchd actually load it. If you run it, please open an
issue either way.

Run the same script; it detects Darwin and installs a **LaunchDaemon** rather
than a systemd unit. A LaunchAgent would be easier to write and it dies at
logout, which is the whole problem, so: `/Library/LaunchDaemons`, root to
install, and `UserName` in the plist so it drops to your account to run.

```bash
./scripts/install-service.sh --dry-run      # no root needed to look
sudo ./scripts/install-service.sh
tail -f state/service.log
sudo ./scripts/install-service.sh --uninstall
```

The preflight refuses unless two things are true, both of them things that
would otherwise fail confusingly at runtime:

**A credential in `.env`.** Required here, not optional. Claude Code keeps its
OAuth token in the login Keychain, and a LaunchDaemon runs outside any login
session — so the Keychain is locked and unreachable, and `~/.claude` existing
proves nothing. An API key, or a token from `claude setup-token`, is the way
in. Either kind will do.

**`claude` in `~/.local/bin`, `/usr/local/bin` or `/opt/homebrew/bin`.** A
LaunchDaemon starts with a bare `PATH`, so the plist has to name the directory
the CLI lives in, and the script only knows those three.

Two more it cannot check for you:

- **Privacy protection.** macOS gates `~/Documents`, `~/Desktop` and friends,
  and a daemon reaching into them gets permission errors that read like bugs.
  Point the workspace somewhere plain, like `~/code/something`, rather than
  granting Full Disk Access to a process that runs shell commands on request.
- **`launchctl` is silent about a bad plist.** It refuses one not owned by
  `root:wheel`, or one that is group-writable, and the symptom is a service
  that simply never starts. The script sets both; if you hand-edit it, keep
  them.

## Windows

The most apparatus and the worst answer, so read the table again before you
start. Use a Linux VM on the same box if you can.

What works today, and does not survive logging out:

```powershell
.\scripts\install-service.ps1 -Scheduler -DryRun
.\scripts\install-service.ps1 -Scheduler
Start-ScheduledTask -TaskName Ricardi
```

`-Scheduler` registers a **logon task**: runs as you, no stored credential, no
elevation. It starts when you log in and stops when you log out — fine for a
machine you use, useless for one in a cupboard.

A task has no console and captures nothing, so the installer passes
`--log-file state\service.log` and Ricardi writes its own log, rotating at
10MB. Without it the task runs perfectly and leaves nothing to read, which
looks exactly like being down.

For something that stays up with nobody signed in, drop `-Scheduler` and it
installs an [NSSM](https://nssm.cc) service, which needs `nssm` on PATH, an
elevated prompt, and the account's **password**, set through `nssm edit` and
never on the command line where it would land in your shell history and the
process list.

Three things that catch people out:

**A PIN is not a password.** Windows Hello is device-local and cannot be used
for service logon. If you sign in with a PIN there is no password to give and
this route is closed.

**LocalSystem is not a shortcut.** Its home is
`C:\Windows\system32\config\systemprofile`, so `~\.claude` is not there. A
token fixes the credential half, but it still runs the agent with the highest
privilege on the machine, which is not a trade worth making.

**The environment.** A service does not inherit your shell. `.env` is read from
the working directory, which is fine; the `claude` CLI being on `PATH` is not —
the same bare-`PATH` problem as launchd, and it has to be set on the service.

**Still to do:** a passwordless service under a *virtual service account*
(`NT SERVICE\Ricardi`) — least privilege, no password, survives logout. A token
removes the reason we first called it impossible. It needs the CLI on the
service `PATH` and `icacls` grants on the config directory and the workspace,
and it has not been tried yet.

## Check it is actually working

Restarting cleanly is something to find out on a Tuesday afternoon, not during
an incident. After setting it up:

1. Have a conversation in Slack — enough that the agent knows something.
2. Restart it:
   - `docker compose restart`
   - `systemctl --user restart ricardi`
   - `sudo launchctl kickstart -k system/local.ricardi`
   - `Stop-ScheduledTask -TaskName Ricardi` then
     `Start-ScheduledTask -TaskName Ricardi` — there is no
     `Restart-ScheduledTask`, whatever the symmetry suggests.
3. In the same thread, ask the agent about what you discussed, **without
   repeating it**. If it answers, both halves survived.
4. `python -m ricardi.report` — decisions should still be landing.

Then the one that matters for a box in a cupboard: **log out, log back in, and
mention it in Slack.** Everything above passes on a machine you are sitting at.
This is the step that tells you which row of the table you are actually in.

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
- On macOS, `state/launchd-stderr.log` as well: it catches a traceback from
  before logging exists, which is where a bad virtualenv shows up.
- Slack itself: if Ricardi is down, a mention simply gets no reply. There is no
  notification, and an expired credential looks identical. Worth adding to
  whatever already watches your other services.

## Upgrading

Docker: `git pull && docker compose up -d --build`.

Otherwise: stop it, `git pull`, `uv pip install -e ".[slack,sdk]"`, run
`pytest`, start it. The state files are forward-compatible; nothing in `state/`
needs clearing between versions.
