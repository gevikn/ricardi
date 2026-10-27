"""A marker the process owns, because the container log cannot be trusted.

The deploy used to decide "is it up" by grepping the container log for
`connected to Slack`. On the box that gate reads a log that loses its newest
lines within minutes: the same `docker logs` call returned the current boot
at one moment and, minutes later, only lines from a boot two days earlier.
An unbounded `--tail` passes that gate on a line from a previous deploy --
it was passing for the wrong reason -- and a window anchored on the current
boot fails on a container that is perfectly healthy.

So the signal moves to something the process writes and the host can read
directly: `state/` is already a bind mount, so a file there crosses the
container boundary with no log driver in the path.
"""

import json
from datetime import datetime, timezone

import pytest

from ricardi import readiness


def test_the_marker_sits_in_the_state_directory(tmp_path):
    assert readiness.path_for(tmp_path) == tmp_path / "ready.json"


def test_being_ready_records_when_where_and_which_agents(tmp_path):
    readiness.mark_ready(tmp_path, where="Slack", agents=["ricardi"])
    marker = json.loads(readiness.path_for(tmp_path).read_text(encoding="utf-8"))
    assert marker["ready"] is True
    assert marker["where"] == "Slack"
    assert marker["agents"] == ["ricardi"]
    assert marker["connected_at"]


def test_the_timestamp_is_utc_in_the_form_the_deploy_parses(tmp_path):
    # The deploy turns this into seconds with `date -u -d` and compares it to
    # the container's StartedAt. A local-time or fractional stamp would still
    # look fine here and be wrong by hours there.
    readiness.mark_ready(tmp_path, where="Slack", agents=["ricardi"])
    stamp = json.loads(readiness.path_for(tmp_path).read_text(encoding="utf-8"))["connected_at"]
    parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 60


def test_the_state_directory_is_created_if_it_is_missing(tmp_path):
    state = tmp_path / "not" / "there" / "yet"
    readiness.mark_ready(state, where="Slack", agents=["ricardi"])
    assert readiness.path_for(state).exists()


def test_nothing_is_ready_until_it_says_so(tmp_path):
    # Startup clears the marker, so "the file is there" means ready now
    # rather than ready at some point in this container's history.
    readiness.mark_ready(tmp_path, where="Slack", agents=["ricardi"])
    readiness.clear(tmp_path)
    assert not readiness.path_for(tmp_path).exists()


def test_clearing_when_there_is_nothing_to_clear_is_not_an_error(tmp_path):
    readiness.clear(tmp_path)
    readiness.clear(tmp_path / "never" / "existed")


def test_a_failed_write_leaves_the_last_marker_intact(tmp_path, monkeypatch):
    # The deploy polls this file every two seconds while the process may be
    # rewriting it. A writer that truncates in place hands it half a file,
    # and `date -u -d` on a half-read timestamp fails the gate on a healthy
    # container. Written to a temporary name and renamed into place.
    readiness.mark_ready(tmp_path, where="Slack", agents=["first"])
    before = readiness.path_for(tmp_path).read_text(encoding="utf-8")

    def refuse(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(readiness.os, "replace", refuse)
    with pytest.raises(OSError):
        readiness.mark_ready(tmp_path, where="Slack", agents=["second"])

    assert readiness.path_for(tmp_path).read_text(encoding="utf-8") == before
    assert json.loads(before)["agents"] == ["first"]


def test_a_failed_write_leaves_no_litter_behind(tmp_path, monkeypatch):
    readiness.mark_ready(tmp_path, where="Slack", agents=["first"])

    def refuse(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(readiness.os, "replace", refuse)
    with pytest.raises(OSError):
        readiness.mark_ready(tmp_path, where="Slack", agents=["second"])

    assert sorted(p.name for p in tmp_path.iterdir()) == ["ready.json"]
