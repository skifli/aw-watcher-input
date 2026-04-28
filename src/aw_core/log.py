from __future__ import annotations

import logging
from pathlib import Path


def setup_logging(
    name: str,
    testing: bool = False,
    verbose: bool = False,
    log_stderr: bool = True,
    log_file: bool = False,
):
    level = logging.DEBUG if verbose or testing else logging.INFO
    handlers = []
    if log_stderr:
        handlers.append(logging.StreamHandler())
    if log_file:
        state_home = Path.home() / ".local" / "state"
        state_home.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(state_home / f"{name}.log"))
    logging.basicConfig(level=level, handlers=handlers or None)
