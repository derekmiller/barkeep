"""IMAP polling for scheduled TouchBistro report emails."""

from __future__ import annotations

import imaplib
import os
from dataclasses import dataclass
from datetime import date, timedelta

import psycopg

from barkeep.loader import LoadResult, load_file, record_failure
from barkeep.loader.db import connect
from barkeep.loader.errors import LoaderError
from barkeep.loader.secrets import read_secret_file

from .attachments import extract_csv_attachments

_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _imap_date(value: date) -> str:
    """Format a date the way IMAP ``SINCE`` expects (e.g. ``08-Nov-2026``)."""
    return f"{value.day:02d}-{_MONTHS[value.month - 1]}-{value.year}"


def _imap_quote(value: str) -> str:
    """Quote an IMAP search value, escaping backslashes and double quotes."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_search_criteria(
    *,
    sender: str | None = None,
    subject: str | None = None,
    since: date | None = None,
) -> list[str]:
    """Build IMAP search tokens for report email discovery.

    Read state is deliberately not part of the criteria: a report must still be
    found after someone opens it in a mail client. Duplicate protection comes
    from the loader's content hash, not from the ``\\Seen`` flag.
    """
    criteria: list[str] = []
    if sender:
        criteria.extend(["FROM", _imap_quote(sender)])
    if subject:
        criteria.extend(["SUBJECT", _imap_quote(subject)])
    if since is not None:
        criteria.extend(["SINCE", _imap_date(since)])
    if not criteria:
        criteria.append("ALL")
    return criteria


@dataclass(frozen=True)
class ImapConfig:
    """Connection settings for the report mailbox."""

    host: str
    user: str
    password: str
    mailbox: str = "INBOX"
    sender: str | None = None
    subject: str | None = None
    since_days: int = 21

    @classmethod
    def from_env(cls) -> "ImapConfig":
        values = {
            "host": os.environ.get("IMAP_HOST"),
            "user": os.environ.get("IMAP_USER"),
            "password": os.environ.get("IMAP_PASSWORD"),
            "mailbox": os.environ.get("IMAP_MAILBOX", "INBOX"),
            "sender": os.environ.get("IMAP_SENDER"),
            "subject": os.environ.get("IMAP_SUBJECT"),
            "since_days": os.environ.get("IMAP_SINCE_DAYS", "21"),
        }
        missing = [
            key.upper() for key in ("host", "user") if not values[key]
        ]
        if missing:
            raise LoaderError(f"missing IMAP configuration: {', '.join(missing)}")

        password = values["password"]
        if not password:
            password_file = os.environ.get("IMAP_PASSWORD_FILE")
            if password_file:
                password = read_secret_file(password_file)
        if not password:
            raise LoaderError(
                "missing IMAP configuration: IMAP_PASSWORD or IMAP_PASSWORD_FILE"
            )

        try:
            since_days = int(values["since_days"] or "21")
        except ValueError as exc:
            raise LoaderError(
                f"IMAP_SINCE_DAYS must be an integer, got {values['since_days']!r}"
            ) from exc
        if since_days < 0:
            raise LoaderError("IMAP_SINCE_DAYS must be zero or positive")

        return cls(
            host=values["host"] or "",
            user=values["user"] or "",
            password=password,
            mailbox=values["mailbox"] or "INBOX",
            sender=values["sender"] or None,
            subject=values["subject"] or None,
            since_days=since_days,
        )


def run_once(
    conn: psycopg.Connection,
    config: ImapConfig,
    *,
    limit: int | None = None,
    today: date | None = None,
) -> list[LoadResult]:
    """Process matching report emails once.

    Only messages carrying a report attachment are touched; unmatched messages
    are left exactly as they were, including their read/unread state. A report
    message is marked ``\\Seen`` only after all of its attachments load
    successfully, so a failure stays visible for retry.
    """
    since = None
    if config.since_days > 0:
        since = (today or date.today()) - timedelta(days=config.since_days)
    criteria = build_search_criteria(
        sender=config.sender,
        subject=config.subject,
        since=since,
    )

    results: list[LoadResult] = []
    client = imaplib.IMAP4_SSL(config.host)

    try:
        client.login(config.user, config.password)
        status, _ = client.select(config.mailbox)
        if status != "OK":
            raise LoaderError(
                f"IMAP SELECT {config.mailbox!r} failed: {status}"
            )

        status, data = client.search(None, *criteria)
        if status != "OK":
            raise LoaderError(f"IMAP search failed: {status}")

        uids = data[0].split() if data and data[0] else []
        if limit is not None:
            uids = uids[:limit]

        for uid in uids:
            status, message_data = client.fetch(uid, "(RFC822)")
            if status != "OK":
                continue

            raw_email = next(
                (part[1] for part in message_data if isinstance(part, tuple)),
                None,
            )
            if raw_email is None:
                continue

            attachments = extract_csv_attachments(raw_email)
            if not attachments:
                # Not a report email: never change its flags.
                continue

            failed = False
            for filename, payload in attachments:
                try:
                    results.append(load_file(conn, payload, filename))
                except Exception as exc:  # noqa: BLE001 - record and continue
                    record_failure(conn, filename, str(exc))
                    results.append(
                        LoadResult(
                            filename=filename,
                            status="failed",
                            message=str(exc),
                        )
                    )
                    failed = True

            if not failed:
                client.store(uid, "+FLAGS", "\\Seen")
    finally:
        try:
            client.logout()
        except imaplib.IMAP4.error:
            pass

    return results


def main() -> None:
    """CLI entry point: ``python -m barkeep.ingest.imap``."""
    config = ImapConfig.from_env()
    with connect() as conn:
        results = run_once(conn, config)
    for result in results:
        detail = f" ({result.message})" if result.message else ""
        print(f"{result.status}: {result.filename}{detail}")


if __name__ == "__main__":
    main()
