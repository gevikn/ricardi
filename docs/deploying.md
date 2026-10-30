# Deploying without getting on the box

The goal is narrow: ship a commit to the machine, from anywhere, without an
SSH session and without remembering what the compose incantation was.

```bash
gh workflow run deploy.yml -f ref=main
gh run watch
```

That is the whole interface. What follows is why it is shaped this way and how
to set it up once.

## Why the runner lives on the box

A hosted runner cannot reach this machine. That is not an accident to work
around — it is the design. Nothing listens: Socket Mode dials out to Slack, and
SSH arrives through a cloudflared tunnel that also dials out. There is no
inbound port, and adding one to let a CI system in would undo the property the
rest of this project is built on.

A self-hosted runner dials out too, exactly like the other two. So it fits
without opening anything.

Two consequences come free with that choice:

- **The image is built where it is used.** No registry in the path, nothing to
  push or pull, no chance of an architecture mismatch.
- **The layer cache survives.** A rebuild after a one-line change takes seconds,
  because the dependency layers are still there from last time. On a hosted
  runner every build starts from nothing.

And one that matters for a private repository: self-hosted runners do not
consume GitHub Actions minutes. Building a Docker image on a hosted runner for
a private repo does.

## Going public, with the runner still attached

The received wisdom is that a self-hosted runner and a public repository do not
mix, and GitHub's own documentation says they should "almost never" be
combined. That is sound advice aimed at the common mistake, which is attaching
a runner and then letting fork pull requests reach it. The danger is specific
rather than general, so the rule here is specific too.

**A self-hosted runner executes whatever the *triggering* workflow file says**
-- not the one on `main`, the one on the ref being run -- as the runner's user.
That is fine while every trigger that can reach it comes from someone with
write access. A `pull_request` from a fork does not.

So the rule, and it is the only one that matters:

> **No fork-triggerable event may ever name a self-hosted runner.**

Which splits the work by trust, not by habit:

| Trigger | Who can cause it | Runner |
|---|---|---|
| `pull_request` | anyone | GitHub-hosted |
| `push`, `workflow_dispatch`, `release`, `schedule` | write access only | self-hosted, fine |

This repository is already shaped that way. `tests.yml` runs on hosted runners;
`deploy.yml` is `workflow_dispatch` only. Going public needs no change to
either -- which is the point of having checked.

`tests/test_workflow_trust.py` enforces it rather than trusting anyone to
remember. Wire a fork-triggerable event to `[self-hosted, ricardi-home]` and
the suite fails, naming the file, the job and the event.

**Do not rely on fork-PR approval settings.** The "first-time contributor"
exemption lets anyone through once they have had a single commit merged, so an
attacker lands a typo fix, waits, and sends the real payload. Approval is also
a human reading a diff, and a workflow change inside an otherwise sensible pull
request is exactly what gets approved. Treat it as a speed bump, never as the
control.

**Never add `pull_request_target`.** It runs with this repository's secrets;
checking out the fork's head under it is the "pwn request" pattern. The guard
flags it wherever it appears, including on hosted runners.

One more thing that changes on the day you go public: **GitHub-hosted runners
are free for public repositories**, so the cost argument for self-hosting
*tests* disappears at exactly the moment self-hosting them would become
dangerous. Deployment stays self-hosted, because it must run on the box.

### The honest part

The runner runs as `ricardi`, which is in the `docker` group -- root-equivalent
on this host. A dedicated unprivileged user with a narrow sudoers rule for
`docker compose` would be tighter. It is not what is set up here, and the
reason is convenience rather than principle: `ricardi` already owns the
deployment directory and already holds the deploy key, so using it needed no
new credential. Anyone with write access to this repository can dispatch a
workflow and therefore run commands as that user. That is the same trust as
handing them a shell on the box, which is the right mental model for who gets
write access.

## What the deploy job actually does

1. **Fetches and resolves the ref** to a commit, and says which one.
2. **Refuses to deploy a commit whose tests did not pass.** It asks the API for
   the `tests` workflow's conclusion on that exact SHA. A deployer that merely
   follows a branch will cheerfully ship a red commit; this is the step that
   stops it. `-f skip_test_gate=true` overrides it deliberately.
3. **Tags the running image `:rollback`.** A rebuild replaces the image under
   the same name, so without this there is nothing to go back to.
4. **Moves the working copy** — and refuses if tracked files are modified on
   the box. A dirty file means a person changed something there, and silently
   discarding it is how that person stops trusting this pipeline.
5. **Rebuilds and restarts** with `UID`/`GID` passed explicitly, because `UID`
   is readonly in bash and does not reach compose on its own.
6. **Waits for `state/ready.json`**, up to a minute. Running is not the same
   as working, so the gate wants the process's own word for it: Ricardi writes
   that file once it is serving, and clears it at startup, so its presence
   means ready *now*. The gate compares the `connected_at` in it against the
   container's `StartedAt`.

   It used to grep the container log for `connected to Slack` instead, and
   that was wrong twice over. `--tail` is unbounded in time, so a container
   restarted rather than recreated keeps its old output and the gate passes on
   a line from a previous deploy — indistinguishable from one that never came
   up. And this box's log loses its newest lines within minutes, so anchoring
   the window on the current boot, the obvious fix, fails a container that is
   perfectly healthy. `state/` is a bind mount, so the marker crosses the
   container boundary with no log driver in the path.

   The comparison is against the container, never against when the deploy ran:
   an unchanged image is not recreated, and a gate expecting something new
   every time would fail the deploy that correctly changed nothing.
