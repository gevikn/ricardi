"""install-service.sh, both branches -- including macOS, without a Mac.

Nobody on this project has one. Writing a launchd installer and shipping it
unverified is how a project fails on someone's first contact with it, so this
runs the real script with `uname` stubbed to Darwin and checks the artefact it
produces -- the plist -- rather than taking it on faith. The Linux branch is
checked here too: it was refactored to make room for the macOS one, and a
systemd unit breaks just as silently.

What this cannot tell you: whether launchd loads it, whether the agents
authenticate, or whether macOS privacy protection blocks the workspace. Those
need hardware and are marked unverified in docs/running.md. What it does tell
you is that the plist parses, runs as a person rather than root, and does not
have a live credential in it -- the three ways this script could be wrong that
do not need a Mac to detect.
"""

import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-service.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32",
    reason="needs bash, and the stubs assume a POSIX shell",
)


def _stubs(tmp_path: Path, home: Path, kernel: str = "Darwin") -> Path:
    """uname, id and dscl, enough to make the script believe it is on a Mac."""
    d = tmp_path / "stub"
    d.mkdir()
    (d / "uname").write_text(
        f'#!/bin/sh\nif [ "$1" = "-s" ]; then echo {kernel}; else /usr/bin/uname "$@"; fi\n'
    )
    # The root check: the script refuses to install a LaunchDaemon unprivileged.
    (d / "id").write_text(
        '#!/bin/sh\nif [ "$1" = "-u" ]; then echo 0; else /usr/bin/id "$@"; fi\n'
    )
    # Real dscl prints "NFSHomeDirectory: /Users/x"; the script parses field 2.
    (d / "dscl").write_text(f'#!/bin/sh\necho "NFSHomeDirectory: {home}"\n')
    for f in d.iterdir():
        f.chmod(0o755)
    return d


def _repo(
    tmp_path: Path,
    *,
    token: bool = True,
    claude: bool = True,
    api_key: bool = False,
    stored_login: bool = False,
) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copy(SCRIPT, root / "scripts" / SCRIPT.name)
    py = root / ".venv" / "bin" / "python"
    py.write_text("#!/bin/sh\nexit 0\n")
    py.chmod(0o755)
    (root / "agents.toml").write_text("")
    (root / "policy.toml").write_text("")
    # Not a real token: the point is that the script looks for the key and that
    # whatever is there does not end up in a world-readable plist.
    lines = ["SLACK_BOT_TOKEN=xoxb-test"]
    if token:
        lines.append("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-NOT-A-REAL-TOKEN")
    if api_key:
        lines.append("ANTHROPIC_API_KEY=sk-ant-api-NOT-A-REAL-KEY")
    (root / ".env").write_text("\n".join(lines) + "\n")

    home = tmp_path / "home"
    if claude:
        (home / ".local" / "bin").mkdir(parents=True)
        cli = home / ".local" / "bin" / "claude"
        cli.write_text("#!/bin/sh\nexit 0\n")
        cli.chmod(0o755)
    else:
        home.mkdir()
    if stored_login:
        # What `claude` or `/login` leaves behind, and what takes precedence
        # over anything in .env.
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / ".credentials.json").write_text("{}")
    return root, home


def _run(
    root: Path,
    home: Path,
    stubs: Path,
    *args: str,
    env_extra: dict | None = None,
) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ)
    env.update(
        PATH=f"{stubs}{os.pathsep}{env.get('PATH', '')}",
        HOME=str(home),
        USER="testuser",
        SUDO_USER="testuser",
    )
    # A None value removes the variable, which is the only way to reproduce an
    # invocation that is not `sudo` -- see the root-shell test below.
    for key, value in (env_extra or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        ["bash", str(root / "scripts" / SCRIPT.name), *args],
        capture_output=True, text=True, env=env, cwd=str(root),
    )


def _plist(out: str) -> dict:
    body = re.search(r"<\?xml.*?</plist>", out, re.S)
    assert body, f"no plist in output:\n{out}"
    # The dry run indents its preview by four spaces.
    xml = "\n".join(line[4:] if line.startswith("    ") else line
                    for line in body.group(0).splitlines())
    return plistlib.loads(xml.encode())


