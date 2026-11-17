"""IMAP polling for scheduled TouchBistro report emails."""

from __future__ import annotations

import imaplib
import os
from dataclasses import dataclass

import psycopg

from barkeep.loader import LoadResult, load_file, record_failure
from barkeep.loader.db import connect
from barkeep.loader.errors import LoaderError

from .attachments import extract_csv_attachments


@dataclass(frozen=True)
class ImapConfig:
    """Connection settings for the report mailbox."""

    host: str
    user: str
    password: str
    mailbox: str = "INBOX"

    @classmethod
    def from_env(cls) -> "ImapConfig":
        values = {
            "host": os.environ.get("IMAP_HOST"),
            "user": os.environ.get("IMAP_USER"),
            "password": os.environ.get("IMAP_PASSWORD"),
            "mailbox": os.environ.get("IMAP_MAILBOX", "INBOX"),
        }
        missing = [key.upper() for key in ("host", "user", "password") if not values[key]]
        if missing:
            raise LoaderError(f"missing IMAP configuration: {', '.join(missing)}")
        return cls(
            host=values["host"] or "",
            user=values["user"] or "",
            password=values["password"] or "",
            mailbox=values["mailbox"] or "INBOX",
        )


def run_once(
    conn: psycopg.Connection,
    config: ImapConfig,
    *,
    limit: int | None = None,
) -> list[LoadResult]:
    """Process unread report emails once, marking successful messages as seen."""
    results: list[LoadResult] = []
    client = imaplib.IMAP4_SSL(config.host)

    try:
        client.login(config.user, config.password)
        client.select(config.mailbox)

        status, data = client.search(None, "UNSEEN")
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
                client.store(uid, "+FLAGS", "\\Seen")
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
