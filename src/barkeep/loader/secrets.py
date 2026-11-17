"""Secret loading helpers shared by the loader and ingest packages."""

from __future__ import annotations

from pathlib import Path

from .errors import LoaderError


def read_secret_file(path: str | Path) -> str:
    """Read a secret from ``path``, stripping only the trailing newline.

    The path is included in the error message; the secret value never is.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise LoaderError(f"cannot read secret file {path}: {exc}") from exc
    return text.rstrip("\r\n")
