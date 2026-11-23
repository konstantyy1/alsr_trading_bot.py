"""WSGI entry point for gunicorn: `gunicorn wsgi:application`.

Keeps the bot package importable whether the service root is `alsr/` or the repo root.
"""
from __future__ import annotations

import os
import pathlib
import sys

_here = pathlib.Path(__file__).resolve().parent
_bot = _here / "bot"
if str(_bot) not in sys.path:
    sys.path.insert(0, str(_bot))
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

os.environ.setdefault("PORT", "10000")

from web import application  # noqa: E402

__all__ = ["application"]
