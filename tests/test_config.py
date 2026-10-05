"""The catalogue is the security boundary, so it is strict on purpose."""

import pytest

from ricardi import config


def write(tmp_path, body):
    path = tmp_path / "agents.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_full_catalogue_loads(tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    path = write(
        tmp_path,
        f"""
[ricardi]
ack_after = 2.5

[[agent]]
name = "scout"
description = "reads things"
cwd = {str(work)!r}
system_prompt = "Be brief."
""",
    )
    settings = config.load(path)
    assert settings.ack_after == 2.5
    assert len(settings.agents) == 1
    agent = settings.agents[0]
    assert agent.name == "scout"
    # Defaults to a state directory beside the catalogue.
    assert settings.state_dir == tmp_path / "state"


def test_a_missing_file_says_so(tmp_path):
    with pytest.raises(config.ConfigError, match="No agent catalogue"):
        config.load(tmp_path / "nope.toml")


def test_a_catalogue_with_no_agents_is_refused(tmp_path):
    path = write(tmp_path, "[ricardi]\nack_after = 1.0\n")
    with pytest.raises(config.ConfigError, match="no \\[\\[agent\\]\\]"):
        config.load(path)


def test_an_agent_without_a_name_is_refused(tmp_path):
    path = write(tmp_path, '[[agent]]\ncwd = "."\n')
    with pytest.raises(config.ConfigError, match="no name"):
        config.load(path)


def test_an_unaddressable_name_is_refused(tmp_path):
    # Spaces and punctuation cannot be typed in "tell <agent> ..." reliably.
    path = write(tmp_path, '[[agent]]\nname = "my scout"\ncwd = "."\n')
    with pytest.raises(config.ConfigError, match="must be letters"):
        config.load(path)


def test_a_duplicate_name_is_refused(tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    path = write(
        tmp_path,
        f"""
[[agent]]
name = "scout"
cwd = {str(work)!r}

[[agent]]
name = "Scout"
cwd = {str(work)!r}
""",
    )
    # Two agents answering to one name would make addressing ambiguous.
    with pytest.raises(config.ConfigError, match="twice"):
        config.load(path)


def test_an_agent_without_a_cwd_is_refused(tmp_path):
    path = write(tmp_path, '[[agent]]\nname = "scout"\n')
    with pytest.raises(config.ConfigError, match="needs a cwd"):
        config.load(path)


def test_a_cwd_that_is_not_there_is_refused(tmp_path):
    missing = tmp_path / "gone"
    path = write(tmp_path, f'[[agent]]\nname = "scout"\ncwd = {str(missing)!r}\n')
    # Caught at load rather than when someone in Slack first asks.
    with pytest.raises(config.ConfigError, match="not a directory"):
        config.load(path)


def test_broken_toml_names_the_file(tmp_path):
    path = write(tmp_path, "[[agent]\nname = ")
    with pytest.raises(config.ConfigError, match="could not be read"):
        config.load(path)


def test_a_catalogue_that_still_grants_tools_is_refused(tmp_path):
    """These moved to policy, and silently ignoring them would be worse."""
    work = tmp_path / "repo"
    work.mkdir()
    for moved in ("allowed_tools = [\"Read\"]", 'permission_mode = "acceptEdits"'):
        path = write(
            tmp_path,
            f"""
[[agent]]
name = "scout"
cwd = {str(work)!r}
{moved}
""",
        )
        with pytest.raises(config.ConfigError, match="policy.toml"):
            config.load(path)


def test_budget_ceilings_load(tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    path = write(
        tmp_path,
        f"""
[[agent]]
name = "bench"
cwd = {str(work)!r}
max_budget_usd = 2.5
max_turns = 40
""",
    )
    agent = config.load(path).agents[0]
    assert agent.max_budget_usd == 2.5
    assert agent.max_turns == 40


def test_no_ceiling_is_the_default_and_means_none(tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    path = write(tmp_path, f'[[agent]]\nname = "s"\ncwd = {str(work)!r}\n')
    agent = config.load(path).agents[0]
    assert agent.max_budget_usd == 0.0
    assert agent.max_turns == 0


def test_a_negative_ceiling_is_refused(tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    path = write(
        tmp_path,
        f'[[agent]]\nname = "s"\ncwd = {str(work)!r}\nmax_budget_usd = -1\n',
    )
    with pytest.raises(config.ConfigError, match="negative"):
        config.load(path)


def test_the_shipped_example_sets_a_ceiling_on_every_agent():
    """On a machine left running, "no limit" should be chosen, not forgotten.

    Read rather than loaded: the example points at scratch directories that do
    not exist on a fresh clone, and loading it would make this test pass only
    on a machine where someone happened to create them.
    """
    import tomllib

    with open("agents.example.toml", "rb") as fh:
        raw = tomllib.load(fh)

    assert raw["agent"], "the example declares no agents"
    for entry in raw["agent"]:
        assert entry.get("max_budget_usd", 0) > 0, entry["name"]
        assert entry.get("max_turns", 0) > 0, entry["name"]
