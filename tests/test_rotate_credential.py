"""The rotation workflow's own shell, run against a fake box.

Rotating a credential rewrites `/opt/ricardi/.env` -- a file no deploy will
replace and nobody can get at without a session on the box, which is the
whole reason this workflow exists. A rewrite that drops the Slack tokens, or
mangles a key with a `&` in it, is therefore not a failed workflow but an
outage that needs hands on the machine it was built to avoid touching.

So these tests execute the steps exactly as shipped: the YAML is parsed, the
`run:` script is handed to bash with a temporary DEPLOY_DIR, and the file it
leaves behind is read back. Nothing is retyped here, which is the point -- a
copy of the script would pass while the shipped one rotted.

Docker is never reached: the steps under test are the ones before it.
"""

import shlex
import tempfile
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "workflows"
    / "rotate-credential.yml"
)

REFUSE = "Refuse a configuration this key cannot win"
WRITE = "Write it, keeping a way back"

def _usable_shell() -> str:
    """Why these tests cannot run here, or "" when they can.

    Probed rather than assumed. `which bash` says a shell exists, which is a
    different claim from "a shell this process starts can read a directory
    this process just made" -- and that is the claim every test here rests
    on. Sandboxes that give a child process its own filesystem view answer
    the first question yes and the second no, and the failure arrives as
    twenty puzzling red tests rather than one honest skip.
    """
    for tool in ("bash", "awk"):
        if shutil.which(tool) is None:
            return f"no {tool} on this platform"
    with tempfile.TemporaryDirectory() as box:
        probe = f'cd "{Path(box).as_posix()}" && printf reached > marker\n'
        try:
            subprocess.run(
                ["bash", "-s"], input=probe.encode("utf-8"), capture_output=True
            )
            if (Path(box) / "marker").read_text(encoding="utf-8") == "reached":
                return ""
        except OSError:
            pass
    return "a bash child of this process cannot reach this process's files"


UNUSABLE = _usable_shell()
pytestmark = pytest.mark.skipif(bool(UNUSABLE), reason=UNUSABLE or "shell is usable")


