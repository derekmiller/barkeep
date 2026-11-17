"""Opt-in scan over a corpus of real SalesByMenuItem exports.

The corpus lives at ``data/raw/`` and is gitignored, so real sales data is never
committed. Put exports there with their original filenames and run:

    pytest -m corpus -s -q

Set ``BARKEEP_CORPUS`` to scan a different directory instead.

Every file gets a parse test. A second test prints a consolidated anomaly report
(embedded commas, nonzero refunds/voids, duplicate keys, summary mismatches,
and so on) without failing the run for observations that are merely unusual.
"""

from __future__ import annotations

import csv
import io
import os
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from barkeep.errors import ParseError
from barkeep.models import ParseResult
from barkeep.money import parse_decimal, parse_percentage
from barkeep.parse import ACCEPTED_HEADERS, parse_csv

CORPUS_ENV = "BARKEEP_CORPUS"
DEFAULT_CORPUS_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

_corpus_value = os.environ.get(CORPUS_ENV)
CORPUS_DIR = (
    Path(_corpus_value).expanduser() if _corpus_value else DEFAULT_CORPUS_DIR
)

pytestmark = [pytest.mark.corpus]

_SUMMARY_LABEL_RE = re.compile(r"\((\d+)\s+entries?\)")
_ACCEPTED_HEADERS = {tuple(header) for header in ACCEPTED_HEADERS}
_NUMERIC_NAMES = (
    "Menu Item Void Quantity",
    "Voids",
    "Menu Item Quantity",
    "Gross Sales",
    "Item Discounts",
    "Bill Discounts",
    "Discounts",
    "Net Sales",
    "Menu Item Quantity (Refunds)",
    "Refund Amount (Net Sales)",
    "Total Tax",
    "Tax 1",
    "Tax 2",
    "Tax 3",
)
_ZERO_LABELS = {
    "Menu Item Void Quantity": "void quantity",
    "Voids": "voids",
    "Bill Discounts": "bill discounts",
    "Menu Item Quantity (Refunds)": "refund quantity",
    "Refund Amount (Net Sales)": "refund amount",
    "Total Tax": "total tax",
    "Tax 1": "tax 1",
    "Tax 2": "tax 2",
    "Tax 3": "tax 3",
}


def _corpus_files() -> list[Path]:
    if not CORPUS_DIR.is_dir():
        return []
    return sorted(CORPUS_DIR.glob("*.csv"))


CORPUS_FILES = _corpus_files()
_CORPUS_IDS = [path.name for path in CORPUS_FILES]


def _is_summary_row(row: list[str]) -> bool:
    return row[0].startswith("REPORT SUMMARY") or (
        row[1].strip() == "" and row[2].strip() == ""
    )


