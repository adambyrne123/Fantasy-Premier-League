"""Lets the package be run as `python -m fpl_manager`."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