def _script(step_name: str) -> str:
    """The shipped `run:` body for one step, by name.

    Named lookup rather than an index: a step inserted above this one must not
    silently move the test onto a different script. A rename fails here, which
    is the correct amount of noise for renaming the thing under test.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["rotate"]["steps"]
    for step in steps:
        if step.get("name") == step_name:
            return step["run"]
    raise AssertionError(
        f"no step named {step_name!r} in {WORKFLOW.name}; "
        f"found {[s.get('name') for s in steps]}"
    )


def _posix(path: Path) -> str:
    """A path this shell can actually reach.

    Git Bash on Windows is a POSIX shell holding a Windows path: backslashes
    are escape characters to it, so `C:\\Users\\...` arrives with the
    separators eaten. Forward slashes are understood on both platforms.
    """
    return str(path).replace("\\", "/")


def _run(step_name: str, box: Path, **env: str) -> subprocess.CompletedProcess:
    """Run one shipped step with the inputs the runner would give it."""
    inputs = {"DEPLOY_DIR": _posix(box), "GITHUB_OUTPUT": _posix(box / "gh-output")}
    inputs.update(env)
    # Exported by a prelude on stdin rather than passed as a subprocess
    # environment. Both reach the script as environment variables, which is
    # what `awk`'s ENVIRON and every `$VAR` here read; the prelude also works
    # under a sandbox that refuses to hand a child a custom environment.
    prelude = "".join(
        f"export {name}={shlex.quote(value)}\n" for name, value in inputs.items()
    )
    # The script goes in on stdin rather than as a file, so there is no second
    # path for the shell to misread -- and nothing left on disk afterwards.
    #
    # Bytes, not text=True: text mode rewrites every \n on the way in to the
    # platform's line ending, and a shell handed `set -euo pipefail\r` reports
    # an invalid option name -- which looks exactly like a broken script.
    done = subprocess.run(
        ["bash", "-s"],
        input=(prelude + _script(step_name)).encode("utf-8"),
        capture_output=True,
    )
    return subprocess.CompletedProcess(
        done.args,
        done.returncode,
        done.stdout.decode("utf-8", "replace"),
        done.stderr.decode("utf-8", "replace"),
    )


# A .env in the shape install-service.sh leaves behind: the Slack tokens that
# must survive, a commented example line that must not be counted as the key,
# and the key itself in the middle rather than at either end.
ENV_WITH_KEY = """\
SLACK_APP_TOKEN=xapp-old
SLACK_BOT_TOKEN=xoxb-old
# ANTHROPIC_API_KEY=
ANTHROPIC_API_KEY=sk-ant-old
# RICARDI_STATE_DIR=./state
"""

ENV_WITHOUT_KEY = """\
SLACK_APP_TOKEN=xapp-old
SLACK_BOT_TOKEN=xoxb-old
"""


@pytest.fixture
def box(tmp_path: Path) -> Path:
    (tmp_path / "claude-config").mkdir()
    (tmp_path / ".env").write_text(ENV_WITH_KEY, newline="\n", encoding="utf-8")
    return tmp_path


def test_a_healthy_env_is_accepted(box: Path):
    done = _run(REFUSE, box)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ANTHROPIC_API_KEY lines in .env: 1" in done.stdout


def test_a_missing_env_is_refused_rather_than_created(box: Path):
    (box / ".env").unlink()
    done = _run(REFUSE, box)
    assert done.returncode == 1
    assert "never installed" in done.stdout


def test_an_oauth_token_in_the_same_file_is_refused(box: Path):
    # install-service.sh refuses both at once, and so must this: the one that
    # wins is not the one the person rotating thinks they changed.
    with (box / ".env").open("a", newline="\n") as handle:
        handle.write("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-x\n")
    done = _run(REFUSE, box)
    assert done.returncode == 1
    assert "conflict" in done.stdout


def test_a_commented_or_empty_oauth_line_is_not_a_conflict(box: Path):
    with (box / ".env").open("a", newline="\n") as handle:
        handle.write("# CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-x\nCLAUDE_CODE_OAUTH_TOKEN=\n")
    done = _run(REFUSE, box)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_stored_login_is_refused_because_it_would_swallow_the_rotation(box: Path):
    (box / "claude-config" / ".credentials.json").write_text("{}", encoding="utf-8")
    done = _run(REFUSE, box)
    assert done.returncode == 1
    assert "takes precedence" in done.stdout


def test_the_stored_login_is_looked_for_where_env_says_it_is_mounted(box: Path):
    # compose mounts ${RICARDI_CLAUDE_DIR:-./claude-config} at /config/claude.
    # Checking only the default would miss a shadowing login on every box that
    # overrode it -- and report success.
    elsewhere = box / "somewhere-else"
    elsewhere.mkdir()
    (elsewhere / ".credentials.json").write_text("{}", encoding="utf-8")
    with (box / ".env").open("a", newline="\n") as handle:
        handle.write(f"RICARDI_CLAUDE_DIR={elsewhere}\n")
    done = _run(REFUSE, box)
    assert done.returncode == 1
    assert "takes precedence" in done.stdout


def test_two_key_lines_are_refused_rather_than_guessed(box: Path):
    with (box / ".env").open("a", newline="\n") as handle:
        handle.write("ANTHROPIC_API_KEY=sk-ant-other\n")
    done = _run(REFUSE, box)
    assert done.returncode == 1
    assert "will not guess" in done.stdout


def test_no_key_line_is_refused_unless_adding_one_was_asked_for(box: Path):
    (box / ".env").write_text(ENV_WITHOUT_KEY, newline="\n", encoding="utf-8")
    refused = _run(REFUSE, box)
    assert refused.returncode == 1
    assert "allow_add=true" in refused.stdout

    allowed = _run(REFUSE, box, ALLOW_ADD="true")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr


def test_the_key_is_replaced_and_everything_else_survives(box: Path):
    done = _run(WRITE, box, NEW_KEY="sk-ant-new")
    assert done.returncode == 0, done.stdout + done.stderr

    after = (box / ".env").read_text(encoding="utf-8").splitlines()
    assert after == [
        "SLACK_APP_TOKEN=xapp-old",
        "SLACK_BOT_TOKEN=xoxb-old",
        "# ANTHROPIC_API_KEY=",
        "ANTHROPIC_API_KEY=sk-ant-new",
        "# RICARDI_STATE_DIR=./state",
    ]


def test_the_previous_file_is_kept_as_the_way_back(box: Path):
    _run(WRITE, box, NEW_KEY="sk-ant-new")
    assert (box / ".env.bak").read_text(encoding="utf-8") == ENV_WITH_KEY
    assert not (box / ".env.next").exists(), "the temporary file outlived the rename"


def test_the_write_step_reports_that_it_wrote(box: Path):
    # The rollback step is gated on this output rather than on the existence
    # of .env.bak, so that a run which failed before writing cannot restore a
    # previous rotation's file.
    _run(WRITE, box, NEW_KEY="sk-ant-new")
    assert "rewrote=yes" in (box / "gh-output").read_text(encoding="utf-8")


def test_a_key_is_added_when_the_file_had_none(box: Path):
    (box / ".env").write_text(ENV_WITHOUT_KEY, newline="\n", encoding="utf-8")
    done = _run(WRITE, box, NEW_KEY="sk-ant-new")
    assert done.returncode == 0, done.stdout + done.stderr
    after = (box / ".env").read_text(encoding="utf-8").splitlines()
    assert after[-1] == "ANTHROPIC_API_KEY=sk-ant-new"
    assert after[:2] == ["SLACK_APP_TOKEN=xapp-old", "SLACK_BOT_TOKEN=xoxb-old"]


@pytest.mark.parametrize(
    "key",
    [
        "sk-ant-with&ampersand",
        "sk-ant-with$dollar",
        "sk-ant-with\\backslash",
        'sk-ant-with"quote',
        "sk-ant-with spaces",
        "sk-ant-with#hash",
    ],
)
def test_a_key_with_shell_or_awk_punctuation_in_it_lands_verbatim(box: Path, key: str):
    # `&` is the substitution character in awk's sub(), `$` and `\` are the
    # shell's, and a key written with any of them mangled is a box that cannot
    # answer and cannot be reached. The value travels through ENVIRON for
    # exactly this reason; this is the assertion that it stays intact.
    done = _run(WRITE, box, NEW_KEY=key)
    assert done.returncode == 0, done.stdout + done.stderr
    after = (box / ".env").read_text(encoding="utf-8").splitlines()
    assert f"ANTHROPIC_API_KEY={key}" in after


def test_the_key_is_never_printed_by_the_step_that_writes_it(box: Path):
    # A workflow log is readable by anyone who can read the repository, and
    # this one is public.
    done = _run(WRITE, box, NEW_KEY="sk-ant-do-not-print-me")
    assert "do-not-print-me" not in done.stdout + done.stderr
