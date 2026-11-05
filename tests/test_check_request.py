"""`ricardi check-request`: the checker, reachable by the person applying it.

`provision.py` has refused bad requests since it was written, but until this
command the only caller was pytest -- so the admin standing on the box with a
merged request in front of them had no way to run the check, and the owner was
told to install the test dependencies to check their own file.

Two things here matter beyond the happy path. Every file is reported, not just
the first, because a reviewer wants the whole list in one go. And a catalogue
that cannot be read degrades to a printed note rather than an error: agents.toml
is not in the repository, so CI and a fresh clone have none, and a collision
check that silently did not happen is the failure this whole directory exists to
prevent.
"""

from __future__ import annotations

from ricardi.cli import main


MINIMAL = """
workspace = "acme"
name = "project1"

[owner]
slack = "U00000000000"
email = "owner@example.com"
git = "owner-login"

[limits]
max_budget_usd = 2.0
max_turns = 60
"""


def write(dirpath, body=MINIMAL, filename="project1.toml"):
    path = dirpath / filename
    path.write_text(body, encoding="utf-8")
    return path


def check(tmp_path, *argv: str) -> int:
    """Run the command with no catalogue, so collisions are out of the way.

    `--agents` goes after the subcommand deliberately: that is where someone
    types it, and the parser has to accept it there.
    """
    return main(["--env", "nope", "check-request", *argv,
                 "--agents", str(tmp_path / "nope.toml")])


def test_a_good_request_passes(tmp_path, capsys):
    path = write(tmp_path)
    assert check(tmp_path, str(path)) == 0
    out = capsys.readouterr().out
    assert "project1" in out
    assert "REFUSED" not in out


def test_a_refused_request_exits_non_zero(tmp_path, capsys):
    """The exit code is the whole point: CI and a deploy gate read that."""
    path = write(tmp_path, MINIMAL.replace('max_turns = 60', 'max_turns = 0'))
    assert check(tmp_path, str(path)) == 1
    assert "max_turns" in capsys.readouterr().out


def test_the_reason_is_the_one_provision_already_gives(tmp_path, capsys):
    body = MINIMAL + """
[[invitee]]
slack = "U11111111111"
email = "them@example.com"
git = "them-login"
tier = "exec"
"""
    assert check(tmp_path, str(write(tmp_path, body))) == 1
    out = capsys.readouterr().out
    # Not a code, not a line number: the sentence provision.py wrote, which
    # explains why there is no exec tier.
    assert "scope of the session" in out


def test_the_filename_has_to_match_the_name_asked_for(tmp_path, capsys):
    """The filename is how a reviewer finds the request in a diff."""
    path = write(tmp_path, filename="something-else.toml")
    assert check(tmp_path, str(path)) == 1
    out = capsys.readouterr().out
    assert "project1" in out and "something-else" in out


def test_the_shipped_template_passes_the_command(tmp_path, capsys):
    """The obvious first thing to try has to answer about the template.

    `provision.example.toml` is the file people copy, so the name inside it is
    the session it demonstrates rather than its own filename -- but everything
    else in it is checked, because it is what a request gets written from.
    """
    assert check(tmp_path, "provision.example.toml") == 0
    assert "REFUSED" not in capsys.readouterr().out


def test_a_mismatched_name_is_still_refused_outside_the_template(tmp_path, capsys):
    """The exemption is the `.example.toml` convention, not any dotted name."""
    assert check(tmp_path, str(write(tmp_path, filename="project1.draft.toml"))) == 1
    assert "project1.draft.toml" in capsys.readouterr().out


def test_every_file_is_reported_not_only_the_first(tmp_path, capsys):
    one = write(tmp_path, MINIMAL.replace("max_turns = 60", "max_turns = 0"),
                filename="project1.toml")
    two = write(tmp_path, MINIMAL.replace('name = "project1"', 'name = "project2"')
                .replace("max_budget_usd = 2.0", "max_budget_usd = 0"),
                filename="project2.toml")
    assert check(tmp_path, str(one), str(two)) == 1
    out = capsys.readouterr().out
    assert "max_turns" in out and "max_budget_usd" in out
    assert "2 of 2" in out


def test_a_directory_checks_every_request_in_it(tmp_path, capsys):
    write(tmp_path, filename="project1.toml")
    write(tmp_path, MINIMAL.replace('name = "project1"', 'name = "project2"'),
          filename="project2.toml")
    assert check(tmp_path, str(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "project1" in out and "project2" in out


def test_a_directory_with_no_requests_says_so(tmp_path, capsys):
    """A glob over nothing is a green run that checked nothing."""
    empty = tmp_path / "requests"
    empty.mkdir()
    assert check(tmp_path, str(empty)) == 0
    assert "no requests" in capsys.readouterr().out.lower()


def test_a_directory_that_is_not_there_is_refused(tmp_path, capsys):
    assert check(tmp_path, str(tmp_path / "requests")) == 1
    assert "No request" in capsys.readouterr().out


def test_two_requests_cannot_ask_for_the_same_name(tmp_path, capsys):
    """Checked across the arguments, not file by file: the collision is between
    two files in the same pull request, which is where it will actually happen."""
    one = write(tmp_path, filename="project1.toml")
    second = tmp_path / "second"
    second.mkdir()
    two = write(second, filename="project1.toml")
    assert check(tmp_path, str(one), str(two)) == 1
    assert "already in use" in capsys.readouterr().out


def test_the_catalogue_bounds_the_names(tmp_path, capsys):
    agents = tmp_path / "agents.toml"
    agents.write_text(
        f'[[agent]]\nname = "project1"\ncwd = "{tmp_path.as_posix()}"\n',
        encoding="utf-8",
    )
    path = write(tmp_path, filename="project1.toml")
    # The flag before the subcommand, the other form people type.
    assert main(["--env", "nope", "--agents", str(agents),
                 "check-request", str(path)]) == 1
    assert "already in use" in capsys.readouterr().out


def test_an_unreadable_catalogue_is_reported_and_not_fatal(tmp_path, capsys):
    """Fails open, so it has to say so.

    agents.toml names real directories on one machine, so it is gitignored and
    absent in CI and in a fresh clone. Refusing here would mean the gate could
    never run; swallowing it would mean a collision silently goes unchecked.
    """
    path = write(tmp_path)
    assert check(tmp_path, str(path)) == 0
    out = capsys.readouterr().out
    assert "not checking" in out.lower()
    assert "nope.toml" in out


def test_a_ceiling_can_be_applied_from_the_command_line(tmp_path, capsys):
    """The admin's ceiling is not in any file, so it is an argument."""
    path = write(tmp_path)
    assert check(tmp_path, str(path), "--max-budget-usd", "1.0") == 1
    assert "ceiling" in capsys.readouterr().out


def test_the_ceiling_is_not_applied_unless_asked_for(tmp_path):
    assert check(tmp_path, str(write(tmp_path))) == 0