7. **Rolls back** to the previous commit and image if that never arrives.

### The first deploy that carries the grant store

Who may address which agent is recorded in `state/grants.db`, and from the
commit that wires it in, Ricardi checks it on every `ask`. A fresh store is
empty, so the first deploy carrying this needs the seed run once on the box.

The route that works is the plain one: deploy normally, then seed straight
away.

```bash
cd /opt/ricardi
docker compose exec ricardi python -m ricardi.cli grants seed --by <your email>
```

Between the new container coming up and the seed finishing, every `ask` is
refused. That is seconds, and it is safe: the store is read on every call with
no cache, so the first seeded row takes effect immediately and nothing needs a
restart. It has to be `exec` into the *new* container, because that is the
first one whose image has the `grants` command at all.

Skip it and nothing fails loudly. Ricardi starts, the health gate passes, and
every `ask` is refused until the store is seeded. That looks exactly like a
policy bug, and you will go and read `policy.toml` for a mistake that is not
there. A *missing* store is the intended pre-seed state and not an error; a
*corrupt* one is different, and stops startup with a message naming the file.
Unlike `threads.json` and `sessions.json`, which shrug off corruption so as not
to stop Ricardi answering, serving with no grants would deny everyone silently.

Seeding grants what `policy.toml` already permits and nothing else, and it is
idempotent: running it again adds nothing that is already in force, so it is
safe to repeat.

It is `docker compose exec`, inside the running container, rather than
`python -m ricardi.cli` against `state/` from the host. The store is SQLite in
WAL mode, and WAL coordinates its readers and writers through shared memory
that is only reliable between processes on the same side of a mount boundary.
Here both sides are Linux on one filesystem, so the host route would probably
work; running it in the container removes the question.

If you would rather have no refusals at all, there is a two-stage deploy.
The commits that add the `grants` command and the one that wires the store into
startup are separate, so deploy the commit that adds the `grants` command
first, while the running container still serves without checking the store;
seed with the `exec` above; then deploy the wiring. The catch is that the
intermediate commit has no `tests` run of its own, and `deploy.yml` refuses
any SHA without one. Dispatch that first deploy with the `skip_test_gate`
input, and only for that one; the wiring deploy goes through the gate as
usual. Whether a few seconds of refusals are worth a bypassed gate is your
call. The plain route is what this document recommends.

It never touches `.env`, `policy.toml`, `agents.toml` or `state/`. Those are
untracked, irreplaceable, and the reason the job operates on `/opt/ricardi`
rather than checking out a fresh copy.

## Setting the runner up, once

On the box, as `ricardi`. Get a registration token from a machine with `gh`
(it expires in an hour):

```bash
gh api -X POST repos/<owner>/<repo>/actions/runners/registration-token --jq .token
```

Then:

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -fsSL -o runner.tar.gz \
  https://github.com/actions/runner/releases/download/v<VERSION>/actions-runner-linux-x64-<VERSION>.tar.gz
tar xzf runner.tar.gz && rm runner.tar.gz

./config.sh --unattended \
  --url https://github.com/<owner>/<repo> \
  --token <REGISTRATION_TOKEN> \
  --name ricardi-home \
  --labels self-hosted,ricardi-home \
  --work _work
