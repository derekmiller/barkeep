"""Tests for secret-file support (no database required)."""

from __future__ import annotations

from pathlib import Path

import pytest

from barkeep.ingest.imap import ImapConfig
from barkeep.loader import LoaderError, read_secret_file, resolve_database_url


def test_read_secret_file_strips_only_trailing_newlines(tmp_path: Path) -> None:
    path = tmp_path / "secret"
    path.write_text("p@ss word\n", encoding="utf-8")

    assert read_secret_file(path) == "p@ss word"


def test_read_secret_file_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(LoaderError, match="cannot read secret file"):
        read_secret_file(tmp_path / "absent")


def test_resolve_database_url_precedence(tmp_path: Path) -> None:
    path = tmp_path / "dsn"
    path.write_text("postgresql://from/file\n", encoding="utf-8")

    assert (
        resolve_database_url("postgresql://explicit", {})
        == "postgresql://explicit"
    )
    assert (
        resolve_database_url(
            None,
            {
                "DATABASE_URL": "postgresql://from/env",
                "DATABASE_URL_FILE": str(path),
            },
        )
        == "postgresql://from/env"
    )
    assert (
        resolve_database_url(None, {"DATABASE_URL_FILE": str(path)})
        == "postgresql://from/file"
    )
    assert resolve_database_url(None, {}) is None


def test_imap_config_reads_password_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "imap_password"
    path.write_text("app-password\n", encoding="utf-8")

    for name in ("IMAP_PASSWORD", "IMAP_SENDER", "IMAP_SUBJECT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "owner@example.com")
    monkeypatch.setenv("IMAP_PASSWORD_FILE", str(path))

    config = ImapConfig.from_env()

    assert config.password == "app-password"
    assert config.mailbox == "INBOX"


def test_imap_config_requires_a_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("IMAP_PASSWORD", "IMAP_PASSWORD_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "owner@example.com")

    with pytest.raises(LoaderError, match="IMAP_PASSWORD"):
        ImapConfig.from_env()
