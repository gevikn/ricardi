# Keeping it running

Ricardi holds its agents as child processes, so when Ricardi stops they stop.
A terminal you leave open is fine while you are shaking bugs out. It is not
fine once anyone depends on it: a policy enforced only while someone is logged
in is not much of a policy, and the first reboot ends every conversation.

What survives a restart, once it is running again: thread bindings
(`state/threads.json`), each agent's session id (`state/sessions.json`), and
the audit log. What does not: anything an agent was mid-way through.

## Windows — NSSM

[NSSM](https://nssm.cc) is the least ceremonious way to make a Windows service
out of a command.

```powershell
nssm install Ricardi "C:\path\to\ricardi\.venv\Scripts\python.exe"
nssm set Ricardi AppParameters "-u -m ricardi.cli --transport slack --backend sdk"
nssm set Ricardi AppDirectory "C:\path\to\ricardi"
nssm set Ricardi AppStdout "C:\path\to\ricardi\state\service.log"
nssm set Ricardi AppStderr "C:\path\to\ricardi\state\service.log"
nssm set Ricardi AppRotateFiles 1
nssm set Ricardi Start SERVICE_AUTO_START
nssm start Ricardi
```

Two things that will catch you out:

**The account.** A service runs as LocalSystem unless told otherwise, and
LocalSystem is not you: it has a different home directory, so `~/.claude` is
somewhere else and the agents have no Claude Code credentials. Set it to your
own account — `nssm set Ricardi ObjectName ".\\yourname" "password"` — or accept
that you are running agents as a far more privileged user than you meant to,
which is the opposite of everything in `SECURITY.md`.

**The environment.** A service does not inherit your shell. `.env` is read from
`AppDirectory`, so that part is fine, but anything you rely on from your profile
is not there.

## Windows — Task Scheduler

No extra software, slightly more clicking. Create a task that runs at logon (or
at startup, with the account caveat above), action:

```
Program:   C:\path\to\ricardi\.venv\Scripts\python.exe
Arguments: -u -m ricardi.cli --transport slack --backend sdk
Start in:  C:\path\to\ricardi
```

Tick **Run whether user is logged on or not** only if you have thought about
which account it then runs as. Under Settings, turn off "Stop the task if it
runs longer than", which otherwise kills a long-lived process on a timer.

## Linux and macOS — systemd

```ini
# ~/.config/systemd/user/ricardi.service
[Unit]
Description=Ricardi
After=network-online.target

[Service]
WorkingDirectory=%h/ricardi
ExecStart=%h/ricardi/.venv/bin/python -u -m ricardi.cli --transport slack --backend sdk
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now ricardi
loginctl enable-linger "$USER"   # or it stops when you log out
journalctl --user -u ricardi -f
```

A **user** unit rather than a system one, deliberately: it runs as you, with
your `~/.claude`, and with no more access than you have.

## Check it is actually working

Restarting cleanly is something to find out on a Tuesday afternoon, not during
an incident. After setting it up:

1. Have a conversation in Slack — enough that the agent knows something.
2. Restart the service.
3. In the same thread, ask the agent about what you discussed, without
   repeating it. If it answers, both halves survived.
4. `tail state/audit.jsonl` — decisions should still be landing.

If step 3 fails but the agent still replies, the thread binding survived and the
session id did not. Check that `state/sessions.json` exists and is being
written, and that the service's working directory is where you think it is.

## Watching it

- `state/audit.jsonl` — every decision. The denials are the interesting ones.
- `state/service.log` or `journalctl` — connection, agents, policy errors.
- Slack itself: if Ricardi is down, a mention simply gets no reply. There is no
  notification. Worth a check on whatever already watches your other services.

## Upgrading

Stop the service, pull, `uv pip install -e ".[slack,sdk]"`, run `pytest`, start
it again. The state files are forward-compatible; nothing in `state/` needs
clearing between versions.
