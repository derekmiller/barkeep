"""Extract CSV attachments from raw email messages."""

from __future__ import annotations

from email import message_from_bytes
from email.header import decode_header
from email.utils import parseaddr


def _decode_filename(value: str) -> str:
    parts = decode_header(value)
    decoded: list[str] = []
    for chunk, encoding in parts:
        if isinstance(chunk, bytes):
            decoded.append(chunk.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded.append(chunk)
    return "".join(decoded)


def is_sales_report(filename: str) -> bool:
    """Return whether ``filename`` looks like a SalesByMenuItem CSV export."""
    return filename.lower().endswith(".csv") and "salesbymenuitem" in filename.lower()


def sender_matches(raw_email: bytes, expected: str) -> bool:
    """Return whether the message ``From`` address matches ``expected``.

    ``expected`` may be a full address (``reports@example.com``) or a bare
    domain (``example.com``). An empty or missing ``From`` never matches.
    """
    expected = expected.strip().lower().lstrip("@")
    if not expected:
        return False

    message = message_from_bytes(raw_email)
    _, address = parseaddr(message.get("From", ""))
    address = address.lower()
    if not address:
        return False
    if "@" in expected:
        return address == expected
    return address.endswith("@" + expected)


def extract_csv_attachments(raw_email: bytes) -> list[tuple[str, bytes]]:
    """Return ``(filename, payload)`` for each SalesByMenuItem CSV attachment."""
    message = message_from_bytes(raw_email)
    attachments: list[tuple[str, bytes]] = []

    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        raw_filename = part.get_filename()
        if not raw_filename:
            continue
        filename = _decode_filename(raw_filename)
        if not is_sales_report(filename):
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            attachments.append((filename, payload))

    return attachments
