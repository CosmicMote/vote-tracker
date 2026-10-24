"""Logging to stdout and a rotating file, shared by the CLI and the web server."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import Settings


def configure_logging(settings: Settings, verbose: bool = False) -> None:
    root = logging.getLogger()
    if any(getattr(h, "_tracker", False) for h in root.handlers):
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    out = logging.StreamHandler(sys.stdout)
    settings.storage.log_dir.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(settings.storage.log_dir / "tracker.log", maxBytes=5_000_000, backupCount=5)
    for h in (out, fh):
        h.setFormatter(fmt)
        h._tracker = True  # type: ignore[attr-defined]
        root.addHandler(h)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines would include the API key
