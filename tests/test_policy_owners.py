import pytest

from ricardi.policy import PolicyError, load

ONE_ASK_ALLOW = '[[rule]]\neffect="allow"\nactions=["ask"]\n'


@pytest.fixture
def written_toml(tmp_path):
    def write(text: str):
        path = tmp_path / "policy.toml"
        path.write_text(text, encoding="utf-8")
        return path

    return write


def test_an_owner_entry_binds_one_person_to_one_name(written_toml):
    p = load(written_toml(ONE_ASK_ALLOW + '\n[[owner]]\nagent="n1"\nemail="Owner@Example.COM"\n'))
    assert p.owns("owner@example.com", "n1") is True
    assert p.owns("owner@example.com", "n2") is False
    assert p.owns("someone@example.com", "n1") is False


def test_ownership_is_normalised_on_both_sides(written_toml):
    p = load(written_toml(ONE_ASK_ALLOW + '\n[[owner]]\nagent="N1"\nemail=" owner@example.com "\n'))
    assert p.owns("OWNER@EXAMPLE.COM", "n1") is True
    assert p.owns(" owner@example.com", "N1") is True
    assert p.owners == {"n1": ("owner@example.com",)}


def test_two_owners_of_one_name_are_both_owners(written_toml):
    p = load(
        written_toml(
            ONE_ASK_ALLOW
            + '\n[[owner]]\nagent="n1"\nemail="a@example.com"\n'
            + '\n[[owner]]\nagent="n1"\nemail="b@example.com"\n'
        )
    )
    assert p.owns("a@example.com", "n1") and p.owns("b@example.com", "n1")
    assert p.owners == {"n1": ("a@example.com", "b@example.com")}


def test_an_owner_entry_missing_a_key_is_a_policy_error(written_toml):
    for toml in (
        '[[owner]]\nagent="n1"\n',
        '[[owner]]\nemail="a@b.com"\n',
        '[[owner]]\nagent="  "\nemail="a@b.com"\n',
        '[[owner]]\nagent="n1"\nemail=""\n',
    ):
        with pytest.raises(PolicyError, match="owner"):
            load(written_toml(ONE_ASK_ALLOW + "\n" + toml))


def test_an_owner_error_names_the_file(written_toml):
    path = written_toml(ONE_ASK_ALLOW + '\n[[owner]]\nagent="n1"\n')
    with pytest.raises(PolicyError, match="policy.toml"):
        load(path)


def test_a_policy_with_no_owner_entries_still_loads(written_toml):
    p = load(written_toml(ONE_ASK_ALLOW))
    assert p.owners == {}
    assert p.owns("a@example.com", "n1") is False


def test_a_blank_email_is_never_an_owner(written_toml):
    p = load(written_toml(ONE_ASK_ALLOW + '\n[[owner]]\nagent="n1"\nemail="a@example.com"\n'))
    assert p.owns("", "n1") is False
    assert p.owns("   ", "n1") is False


@pytest.mark.parametrize("policy_file", ["policy.example.toml", "docker/policy.example.toml"])
def test_both_examples_document_an_owner_and_still_load(policy_file):
    assert load(policy_file).owners
