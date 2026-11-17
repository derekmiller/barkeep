"""Email ingestion for scheduled TouchBistro reports."""

from __future__ import annotations

from .attachments import extract_csv_attachments, is_sales_report, sender_matches
from .imap import ImapConfig, build_search_criteria, run_once

__all__ = [
    "ImapConfig",
    "build_search_criteria",
    "extract_csv_attachments",
    "is_sales_report",
    "run_once",
    "sender_matches",
]
