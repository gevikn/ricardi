"""Shared helpers.

Ricardi denies everything without a policy, which is the right default and
makes every test that isn't about policy need to say so explicitly. That is a
feature: a test passing an open policy is visibly choosing to.
"""

from ricardi.policy import Rule, RulesPolicy

TESTER = "tester@example.com"


def open_policy(roster: dict[str, str] | None = None) -> RulesPolicy:
    """Permits everything, for tests about something other than policy."""
    return RulesPolicy(
        rules=[Rule(effect="allow", name="everything, in tests")],
        roster=roster or {"u1": TESTER, "tester": TESTER},
    )
