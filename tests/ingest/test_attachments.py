"""Tests for attachment extraction (no IMAP or database required)."""

from __future__ import annotations

from email.message import EmailMessage

from barkeep.ingest.attachments import extract_csv_attachments, is_sales_report


def _email_with_attachments(
    attachments: list[tuple[str, bytes]],
) -> bytes:
    message = EmailMessage()
    message["Subject"] = "Scheduled report"
    message.set_content("Attached.")
    for filename, payload in attachments:
        message.add_attachment(
            payload,
            maintype="text",
            subtype="csv",
            filename=filename,
        )
    return message.as_bytes()


def test_is_sales_report_matches_csv_contract() -> None:
    assert is_sales_report(
        "Bay_Horse_Tavern-SalesByMenuItem-2026-11-08-2026-11-14.csv"
    )
    assert is_sales_report("SALESBYMENUITEM-EXPORT.CSV")
    assert not is_sales_report("notes.csv")
    assert not is_sales_report("SalesByMenuItem-2026-11-08.txt")


def test_extract_only_sales_report_attachments() -> None:
    wanted = (
        "Example_Venue-SalesByMenuItem-2026-11-08-2026-11-14.csv",
        b"a,b\n1,2\n",
    )
    ignored = ("readme.csv", b"not a report\n")

    raw = _email_with_attachments([wanted, ignored])

    assert extract_csv_attachments(raw) == [wanted]


def test_email_without_attachments_returns_empty() -> None:
    message = EmailMessage()
    message.set_content("No attachment here.")

    assert extract_csv_attachments(message.as_bytes()) == []
