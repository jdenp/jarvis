"""JARVIS - Just A Rather Very Intelligent System."""

from __future__ import annotations

from pathlib import Path


def _version_in(root: Path) -> str | None:
    """The version out of a checkout's pyproject.toml, or None if there isn't one."""
    import tomllib

    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        return str(tomllib.loads(text)["project"]["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return None


def _version() -> str:
    """pyproject.toml is the one place the version is written down.

    The checkout wins over the installed metadata on purpose. This is installed
    editable, so metadata records whatever the version was at the last `uv sync`
    and goes stale the moment anyone bumps it - which is the drift this is meant
    to end, just moved somewhere harder to see.
    """
    from_checkout = _version_in(Path(__file__).resolve().parents[2])
    if from_checkout is not None:
        return from_checkout

    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("jarvis")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _version()

__all__ = ["__version__"]
