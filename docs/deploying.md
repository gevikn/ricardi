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

## The security condition, which is not a footnote

**A self-hosted runner executes whatever the triggering workflow file says, as
the runner's user.** Not the workflow on `main` — the one on the ref being run.

For a private repository whose collaborators you would hand a shell to anyway,
that is an acceptable trade and the normal way these are used. For a public
one it is not: a pull request from a fork brings its own workflow file, and it
would run on this machine.

So, written down because it will be easy to forget in a year:

> **If this repository is ever made public, detach the runner first.**

The runner runs as `ricardi`, which is in the `docker` group — which is
root-equivalent on this host. A dedicated unprivileged user with a narrow
sudoers rule for `docker compose` would be tighter. It is not what is set up
today, and the reason is honest rather than principled: `ricardi` already owns
the deployment directory and already holds the deploy key, so using it needed
no new credential.

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
6. **Waits for the container to say `connected to Slack`**, up to a minute.
   Running is not the same as working; the log line is the real signal.
7. **Rolls back** to the previous commit and image if that never arrives.

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
| "Refusing to deploy -- tests is 'none'" | No `tests` run exists for that SHA. Push it, or `gh workflow run tests.yml --ref <branch>` first. |
| "Tracked files are modified" | Someone edited the checkout on the box. Resolve it there; the job will not discard it. |
| Rebuild succeeds, health check fails | Read `docker compose logs ricardi`. A bad `policy.toml` fails at startup and never reaches Slack. |
| Rolled back, and the rollback is also broken | The `:rollback` tag is one deep. `git checkout <older sha> && docker compose up -d --build` on the box. |
| Runner gone after an OS upgrade | The service unit survives, the runner binary may need `./config.sh remove` and re-registering. |

See [`running.md`](running.md) for the credential the container needs once it
is up, and [`remote-access.md`](remote-access.md) for the machine underneath.