```

The labels matter: `deploy.yml` targets `[self-hosted, ricardi-home]`, so a
second runner added later does not silently start answering for this one.

`ricardi-home` is just the name of the box this was first set up on — there is
nothing special about the string. Call it whatever your machine is called, but
change it in **both** places or the deploy will queue forever waiting for a
runner that does not exist: here, and the `runs-on` in
`.github/workflows/deploy.yml`.

Install it as a service so it survives the reboot — which on this host is the
part most likely to be forgotten, given what it took to make the VM itself
come back unattended:

```bash
sudo ./svc.sh install ricardi
sudo ./svc.sh start
sudo ./svc.sh status
```

Then prove it the only way that counts, the same way the VM was proved:
reboot the host, do not log in, and run `gh workflow run deploy.yml` from
somewhere else.

## When it goes wrong

| Symptom | Cause |
|---|---|
| Job queues forever | No runner with both labels is online. `svc.sh status` on the box. |
| "Refusing to deploy -- tests is 'none'" | No `tests` run exists for that SHA at all. Push it, or `gh workflow run tests.yml --ref <branch>` first. The gate waits up to ~15 minutes for a run already in flight, so this means absence, not slowness. |
| "Tracked files are modified" | Someone edited the checkout on the box. Resolve it there; the job will not discard it. |
| Rebuild succeeds, health check fails | Read `docker compose logs ricardi`. A bad `policy.toml` fails at startup and never reaches Slack. |
| Every `ask` refused, and `policy.toml` looks right | The grant store is empty. `docker compose exec ricardi python -m ricardi.cli grants list` shows nothing; run `grants seed` (see above). |
| Startup fails naming `grants.db` | The grant store is corrupt. It is deliberately not ignored. Restore the newest snapshot from `state/backups/`, or recover by hand; see below. |
| Rolled back, and the rollback is also broken | The `:rollback` tag is one deep. `git checkout <older sha> && docker compose up -d --build` on the box. |
| Runner gone after an OS upgrade | The service unit survives, the runner binary may need `./config.sh remove` and re-registering. |

### Backing up the grant store

`state/grants.db` is the only thing on this box git cannot rebuild. A recorded
revoke is not re-derivable from `policy.toml` -- that is the entire point of
having a mutable layer -- so losing the file loses the answer to "who let them
in, and when".

Do not back it up by copying the file. The store is in WAL mode and the
auto-checkpoint does not fire until the write-ahead log is about a thousand
pages, so a store with a handful of rows keeps every one of them, and the
schema, in `grants.db-wal`. A copy of `grants.db` alone opens without
complaint and contains no `grant_log` table at all. That was measured on this
box, not reasoned about.

So there is a verb that goes through sqlite's backup API, reopens the copy,
runs `integrity_check`, counts its rows against the source, and deletes the
copy rather than keep one that came back short:

```bash
docker compose exec -T ricardi python -m ricardi.cli grants backup --keep 14
```

On a timer, as the `ricardi` user (`crontab -e`; no sudo needed, and a system
unit would need it):

```cron
17 3 * * * cd /opt/ricardi && /usr/bin/flock -n /tmp/ricardi-grants-backup.lock docker compose exec -T ricardi python -m ricardi.cli grants backup --keep 14 >> /opt/ricardi/state/grants-backup.log 2>&1
```

`flock` because two snapshots at once is pointless rather than harmful, and
cron is the one caller that can overlap itself. It writes to
`state/backups/grants-<stamp>.db` and prunes to the newest 14 -- a few hundred
kilobytes in total, on the same disk.

Each snapshot is deliberately **one file**: it is taken out of WAL mode before
it is closed, so there is no `-wal` or `-shm` to forget. Copying a snapshot is
a copy of exactly one file, which is the thing that is not true of the live
store.

**Same disk is the limit of what this buys you.** It covers corruption, a bad
migration and a mistaken delete; it does not survive the disk. Getting a copy
off the box needs either a backup credential living on the box, or a pull from
a machine that is already authenticated -- SSH here is behind Cloudflare
Access, whose token is interactive and expires daily, so an unattended nightly
pull cannot work by design. Until that is decided, pull one when you are at
the keyboard:

```bash
scp "ricardi-home:/opt/ricardi/state/backups/$(ssh ricardi-home 'ls -1 /opt/ricardi/state/backups/grants-*.db | tail -1' | xargs basename)" .
```

### A corrupt grant store

Be clear about what the options are, because the obvious one is a trap. Check
`state/backups/` first: if the timer above is installed, the newest snapshot is
at most a day old, and restoring it is a file copy with the container stopped.
Verify what you are about to restore before you trust it: a snapshot taken
after the damage is not a recovery. Read it on its own, by the name the store
expects, in a directory of its own:

```bash
docker compose exec -T ricardi sh -c 'mkdir -p /tmp/inspect   && cp /app/state/backups/grants-<stamp>.db /tmp/inspect/grants.db   && python -m ricardi.cli grants list --state-dir /tmp/inspect'
```

Pointing `--state-dir` at `state/backups` itself does *not* read a snapshot.
It looks for `grants.db`, does not find one, and creates a new empty store
there -- which prints nothing and looks exactly like a snapshot with no
grants in it.

If there is no snapshot, say so out loud rather than reaching for the obvious
move. The restic configuration backs up the owner's laptop, not `/opt/ricardi`
on the box, and `state/` is the one directory the deploy promises never to
touch, so without the timer nothing copies it at all.

Moving the file aside and re-seeding works mechanically and does damage
quietly. `grants seed` skips any person, agent and verb that has *any* history,
and discarding the file discards that history, so the re-seed writes a fresh
grant for everyone `policy.toml` permits, including anyone an admin had
revoked. It prints a success count and nothing records that revocations were
undone. If you must do it, first list who had been revoked from whatever you
remember or can salvage, and run `grants revoke` for each straight after
seeding.

Move all three files when you move it: `grants.db`, `grants.db-wal` and
`grants.db-shm`. The store is in WAL mode, so the newest commits live in the
`-wal`; renaming only `grants.db` leaves that beside the fresh empty database
the next startup creates, which sqlite either replays into it or refuses as
corrupt. For the same reason, read the moved-aside copy with its own `-wal`
next to it. `python -c "import sqlite3; ..."` is the tool to reach for -- the
`sqlite3` CLI is not installed in the container.

See [`running.md`](running.md) for the credential the container needs once it
is up, and [`remote-access.md`](remote-access.md) for the machine underneath.