def test_the_plist_parses(tmp_path):
    root, home = _repo(tmp_path)
    r = _run(root, home, _stubs(tmp_path, home), "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    d = _plist(r.stdout)
    assert d["Label"] == "local.ricardi"
    assert d["WorkingDirectory"] == str(root)


def test_it_does_not_run_as_root(tmp_path):
    # Root to install, the invoking user to run. An agent that executes shell
    # commands on request from a chat channel must not be root.
    root, home = _repo(tmp_path)
    r = _run(root, home, _stubs(tmp_path, home), "--dry-run")
    d = _plist(r.stdout)
    assert d["UserName"] == "testuser"
    assert d["UserName"] != "root"


def test_the_token_never_reaches_the_plist(tmp_path):
    # /Library/LaunchDaemons is world-readable. The token stays in .env, which
    # Ricardi reads from WorkingDirectory itself.
    root, home = _repo(tmp_path)
    r = _run(root, home, _stubs(tmp_path, home), "--dry-run")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in r.stdout
    assert "NOT-A-REAL-TOKEN" not in r.stdout


def test_it_refuses_without_a_token(tmp_path):
    # The macOS-specific trap: Claude Code keeps credentials in the login
    # Keychain, and a LaunchDaemon has no login session to unlock it. Failing
    # here with an explanation beats a daemon that respawns forever.
    root, home = _repo(tmp_path, token=False)
    r = _run(root, home, _stubs(tmp_path, home), "--dry-run")
    assert r.returncode == 1
    assert "Keychain" in r.stdout
    assert "setup-token" in r.stdout


def test_it_refuses_when_the_cli_is_not_on_the_daemon_path(tmp_path):
    # A LaunchDaemon gets a bare PATH. If `claude` is somewhere else, the SDK
    # cannot spawn it and every turn fails at runtime instead of at install.
    root, home = _repo(tmp_path, claude=False)
    r = _run(root, home, _stubs(tmp_path, home), "--dry-run")
    assert r.returncode == 1
    assert "does not inherit your PATH" in r.stdout


def test_the_daemon_path_includes_the_users_local_bin(tmp_path):
    root, home = _repo(tmp_path)
    r = _run(root, home, _stubs(tmp_path, home), "--dry-run")
    d = _plist(r.stdout)
    # Resolved through dscl, not $HOME: under sudo they are different.
    assert f"{home}/.local/bin" in d["EnvironmentVariables"]["PATH"]


def test_it_restarts_and_does_not_spin(tmp_path):
    root, home = _repo(tmp_path)
    r = _run(root, home, _stubs(tmp_path, home), "--dry-run")
    d = _plist(r.stdout)
    assert d["RunAtLoad"] is True
    assert d["KeepAlive"] is True
    assert d["ThrottleInterval"] == 10


def test_it_writes_its_own_rotating_log(tmp_path):
    # StandardErrorPath grows until the disk is full, so it catches only
    # tracebacks from before logging exists; the real log rotates.
    root, home = _repo(tmp_path)
    r = _run(root, home, _stubs(tmp_path, home), "--dry-run")
    d = _plist(r.stdout)
    args = d["ProgramArguments"]
    assert "--log-file" in args
    assert args[args.index("--log-file") + 1] == f"{root}/state/service.log"
    assert d["StandardErrorPath"] == f"{root}/state/launchd-stderr.log"


def test_the_dry_run_touches_nothing(tmp_path):
    root, home = _repo(tmp_path)
    r = _run(root, home, _stubs(tmp_path, home), "--dry-run")
    assert "would write" in r.stdout
    assert not Path("/Library/LaunchDaemons/local.ricardi.plist").exists()
    assert not (root / "state").exists()


# --- the Linux branch ----------------------------------------------------
# Same stubs, kernel reported as Linux. systemctl has to look present for the
# preflight, but nothing is ever enabled: every test here is a dry run.


def _linux(tmp_path: Path, home: Path) -> Path:
    stubs = _stubs(tmp_path, home, kernel="Linux")
    (stubs / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    (stubs / "systemctl").chmod(0o755)
    return stubs


def _unit(out: str) -> dict[str, str]:
    """The unit file out of the preview, as key -> value."""
    body = re.search(r"would write \S+:\n(.*?)\nwould run:", out, re.S)
    assert body, f"no unit in output:\n{out}"
    pairs = {}
    for line in body.group(1).splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            pairs[k] = v
    return pairs


def test_linux_writes_a_user_unit(tmp_path):
    # A *user* unit, not a system one: it runs as you, with your ~/.claude,
    # and with no more access than you have.
    root, home = _repo(tmp_path, token=False)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    r = _run(root, home, _linux(tmp_path, home), "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert ".config/systemd/user/ricardi.service" in r.stdout
    assert "systemctl --user" in r.stdout


def test_linux_unit_points_at_the_venv(tmp_path):
    root, home = _repo(tmp_path, token=False)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    r = _run(root, home, _linux(tmp_path, home), "--dry-run")
    u = _unit(r.stdout)
    assert u["WorkingDirectory"] == str(root)
    assert u["ExecStart"].startswith(f"{root}/.venv/bin/python")
    assert u["Restart"] == "on-failure"


def test_linux_does_not_pass_log_file(tmp_path):
    # Deliberate asymmetry with Windows and macOS: journald already captures
    # stdout, so a second rotating log would just be a file to forget about.
    root, home = _repo(tmp_path, token=False)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    r = _run(root, home, _linux(tmp_path, home), "--dry-run")
    assert "--log-file" not in _unit(r.stdout)["ExecStart"]


def test_linux_enables_lingering(tmp_path):
    # Without it a user unit stops at logout, which is the whole requirement.
    root, home = _repo(tmp_path, token=False)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    r = _run(root, home, _linux(tmp_path, home), "--dry-run")
    assert "enable-linger" in r.stdout


def test_linux_accepts_a_token_instead_of_a_signed_in_home(tmp_path):
    # No ~/.claude is fine if a token is present -- that is how a fresh VM or
    # a container-less headless box gets going.
    root, home = _repo(tmp_path, token=True)
    r = _run(root, home, _linux(tmp_path, home), "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr


def test_linux_refuses_with_neither(tmp_path):
    root, home = _repo(tmp_path, token=False)
    r = _run(root, home, _linux(tmp_path, home), "--dry-run")
    assert r.returncode == 1
    assert "sign in" in r.stdout


def test_an_unknown_kernel_says_so_rather_than_guessing(tmp_path):
    root, home = _repo(tmp_path)
    r = _run(root, home, _stubs(tmp_path, home, kernel="SunOS"), "--dry-run")
    assert r.returncode == 2
    assert "Unsupported" in r.stderr
    assert "install-service.ps1" in r.stderr


# --- exactly one credential ----------------------------------------------
# An always-on host has nobody to answer a login prompt, so which credential
# is in play has to be unambiguous. Two at once is the failure that costs a
# month: the wrong one wins quietly and the right one looks fine.


def test_macos_accepts_an_api_key_instead_of_a_token(tmp_path):
    # ANTHROPIC_API_KEY has no scheduled expiry and no session for a login
    # elsewhere to revoke, so it is the better credential for a box nobody
    # signs into. Either one has to satisfy the preflight.
    root, home = _repo(tmp_path, token=False, api_key=True)
    r = _run(root, home, _stubs(tmp_path, home), "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    # Same rule as the OAuth token: /Library is world-readable.
    assert "ANTHROPIC_API_KEY" not in r.stdout
    assert "NOT-A-REAL-KEY" not in r.stdout


def test_it_refuses_a_credential_next_to_a_stored_login(tmp_path):
    root, home = _repo(tmp_path, token=True, stored_login=True)
    r = _run(root, home, _stubs(tmp_path, home), "--dry-run")
    assert r.returncode == 1
    assert "takes precedence" in r.stdout


def test_linux_refuses_a_credential_next_to_a_stored_login(tmp_path):
    # Likeliest here of anywhere: signing in is the documented Linux path, so
    # adding a token afterwards is how a box ends up holding both.
    root, home = _repo(tmp_path, token=True, stored_login=True)
    r = _run(root, home, _linux(tmp_path, home), "--dry-run")
    assert r.returncode == 1
    assert "takes precedence" in r.stdout


def test_it_refuses_two_credentials_in_env(tmp_path):
    root, home = _repo(tmp_path, token=True, api_key=True)
    r = _run(root, home, _stubs(tmp_path, home), "--dry-run")
    assert r.returncode == 1
    assert "conflict" in r.stdout


def test_it_refuses_when_it_would_run_as_root(tmp_path):
    """A root shell is not `sudo`, and only `sudo` sets SUDO_USER.

    `sudo -i`, `su -`, a provisioning script and CI all leave it unset, so
    RUN_USER falls through to USER -- which in a root shell is root. The
    plist would then carry UserName=root and launchd would run a
    chat-driven shell agent as root, which is the one outcome this script
    exists to prevent.

    `test_it_does_not_run_as_root` cannot see this, because the harness it
    uses always sets SUDO_USER. That is why the bug survived having a test
    named after it.
    """
    root, home = _repo(tmp_path)
    r = _run(
        root, home, _stubs(tmp_path, home), "--dry-run",
        env_extra={"SUDO_USER": None, "USER": "root"},
    )
    assert r.returncode != 0, "installed anyway: " + r.stdout + r.stderr
    assert "root" in (r.stdout + r.stderr).lower()
    # And it must not have got as far as producing one.
    assert "UserName" not in r.stdout


def test_the_example_env_ships_no_active_credential():
    """`have_env_key` matches a bare KEY=value, so a placeholder counts.

    An example shipping one breaks the new preflight both ways: uncomment
    the other credential and the installer refuses because "both are set";
    change nothing and it installs a daemon whose credential is the literal
    `sk-ant-...`, which is the failure that check exists to produce.
    """
    names = ("ANTHROPIC_API_KEY=", "CLAUDE_CODE_OAUTH_TOKEN=")
    body = (SCRIPT.parents[1] / ".env.example").read_text(encoding="utf-8")
    active = [
        line
        for line in body.splitlines()
        if any(line.lstrip().startswith(n) for n in names)
        and line.split("=", 1)[1].strip()
    ]
    assert active == [], f"active credential lines in .env.example: {active}"
