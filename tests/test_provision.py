"""A provisioning request, checked before a human reads it.

An owner cannot ask for a session in Slack, because the thing a request
produces is the channel. So a request is a file in a pull request: the owner
writes it, CI checks it, and an admin reviews a diff -- the same shape as the
file that grants everything else here.

These tests are the contract. Everything they refuse, they refuse because a
wrong value would either be applied silently or waste the admin's review.
"""

from __future__ import annotations

import pytest

from ricardi import provision


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


def write(tmp_path, body, filename="project1.toml"):
    path = tmp_path / filename
    path.write_text(body, encoding="utf-8")
    return path


def test_a_minimal_request_loads(tmp_path):
    req = provision.load(write(tmp_path, MINIMAL))
    assert req.workspace == "acme"
    assert req.name == "project1"
    assert req.owner.git == "owner-login"
    assert req.invitees == ()
    assert req.repo is None


def test_an_empty_invitee_list_is_fine(tmp_path):
    """"Could be just 0" -- a session with nobody else in it yet is normal."""
    assert provision.load(write(tmp_path, MINIMAL)).invitees == ()


def test_a_missing_file_says_so(tmp_path):
    with pytest.raises(provision.RequestError, match="No request"):
        provision.load(tmp_path / "nope.toml")


def test_broken_toml_says_so(tmp_path):
    with pytest.raises(provision.RequestError, match="could not be read"):
        provision.load(write(tmp_path, "name = "))


# ------------------------------------------------------- the name


def test_the_name_must_be_addressable(tmp_path):
    """It becomes a name humans type in Slack, so the same rule as the catalogue."""
    body = MINIMAL.replace('name = "project1"', 'name = "pro ject"')
    with pytest.raises(provision.RequestError, match="letters, digits"):
        provision.load(write(tmp_path, body))


def test_a_name_this_accepts_is_a_name_the_catalogue_accepts(tmp_path):
    """The two validators must not drift.

    A request that passes review and is then refused by `config.load` at the
    restart that would have applied it has wasted everyone's time. So this
    drives the real catalogue loader rather than restating its rule.
    """
    from ricardi import config

    cwd = tmp_path / "checkout"
    cwd.mkdir()
    for name in ("ok-name", "ok_name", "name123"):
        body = MINIMAL.replace('name = "project1"', f'name = "{name}"')
        assert provision.load(write(tmp_path, body)).name == name

        catalogue = tmp_path / "agents.toml"
        catalogue.write_text(
            f'[[agent]]\nname = "{name}"\ncwd = "{cwd.as_posix()}"\n', encoding="utf-8"
        )
        assert config.load(catalogue).agents[0].name == name


def test_a_reserved_name_is_refused(tmp_path):
    """`ricardi` and `setup` mean something to the system already."""
    body = MINIMAL.replace('name = "project1"', 'name = "setup"')
    with pytest.raises(provision.RequestError, match="reserved"):
        provision.load(write(tmp_path, body))


def test_a_name_already_in_use_is_refused(tmp_path):
    with pytest.raises(provision.RequestError, match="already"):
        provision.load(write(tmp_path, MINIMAL), existing_names=("project1",))


def test_an_existing_name_is_matched_regardless_of_case(tmp_path):
    with pytest.raises(provision.RequestError, match="already"):
        provision.load(write(tmp_path, MINIMAL), existing_names=("PROJECT1",))


# ------------------------------------------------------- people


def test_the_owner_needs_a_git_identity(tmp_path):
    """The Slack-to-git join is collected here because the owner knows it.

    Nothing else can work it out later: GitHub will not resolve a user by
    email unless it is public.
    """
    body = MINIMAL.replace('git = "owner-login"\n', "")
    with pytest.raises(provision.RequestError, match="git"):
        provision.load(write(tmp_path, body))


def test_the_owner_needs_a_slack_id_and_an_email(tmp_path):
    body = MINIMAL.replace('slack = "U00000000000"\n', "")
    with pytest.raises(provision.RequestError, match="slack"):
        provision.load(write(tmp_path, body))


def test_an_invitee_needs_every_field(tmp_path):
    body = MINIMAL + """
[[invitee]]
slack = "U11111111111"
email = "them@example.com"
tier = "read"
"""
    with pytest.raises(provision.RequestError, match="git"):
        provision.load(write(tmp_path, body))


def test_an_invitee_is_loaded_with_their_tier(tmp_path):
    body = MINIMAL + """
[[invitee]]
slack = "U11111111111"
email = "them@example.com"
git = "them-login"
tier = "read"
"""
    (one,) = provision.load(write(tmp_path, body)).invitees
    assert (one.email, one.git, one.tier) == ("them@example.com", "them-login", "read")


def test_a_tier_outside_the_three_is_refused(tmp_path):
    body = MINIMAL + """
[[invitee]]
slack = "U11111111111"
email = "them@example.com"
git = "them-login"
tier = "admin"
"""
    with pytest.raises(provision.RequestError, match="tier"):
        provision.load(write(tmp_path, body))


def test_exec_is_refused_with_the_reason(tmp_path):
    """`exec` reads like a tier and is not one: it is the name's scope.

    Refusing it with that sentence is cheaper than letting someone request it
    and discover later that it did nothing.
    """
    body = MINIMAL + """
[[invitee]]
slack = "U11111111111"
email = "them@example.com"
git = "them-login"
tier = "exec"
"""
    with pytest.raises(provision.RequestError, match="scope of the session"):
        provision.load(write(tmp_path, body))


