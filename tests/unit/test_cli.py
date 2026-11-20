"""CLI behavior that is not covered by the command modules themselves."""

import logging
import socket
from pathlib import Path

import pytest

from hl7bridge import cli
from hl7bridge.logsafety import THIRD_PARTY_HTTP_LOGGERS

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "hospitals"


@pytest.fixture(autouse=True)
def restore_logging():
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    saved_levels = {n: logging.getLogger(n).level for n in THIRD_PARTY_HTTP_LOGGERS}
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
    root.setLevel(saved_level)
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


def test_serve_on_a_taken_port_fails_with_a_clear_message(tmp_path):
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: make the port truly taken
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(
                [
                    "serve",
                    "--db",
                    str(tmp_path / "bridge.db"),
                    "--config",
                    str(CONFIG_DIR),
                    "--port",
                    str(port),
                ]
            )
    finally:
        holder.close()
    message = str(excinfo.value)
    assert f"cannot listen on 127.0.0.1:{port}" in message
    assert "--port" in message


def test_version_flag(capsys):
    from hl7bridge import __version__

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"hl7bridge {__version__}"
