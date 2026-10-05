"""Ricardi: a Slack proxy to Claude Code agents running on a box.

Two seams hold the design apart, so neither side needs to know the other:

  transports/  where humans are (Slack, or a local stub for tests)
  backends/    where the agents are (Claude Code sessions)

The router in between is pure: text in, a decision out, no I/O. That is what
makes an end-to-end test possible without Slack and without spending tokens.
"""

__version__ = "0.1.0"
