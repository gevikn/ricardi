"""Where the log goes.

The scheduled task on Windows captures nothing: no console, no redirection.
Ricardi ran for hours there, perfectly, and left not one line behind -- which
is indistinguishable from being down until you look in Slack. These check the
file actually gets written, since that was the half that quietly did not.
"""

import logging
from logging.handlers import RotatingFileHandler

from ricardi.cli import log_handlers


def _close(handlers):
    # Windows keeps the file open otherwise, and tmp_path cleanup fails.
    for h in handlers:
        h.close()


def test_console_only_without_a_log_file():
    handlers = log_handlers("")
    try:
        assert len(handlers) == 1
        assert isinstance(handlers[0], logging.StreamHandler)
    finally:
        _close(handlers)


def test_a_log_file_is_written(tmp_path):
    handlers = log_handlers(str(tmp_path / "service.log"))
    try:
        assert len(handlers) == 2
        record = logging.LogRecord(
            "ricardi", logging.INFO, __file__, 1, "connected to Slack", None, None
        )
        handlers[1].emit(record)
        handlers[1].flush()
        assert "connected to Slack" in (tmp_path / "service.log").read_text()
    finally:
        _close(handlers)


def test_the_console_survives_a_log_file(tmp_path):
    # Both, not either: a file does not mean losing `docker compose logs`.
    handlers = log_handlers(str(tmp_path / "service.log"))
    try:
        assert isinstance(handlers[0], logging.StreamHandler)
    finally:
        _close(handlers)


def test_it_rotates_rather_than_growing_forever(tmp_path):
    handlers = log_handlers(str(tmp_path / "service.log"))
    try:
        handler = handlers[1]
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 10 * 1024 * 1024
        assert handler.backupCount == 5
    finally:
        _close(handlers)


def test_a_missing_directory_is_created(tmp_path):
    # state/ does not exist on a fresh clone, and the task would die on
    # startup with a FileNotFoundError nobody could read, having no log.
    target = tmp_path / "state" / "service.log"
    handlers = log_handlers(str(target))
    try:
        assert target.parent.is_dir()
    finally:
        _close(handlers)


def test_a_tilde_is_expanded(tmp_path, monkeypatch):
    # The installer writes an absolute path, but a person editing the unit by
    # hand will write ~, and a literal "~" directory beside the repo is a
    # confusing way to find that out.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    handlers = log_handlers("~/service.log")
    try:
        assert handlers[1].baseFilename == str(tmp_path / "service.log")
    finally:
        _close(handlers)