def _scan_file(path: Path) -> list[str]:
    """Return human-readable observations about one corpus file."""
    anomalies: list[str] = []
    raw = path.read_bytes()

    if raw.startswith(b"\xef\xbb\xbf"):
        anomalies.append("file starts with a UTF-8 BOM")
    if b"\r\n" in raw:
        anomalies.append("file contains CRLF line endings")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"not valid UTF-8: {exc}"]

    parsed = list(csv.reader(io.StringIO(text, newline="")))
    if not parsed:
        return ["file is empty"]

    header = parsed[0]
    if tuple(header) not in _ACCEPTED_HEADERS:
        anomalies.append(f"header is not an accepted layout: {header!r}")
        return anomalies

    width = len(header)
    column = {name: index for index, name in enumerate(header)}
    numeric_names = [name for name in _NUMERIC_NAMES if name in column]

    body: list[tuple[int, list[str]]] = []
    summary_rows: list[tuple[int, list[str]]] = []

    for line_number, row in enumerate(parsed[1:], start=2):
        if len(row) != width:
            anomalies.append(
                f"row {line_number}: {len(row)} columns instead of {width}"
            )
            continue
        if _is_summary_row(row):
            summary_rows.append((line_number, row))
        else:
            body.append((line_number, row))

    item_discount_name = "Discounts" if "Discounts" in column else "Item Discounts"
    total_columns = [
        (name, header_name)
        for name, header_name in (
            ("units", "Menu Item Quantity"),
            ("gross sales", "Gross Sales"),
            ("item discounts", item_discount_name),
            ("bill discounts", "Bill Discounts"),
            ("net sales", "Net Sales"),
        )
        if header_name in column
    ]
    totals = {name: Decimal("0") for name, _ in total_columns}

    natural_keys: Counter[tuple[str, str, str]] = Counter()
    percentage_total = Decimal("0")
    percentage_index = column["Sales Percentage"]

    for line_number, row in body:
        natural_keys[(row[0], row[1].strip(), row[2])] += 1

        if row[0].strip() == "":
            anomalies.append(f"row {line_number}: blank item name")

        values: dict[str, Decimal] = {}
        for name in numeric_names:
            index = column[name]
            cell = row[index]
            if "," in cell:
                anomalies.append(
                    f"row {line_number}: thousands separator in {name!r}"
                )
            try:
                values[name] = parse_decimal(cell)
            except ParseError as exc:
                anomalies.append(
                    f"row {line_number}: unparseable {name!r}: {exc}"
                )

        if any(value < 0 for value in values.values()):
            anomalies.append(f"row {line_number}: contains a negative amount")

        for name, label in _ZERO_LABELS.items():
            if values.get(name, Decimal("0")) != 0:
                anomalies.append(
                    f"row {line_number}: nonzero {label} "
                    f"({row[column[name]]!r})"
                )

        try:
            percentage_total += parse_percentage(row[percentage_index])
        except ParseError as exc:
            anomalies.append(f"row {line_number}: unparseable Sales Percentage: {exc}")

        for name, header_name in total_columns:
            totals[name] += values.get(header_name, Decimal("0"))

    for natural_key, count in natural_keys.items():
        if count > 1:
            anomalies.append(
                f"{count} rows share the natural key {natural_key!r}"
            )

    if not summary_rows:
        anomalies.append("no REPORT SUMMARY row found")
    elif len(summary_rows) > 1:
        anomalies.append(f"{len(summary_rows)} REPORT SUMMARY rows found")

    if summary_rows:
        line_number, summary = summary_rows[-1]
        label_match = _SUMMARY_LABEL_RE.search(summary[0])
        if label_match is not None and int(label_match.group(1)) != len(body):
            anomalies.append(
                f"summary label claims {label_match.group(1)} entries "
                f"but there are {len(body)} body rows"
            )
        for name, header_name in total_columns:
            try:
                summary_value = parse_decimal(summary[column[header_name]])
            except ParseError as exc:
                anomalies.append(f"summary row {line_number}: {exc}")
                continue
            if summary_value != totals[name]:
                anomalies.append(
                    f"summary {name} {summary_value} != body total {totals[name]}"
                )

    if body and abs(percentage_total - Decimal("100")) > Decimal("0.5"):
        anomalies.append(
            f"Sales Percentage sums to {percentage_total}, far from 100"
        )

    return anomalies


@pytest.mark.parametrize("path", CORPUS_FILES, ids=_CORPUS_IDS)
def test_corpus_file_parses(path: Path) -> None:
    result: ParseResult | None = None
    failure: str | None = None
    try:
        result = parse_csv(path.read_bytes(), path.name)
    except ParseError as exc:
        failure = f"{path.name}: {type(exc).__name__}: {exc}"

    if failure is not None:
        pytest.fail(failure, pytrace=False)

    assert result is not None
    assert result.period_start <= result.period_end


def test_corpus_anomaly_report() -> None:
    if not CORPUS_FILES:
        pytest.skip("no CSV files found in the corpus directory")

    failures: list[str] = []
    observations: list[str] = []

    for path in CORPUS_FILES:
        try:
            parse_csv(path.read_bytes(), path.name)
        except ParseError as exc:
            failures.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        for anomaly in _scan_file(path):
            observations.append(f"{path.name}: {anomaly}")

    print()
    print(f"Corpus: {CORPUS_DIR} ({len(CORPUS_FILES)} files)")
    print(f"Parsed OK: {len(CORPUS_FILES) - len(failures)}")
    print(f"Parse failures: {len(failures)}")
    for failure in failures:
        print(f"  - {failure}")
    print(f"Observations: {len(observations)}")
    for observation in observations:
        print(f"  - {observation}")
    print()

    assert not failures, "\n".join(failures)