def test_the_same_person_cannot_be_listed_twice(tmp_path):
    body = MINIMAL + """
[[invitee]]
slack = "U11111111111"
email = "them@example.com"
git = "them-login"
tier = "read"

[[invitee]]
slack = "U11111111111"
email = "other@example.com"
git = "other-login"
tier = "write"
"""
    with pytest.raises(provision.RequestError, match="twice"):
        provision.load(write(tmp_path, body))


def test_the_owner_cannot_also_be_an_invitee(tmp_path):
    """The owner's tier comes from owning it. Listing them again is either a
    mistake or an attempt to give themselves a different one."""
    body = MINIMAL + """
[[invitee]]
slack = "U00000000000"
email = "owner@example.com"
git = "owner-login"
tier = "read"
"""
    with pytest.raises(provision.RequestError, match="owner"):
        provision.load(write(tmp_path, body))


def test_two_people_cannot_share_an_email(tmp_path):
    """The roster is keyed on email, so two people with one email are one
    principal, and rules written about them would apply to both."""
    body = MINIMAL + """
[[invitee]]
slack = "U11111111111"
email = "shared@example.com"
git = "one-login"
tier = "read"

[[invitee]]
slack = "U22222222222"
email = "shared@example.com"
git = "two-login"
tier = "read"
"""
    with pytest.raises(provision.RequestError, match="email"):
        provision.load(write(tmp_path, body))


# ------------------------------------------------------- limits


def test_limits_are_required(tmp_path):
    body = MINIMAL.split("[limits]")[0]
    with pytest.raises(provision.RequestError, match="limits"):
        provision.load(write(tmp_path, body))


def test_a_budget_of_zero_is_refused(tmp_path):
    """`agents.toml` lets 0 mean "no ceiling", deliberately. A *request* may
    not: an unbounded session is an admin's decision, not an owner's default."""
    body = MINIMAL.replace("max_budget_usd = 2.0", "max_budget_usd = 0")
    with pytest.raises(provision.RequestError, match="above zero"):
        provision.load(write(tmp_path, body))


def test_turns_of_zero_are_refused(tmp_path):
    """Same reason as the budget: a turn ceiling nobody set is not a ceiling."""
    body = MINIMAL.replace("max_turns = 60", "max_turns = 0")
    with pytest.raises(provision.RequestError, match="above zero"):
        provision.load(write(tmp_path, body))


def test_a_budget_over_the_ceiling_is_refused(tmp_path):
    body = MINIMAL.replace("max_budget_usd = 2.0", "max_budget_usd = 500")
    with pytest.raises(provision.RequestError, match="ceiling"):
        provision.load(write(tmp_path, body), max_budget_usd=10.0)


# ------------------------------------------------------- the repository


def test_a_repo_is_optional(tmp_path):
    assert provision.load(write(tmp_path, MINIMAL)).repo is None


def test_a_repo_is_loaded(tmp_path):
    body = MINIMAL + """
[repo]
name = "acme/project1"
create_if_missing = true
"""
    repo = provision.load(write(tmp_path, body)).repo
    assert (repo.name, repo.create_if_missing) == ("acme/project1", True)


def test_a_repo_needs_an_owner_and_a_name(tmp_path):
    body = MINIMAL + """
[repo]
name = "project1"
"""
    with pytest.raises(provision.RequestError, match="owner/repo"):
        provision.load(write(tmp_path, body))


# ------------------------------------------------------- typos


def test_an_unknown_top_level_key_is_refused(tmp_path):
    """Refused rather than ignored, for the same reason a bad flag is: a
    setting that looks like it asks for something and silently does not is
    worse than one that fails."""
    body = MINIMAL + '\nchannel_is_public = true\n'
    with pytest.raises(provision.RequestError, match="don't understand"):
        provision.load(write(tmp_path, body))


def test_an_unknown_key_on_a_person_is_refused(tmp_path):
    body = MINIMAL + """
[[invitee]]
slack = "U11111111111"
email = "them@example.com"
git = "them-login"
teir = "read"
"""
    with pytest.raises(provision.RequestError, match="don't understand"):
        provision.load(write(tmp_path, body))


# ------------------------------------------------------- the shipped example


def test_the_example_request_is_valid():
    """The file people copy has to pass the checker they will be judged by."""
    req = provision.load("provision.example.toml")
    assert req.name
    assert req.owner.git
    assert req.invitees


# ------------------------------------------------------- the CI gate


def test_every_request_in_the_repo_is_valid():
    """The gate that makes the pull-request flow real.

    Nothing to check until someone opens a request, and that is the correct
    answer rather than a vacuous one -- but the directory is asserted to exist,
    because a glob pointing at nothing passes for the wrong reason, which this
    repo has been bitten by before.
    """
    from pathlib import Path

    here = Path(__file__).resolve().parent.parent / "requests"
    assert here.is_dir(), f"{here} is missing, so this test checks nothing"

    names: list[str] = []
    for one in sorted(here.glob("*.toml")):
        request = provision.load(one, existing_names=tuple(names))
        # The filename is how a reviewer finds it in a diff, so it has to
        # match what it asks for.
        assert one.stem == request.name, f"{one.name} asks for {request.name!r}"
        names.append(request.name)
