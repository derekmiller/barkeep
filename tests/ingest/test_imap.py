"""Tests for the IMAP poller's search and flag behaviour (no real IMAP)."""

from __future__ import annotations

from datetime import date
from email.message import EmailMessage

import pytest

from barkeep.ingest import imap
from barkeep.ingest.imap import ImapConfig, build_search_criteria, run_once
from barkeep.loader import LoadResult, LoaderError


@pytest.fixture()
def fake_imap(monkeypatch):
    class FakeImap:
        instances: list["FakeImap"] = []
        messages: dict[bytes, bytes] = {}
        select_status = "OK"

        def __init__(self, host: str) -> None:
            self.host = host
            self.selected: str | None = None
            self.search_args: tuple[str | None, tuple[str, ...]] | None = None
            self.store_calls: list[tuple[bytes, str, str]] = []
            FakeImap.instances.append(self)

        def login(self, user: str, password: str):
            self.login_args = (user, password)
            return "OK", []

        def select(self, mailbox: str):
            self.selected = mailbox
            return FakeImap.select_status, [b"1"]

        def search(self, charset, *criteria):
            self.search_args = (charset, criteria)
            return "OK", [b" ".join(FakeImap.messages)]

        def fetch(self, uid, spec):
            return "OK", [(b"header", FakeImap.messages[uid])]

        def store(self, uid, command, value):
            self.store_calls.append((uid, command, value))
            return "OK", []

        def logout(self):
            self.logged_out = True

    monkeypatch.setattr(imap.imaplib, "IMAP4_SSL", FakeImap)
    return FakeImap


def _config(**overrides) -> ImapConfig:
    values = {
        "host": "imap.example.com",
        "user": "owner@example.com",
        "password": "secret",
        "mailbox": "SalesReports",
        "sender": "reports@touchbistro.com",
        "subject": "SalesByMenuItem",
        "since_days": 21,
    }
    values.update(overrides)
    return ImapConfig(**values)


def _raw_email_without_report() -> bytes:
    message = EmailMessage()
    message["Subject"] = "Unrelated"
    message.set_content("Nothing to see here.")
    return message.as_bytes()


def _raw_email_with_report() -> bytes:
    message = EmailMessage()
    message["Subject"] = "SalesByMenuItem"
    message.set_content("Attached.")
    message.add_attachment(
        b"Menu Item\n",
        maintype="text",
        subtype="csv",
        filename="Example_Venue-SalesByMenuItem-2026-11-08-2026-11-14.csv",
    )
    return message.as_bytes()


def test_search_criteria_use_sender_subject_and_since() -> None:
    criteria = build_search_criteria(
        sender="reports@touchbistro.com",
        subject="Weekly Report",
        since=date(2026, 11, 1),
    )

    assert criteria == [
        "FROM",
        '"reports@touchbistro.com"',
        "SUBJECT",
        '"Weekly Report"',
        "SINCE",
        "01-Nov-2026",
    ]


def test_search_criteria_default_to_all() -> None:
    assert build_search_criteria() == ["ALL"]


def test_run_once_never_marks_non_report_mail_seen(fake_imap) -> None:
    fake_imap.messages = {b"1": _raw_email_without_report()}

    results = run_once(
        object(),  # type: ignore[arg-type]
        _config(),
        today=date(2026, 11, 15),
    )

    assert results == []
    instance = fake_imap.instances[0]
    assert instance.store_calls == []
    assert instance.selected == "SalesReports"


def test_run_once_searches_without_read_state(fake_imap) -> None:
    fake_imap.messages = {b"1": _raw_email_without_report()}

    run_once(object(), _config(), today=date(2026, 11, 15))  # type: ignore[arg-type]

    instance = fake_imap.instances[0]
    assert instance.search_args == (
        None,
        (
            "FROM",
            '"reports@touchbistro.com"',
            "SUBJECT",
            '"SalesByMenuItem"',
            "SINCE",
            "25-Oct-2026",
        ),
    )
    assert "UNSEEN" not in instance.search_args[1]


def test_run_once_marks_report_seen_only_after_success(
    fake_imap, monkeypatch
) -> None:
    fake_imap.messages = {b"1": _raw_email_with_report()}
    monkeypatch.setattr(
        imap,
        "load_file",
        lambda conn, data, filename: LoadResult(
            filename=filename, status="loaded"
        ),
    )

    results = run_once(object(), _config(), today=date(2026, 11, 15))  # type: ignore[arg-type]

    assert [result.status for result in results] == ["loaded"]
    instance = fake_imap.instances[0]
    assert instance.store_calls == [(b"1", "+FLAGS", "\\Seen")]


def test_run_once_leaves_failed_report_unread(fake_imap, monkeypatch) -> None:
    fake_imap.messages = {b"1": _raw_email_with_report()}
    monkeypatch.setattr(imap, "record_failure", lambda *args, **kwargs: None)

    def boom(*args, **kwargs):
        raise ValueError("bad report")

    monkeypatch.setattr(imap, "load_file", boom)

    results = run_once(object(), _config(), today=date(2026, 11, 15))  # type: ignore[arg-type]

    assert [result.status for result in results] == ["failed"]
    instance = fake_imap.instances[0]
    assert instance.store_calls == []


def test_select_failure_is_reported(fake_imap) -> None:
    fake_imap.select_status = "NO"

    with pytest.raises(LoaderError, match="SELECT"):
        run_once(object(), _config())  # type: ignore[arg-type]
