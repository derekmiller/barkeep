# AGENTS.md — barkeep

## 1. What this project is

A pipeline library that turns TouchBistro "SalesByMenuItem" CSV exports into
queryable Postgres data:

    TouchBistro scheduled report (email, CSV attachment)
        -> barkeep.ingest      IMAP poller
        -> barkeep.parser      pure CSV -> typed objects
        -> barkeep.loader      idempotent Postgres replace
        -> analytics views
        -> Metabase / any other BI tool

The parser remains a pure function. Database, email, and scheduling concerns live
in separate packages and never leak into `barkeep.parser`.

### Naming and layout

The project is called **`barkeep`**. The TouchBistro format is confined to the
parsing modules; it must not leak into package names, module names, or public API
names.

```
barkeep/
├── AGENTS.md
├── pyproject.toml
├── compose.yaml
├── .env.example
├── sql/
│   ├── 001_schema.sql
│   └── 002_views.sql
├── src/barkeep/
│   ├── __init__.py
│   ├── parser/
│   │   ├── __init__.py      # public API: parse_csv, models, errors
│   │   ├── errors.py        # exception hierarchy
│   │   ├── models.py        # ParseResult, SalesRow, SummaryRow
│   │   ├── filename.py      # vendor filename contract -> venue + period
│   │   ├── money.py         # Decimal parsing (comma-safe)
│   │   ├── normalise.py     # item_key normalisation
│   │   └── parse.py         # parse_csv()
│   ├── loader/
│   │   ├── __init__.py
│   │   ├── db.py            # Postgres connection
│   │   ├── errors.py        # LoaderError
│   │   ├── load.py          # transactional replace + ingest log
│   │   ├── migrate.py       # numbered SQL migrations
│   │   ├── secrets.py       # read a secret from a file
│   │   └── venues.py        # venue alias resolution
│   └── ingest/
│       ├── __init__.py
│       ├── __main__.py      # python -m barkeep.ingest
│       ├── attachments.py   # email -> CSV attachments
│       └── imap.py          # IMAP poller
└── tests/
    ├── fixtures/
    │   ├── Example_Venue-SalesByMenuItem-2026-11-08-2026-11-14.csv
    │   ├── Example_Venue-SalesByMenuItem-2026-11-15-2026-11-15.csv
    │   ├── Example_Venue-SalesByMenuItem-2026-11-16-2026-11-16.csv
    │   ├── Test_Venue-SalesByMenuItem-2024-06-28-2024-07-04.csv
    │   └── Test_Venue-SalesByMenuItem-2025-09-28-2025-10-04.csv
    ├── parser/
    │   ├── test_parse.py
    │   └── test_corpus.py
    ├── loader/
    │   └── test_load.py
    └── ingest/
        └── test_attachments.py
```

Rules that follow from this:
- Parser import path is `from barkeep.parser import parse_csv`.
- `barkeep.parser` must not import `psycopg`, `barkeep.loader`, or
  `barkeep.ingest`. Dependencies flow one way: parser <- loader <- ingest.
- Do not name anything `touchbistro_*`. Vendor-specific knowledge lives in the
  parser's behaviour, documented in this file — not in identifiers.
- Do not create a `pos_parser` package. Earlier discussion used that name; it is
  superseded.
- `pyproject.toml` project name is `barkeep`, package directory `src/barkeep/`.
- Database dependencies belong in the `loader` optional extra. The parser keeps
  zero third-party dependencies.

## 2. Non-negotiable architecture rules

- `parse_csv(data: bytes, filename: str) -> ParseResult` — pure. Same input,
  same output, no side effects, no I/O, no global state, no clock reads.
- `barkeep.parser` must not import `psycopg`, `barkeep.loader`,
  `barkeep.ingest`, any network library, or any scheduler. Purity applies to the
  parser package; the loader and ingest packages are where I/O lives.
- Do **not** call `datetime.date.today()` or `datetime.now()` anywhere in the
  parser. Dates come only from the filename.
- Money and quantities are `decimal.Decimal`. **Never `float`** anywhere in this
  project, including tests and database code.
- Return results; do not mutate inputs.
- Raise a specific exception subclass on bad input. Never return `None` to
  signal failure. Never silently skip a row that fails to parse.
- Loading must be idempotent and transactional: identical bytes are skipped,
  changed bytes for an existing period replace that period atomically, and a
  failed load never leaves a partial report behind.

## 3. Filename contract

    {venue}-SalesByMenuItem-{YYYY-MM-DD}-{YYYY-MM-DD}.csv

Example:

    Example_Venue-SalesByMenuItem-2026-11-08-2026-11-14.csv
    -> venue = "Example_Venue"
    -> period_start = 2026-11-08
    -> period_end   = 2026-11-14

Rules:
- The CSV contains **no date column**. Period comes only from the filename.
- If the filename does not match, raise `FilenameParseError`. Do not guess.
- `period_end` may equal `period_start` (a single-day report). That is valid.
- Compute `days_covered = (period_end - period_start).days + 1`.
- Do not validate that `days_covered == 7`. Reports of other lengths are legal
  and must parse.

## 4. CSV format

- UTF-8, no BOM, LF line endings, comma-delimited, fields quoted.
- Three header layouts are accepted. The header must match one of them exactly;
  anything else raises `FormatError`. Column names have no leading/trailing
  whitespace.

  **Standard (14 columns):**

  ```
  Menu Item
  Sales Category
  Menu Group
  Menu Item Void Quantity
  Voids
  Menu Item Quantity
  Gross Sales
  Item Discounts
  Bill Discounts
  Net Sales
  Menu Item Quantity (Refunds)
  Refund Amount (Net Sales)
  Sales Percentage
  Total Tax
  ```

  **Tax-split (16 columns):** identical to the standard layout, except the
  final `Total Tax` column is replaced by `Tax 1`, `Tax 2`, `Tax 3`.

  **Legacy (15 columns):**

  ```
  Menu Item
  Sales Category
  Menu Group
  Menu Item Void Quantity
  Voids
  Menu Item Quantity
  Gross Sales
  Discounts
  Net Sales
  Menu Item Quantity (Refunds)
  Refund Amount (Net Sales)
  Sales Percentage
  Tax 1
  Tax 2
  Tax 3
  ```

- In the legacy layout, monetary cells are `$`-prefixed and negative amounts
  appear as `-$214.25`. Strip `$` and thousands separators before `Decimal`.
- In the legacy layout, `Discounts` is a combined total: store it in
  `item_discounts`, set `bill_discounts = 0.00`, and record a warning in
  `ParseResult.warnings`.
- `sales_category` may carry surrounding whitespace in any layout (observed in
  both legacy and current files); trim it. `item_name` and `menu_group` stay
  verbatim.
- Validate the header against an accepted layout. If no layout matches, raise
  `FormatError` — do not index positionally on faith.
- `Menu Group` **can contain an embedded newline** inside its quoted field,
  e.g. `"Important" Beers &\nSeltzers`. Read with the `csv` module.
  **Never `split(",")`, never `splitlines()` on the raw text.**

## 5. Hard invariants (each is a real observed defect)

### 5.1 The summary row must be excluded
The final row is a totals row labelled `REPORT SUMMARY (N entries)`, with blank
`Sales Category` and `Menu Group`. Its numeric columns are **totals of all rows
above it**.

If you include it, every aggregate doubles exactly. This is the single most
likely bug in this project.

Detection (use both conditions):
```
row[0].startswith("REPORT SUMMARY")  or  (row[1].strip() == "" and row[2].strip() == "")
```
Keep the summary in `ParseResult.summary` (it is a useful cross-check), but it
must never appear in `ParseResult.rows`.

**Mandatory self-check:** `sum(row.units for row in result.rows)` must equal
`result.summary.units`. If it does not, raise `IntegrityError`. This is your
double-counting alarm — implement it, do not skip it.

The same check applies to every totalled money column — `gross_sales`,
`item_discounts`, `bill_discounts`, and `net_sales`. Units alone can agree while
money does not: a dropped zero-unit comp row is invisible to a units-only check.
All 114 corpus files satisfy every column exactly.

The parser accepts a file with no summary row (`summary is None`), because
parsing is not the place to decide policy. The loader rejects it (§13.1): a file
truncated cleanly between rows still parses, and the summary is the only thing
that could reveal the missing tail.

### 5.2 Thousands separators
Summary-row values contain commas: `1,620.00`, `8,163.50`, `7,973.50`.
`Decimal("1,620.00")` raises `InvalidOperation`. Strip commas before parsing.
Body rows in the current fixtures have no commas, but strip unconditionally —
do not special-case the summary row only.

### 5.3 Pre-rounded percentage
`Sales Percentage` is rounded per row and does **not** sum to 100:
- daily summary: `100.00%`
- weekly summary: `99.99%`

Parse it, never validate it, never aggregate it. Shares must be recomputed from
`Net Sales` if needed.

### 5.4 Total Tax is always zero
`Total Tax` is `0.00` on every row by design. TouchBistro excludes tax from
this report. Do not treat this as "no tax was charged".

### 5.5 Arithmetic identity to preserve
For every body row in the standard and tax-split layouts:
`Gross Sales - Item Discounts - Bill Discounts == Net Sales`.

For every body row in the legacy layout (one combined discount column):
`Gross Sales - Discounts == Net Sales`. The parser maps `Discounts` to
`item_discounts` and sets `bill_discounts = 0.00`, so the same check applies.

Assert it per row and raise `IntegrityError` on violation — if TouchBistro ever
changes this, you want to know loudly.

### 5.6 Refund and void columns
`Menu Item Void Quantity`, `Voids`, `Menu Item Quantity (Refunds)`,
`Refund Amount (Net Sales)`, and `Bill Discounts` are `0.00` in the main weekly
and daily fixtures, but they are **not** always zero in real exports: the
edge-case fixture carries nonzero voids and refunds, and the corpus scan found
hundreds of such rows. Parse and store them anyway. Do not assume they are
always zero, and do not drop the columns. The legacy layout has no separate
`Bill Discounts` column; its combined `Discounts` is handled per §4.

## 6. Units semantics

`Menu Item Quantity` is **gross units** — units sold, before refunds. This is
confirmed by the system owner, not inferred. Consequence:

- Do **not** subtract refunds from `Menu Item Quantity`.
- If net units are ever needed, they must be derived separately and labelled
  `units_net`, never silently.
- Document the gross semantics in the `SalesRow` docstring.

`Net Sales` is a *revenue* figure (after discounts). It is **not** the unit
count. Units and revenue legitimately diverge: `Zero Net Item` has `units == 1`
and `net_sales == 0.00` (fully comped, `Item Discounts == 8.00`). Never infer one
from the other, and never divide by units without handling zero.

## 7. Item identity and renaming

**This is the subtlest area in the project. Read carefully.**

### 7.1 Names are not stable identifiers
Menu items **can be renamed mid-history** (confirmed). Over a two-year backfill,
`"Alpha Lager"` may have been called something else in 2024. The export contains
**no item ID column** — only the display name.

Therefore: `item_name` is a *display string*, not an identity. A parser cannot
resolve renames, and must not pretend to.

### 7.2 The same name appears in multiple menu groups
An item is reported once per `Menu Group` it was rung against. Observed in the
weekly fixture — 2 names, 10 rows, 8 distinct names:

| Name | Rows | Total units |
|---|---|---|
| Alpha Lager | 2 | 15 |
| Gamma Soda | 2 | 12 |

The natural key for a row is `(item_name, sales_category, menu_group)` — verified
unique in both files (10/10 and 4/4). Do not deduplicate by name.

### 7.3 Do NOT auto-merge or fuzzy-match names
Fuzzy matching here is actively destructive. These are **different products**:

- `Rye's Bitter` and `Rye’s - Pint` — different items, and the
  first uses a straight apostrophe while the second uses a curly one (U+2019).
- `Alpha Lager`, `Alpha Lager - Pint`, `Alpha Lager - Pitcher`, and
  `Alpha Lager - Tower` — four distinct products.
- `Gamma Ale` and `Gamma Soda`.
- `Delta Shot` and `Delta Shot Reserve` (both `Spirits`).

Never merge two names because they look similar. Never strip size suffixes.

### 7.4 What the parser MUST do instead
Emit both:
- `item_name` — verbatim, exactly as in the file, no case changes.
- `item_key` — a normalised comparison key: Unicode **NFC**; curly apostrophes
  (U+2019, U+2018) mapped to ASCII `'`; whitespace collapsed and trimmed;
  otherwise unchanged. Do **not** lowercase (it risks merging distinct products
  on case-sensitive menus) and do not remove punctuation.

Apply the same normalisation to `menu_group` (it may contain a newline — replace
runs of whitespace, including newlines, with a single space in the *key* only;
keep the raw value verbatim in `menu_group`).

Rename resolution is **out of scope for the parser**. It will be handled later
by a human-curated alias table mapping historical names to a current product.
The parser's only obligation is to record raw names faithfully and provide a
consistent key.

### 7.5 Non-ASCII names are expected
Observed: `Café Blanco` (U+00E9), `Rye’s - Pint` (U+2019).

`item_key` is a **comparison key, not an ASCII slug**. It normalises apostrophe
variants and whitespace only. It must **not** strip accents and must **not** be
forced to pure ASCII:

- `Café Blanco` -> `item_key == "Café Blanco"` (the é is preserved)
- `Rye’s - Pint` -> `item_key == "Rye's - Pint"` (apostrophe only)

Stripping accents would risk conflating genuinely different menu items. If an
ASCII-only form is ever needed for search, that is a display concern for a later
stage, not the parser's job.

## 8. `menu_group` is unreliable

`menu_group` reflects how staff rang the item, not what it is. Non-alcoholic
items are routinely filed under spirits:

- `Gamma Soda` under `Spirits - Tequila`, `Spirits - Whiskey`, and `Wells`
- `Alpha Lager` under `Non-Alcoholic`, `Spirits - Tequila`, and `Wells`
- `Delta Shot` under `Spirits - Vodka`, `Spirits - Rum`, and `Non-Alcoholic`
- `Zero Net Item` under `Spirits - Whiskey`

Consequences:
- Do not validate category against menu group, and do not derive a hierarchy
  from `menu_group`.
- Do not build any rollup on `menu_group`.
- Store it raw; treat it as descriptive metadata only.

`sales_category` is usually one of `Alcohol`, `NA Drinks`, or `Food`, but it is
not a closed set. Current exports also contain `UNCATEGORIZED`, `Merch`, and
values with trailing whitespace such as `"Alcohol "`. Trim the value and store
it as-is; do not reject or remap unknown categories. Use it for grouping, since
it is still far more reliable than `menu_group`.

## 9. Data model

```python
@dataclass(frozen=True)
class SalesRow:
    item_name: str        # verbatim as exported
    item_key: str         # normalised lookup key
    sales_category: str   # trimmed; usually Alcohol | NA Drinks | Food, but open
    menu_group: str       # raw, may contain newline; unreliable
    units: Decimal        # Menu Item Quantity, GROSS units
    gross_sales: Decimal
    item_discounts: Decimal
    bill_discounts: Decimal
    net_sales: Decimal

@dataclass(frozen=True)
class SummaryRow:
    label: str
    units: Decimal
    gross_sales: Decimal
    item_discounts: Decimal
    bill_discounts: Decimal
    net_sales: Decimal

@dataclass(frozen=True)
class ParseResult:
    venue: str
    period_start: date
    period_end: date
    days_covered: int
    rows: list[SalesRow]
    summary: SummaryRow | None
    warnings: list[str]
```

`days_covered` is derived, not stored in the CSV.

For legacy 15-column files, `item_discounts` holds the combined `Discounts`
column and `bill_discounts` is `0.00`; `ParseResult.warnings` records that the
split is unavailable. For tax-split files the three tax columns are parsed but
not represented in the model.

## 10. Fixtures and golden values

Two synthetic fixtures live in `tests/fixtures/`. They contain no real sales
data; every name, date, and amount is fabricated. All numbers below are the
acceptance criteria.

### Fixture A — `Example_Venue-SalesByMenuItem-2026-11-08-2026-11-14.csv`

| Field | Value |
|---|---|
| venue | `Example_Venue` |
| period_start | `2026-11-08` (Sunday) |
| period_end | `2026-11-14` (Saturday) |
| days_covered | `7` |
| body rows | `10` |
| summary rows excluded | `1` |
| distinct item names | `8` |
| sum(units) | `Decimal("55.00")` |
| sum(gross_sales) | `Decimal("1502.56")` |
| sum(item_discounts) | `Decimal("13.00")` |
| sum(bill_discounts) | `Decimal("0.00")` |
| sum(net_sales) | `Decimal("1489.56")` |

Row-level spot checks (exact):

| item_name | units | net_sales |
|---|---|---|
| `Epsilon Combo` | `1.00` | `1234.56` |
| `Delta Shot` | `20.00` | `100.00` |
| `Beta Wine` | `2.00` | `25.00` |
| `Rye’s - Pint` | `3.00` | `10.00` |
| `Zero Net Item` | `1.00` | `0.00` |

Grouped-by-name aggregates (duplicate menu groups):

| item_name | rows | sum(units) |
|---|---|---|
| `Alpha Lager` | 2 | `15.00` |
| `Gamma Soda` | 2 | `12.00` |

### Fixture B — `Example_Venue-SalesByMenuItem-2026-11-15-2026-11-15.csv`

| Field | Value |
|---|---|
| venue | `Example_Venue` |
| period_start | `2026-11-15` |
| period_end | `2026-11-15` |
| days_covered | **`1`** (single day — must not be assumed to be a week) |
| body rows | `4` |
| summary rows excluded | `1` |
| distinct item names | `3` |
| sum(units) | `Decimal("8.00")` |
| sum(gross_sales) | `Decimal("31.00")` |
| sum(item_discounts) | `Decimal("0.00")` |
| sum(net_sales) | `Decimal("31.00")` |

Row spot checks:

| item_name | units | net_sales |
|---|---|---|
| `Alpha Lager` | `2.00` | `10.00` |
| `Delta Shot` | `3.00` | `9.00` |

Grouped aggregates:

| item_name | rows | sum(units) |
|---|---|---|
| `Gamma Soda` | 2 | `3.00` |

### Edge-case fixture — `Example_Venue-SalesByMenuItem-2026-11-16-2026-11-16.csv`

This fixture exists so the real-export edge cases discovered by the corpus scan
are covered by committed tests instead of only by the local `data/raw/` corpus.

| Field | Value |
|---|---|
| venue | `Example_Venue` |
| period_start / period_end | `2026-11-16` |
| days_covered | `1` |
| body rows | `3` |
| distinct item names | `3` |
| sum(units) | `Decimal("1203.00")` |
| sum(gross_sales) | `Decimal("1274.00")` |
| sum(item_discounts) | `Decimal("19.50")` |
| sum(net_sales) | `Decimal("1254.50")` |

Rows exercise:

| item_name | units | net_sales | Edge case |
|---|---|---|---|
| `Alpha Lager` | `24.00` | `79.50` | nonzero voids (`7.00` / `24.50`) and refunds (`-1.00` / `-3.50`) |
| `Beta Wine` | `2.00` | `-2.00` | discounts exceed gross sales, so net is negative |
| `Gamma Soda` | `1177.00` | `1177.00` | thousands separator in a body-row unit count (`1,177.00`) |

The refund/void row still has gross units: `Menu Item Quantity` is `24.00`, not
`23.00`.

### Cross-fixture facts
- Weekly `Alpha Lager` = `15` units; daily `Alpha Lager` = `2`.
- Weekly `Gamma Soda` = `12` units; daily `Gamma Soda` = `3`.
- Both files share byte-identical headers (14 columns).
- The `Epsilon Combo` body row uses thousands separators (`1,234.56`), as do
  the summary totals (`1,502.56`, `1,489.56`).

## 11. Testing requirements

- Tests first: write `tests/test_parse.py` before the implementation.
- Use `pytest`. Use `Decimal`, never `float`, in assertions.
- Compare `Decimal("1502.56") == Decimal("1502.56")` — note
  `Decimal("1502.56") == Decimal("1502.560")` is also `True`; do not rely on
  string formatting for equality.
- Cover at minimum:
  1. Both fixtures parse without error; all golden values above match.
  2. Summary row excluded — assert `len(result.rows)` and that the double-count
     does not occur.
  3. `days_covered == 7` and `days_covered == 1` respectively.
  4. `menu_group` containing an embedded newline parses as one field; the value
     `"Important" Beers &` + newline + `Seltzers` appears intact on a
     `Gamma Soda` row.
  5. Thousands separators parse: `Decimal("1,234.56")` → `Decimal("1234.56")`.
  6. `Zero Net Item` keeps `units == Decimal("1.00")` and
     `net_sales == Decimal("0.00")`.
  7. `item_key` replaces U+2019 with ASCII `'` in `Rye’s - Pint`, while
     `item_name` preserves the original codepoint. Assert the key is
     `"Rye's - Pint"`. Separately assert `Café Blanco` keeps its é in
     `item_key` (accents are not stripped).
  8. Duplicate names are preserved, not merged — `Alpha Lager` yields 2 rows
     totalling `15.00` units and `Gamma Soda` yields 2 rows totalling `12.00`.
  9. A malformed filename raises `FilenameParseError`.
  10. A row violating `gross - item_disc - bill_disc == net` raises
      `IntegrityError`.
  11. Both legacy layouts parse: the 16-column tax-split header and the
      15-column combined-`Discounts` header. The latter records a warning and
      maps `Discounts` to `item_discounts` with `bill_discounts == 0.00`.
  12. The edge-case fixture covers real-export observations that must not rely
      on the gitignored corpus: nonzero voids/refunds do not reduce gross units,
      a negative `net_sales` row parses, and a thousands separator in a
      body-row unit count parses.
  13. `sales_category` whitespace is trimmed in every layout, and unexpected
      values such as `UNCATEGORIZED` and `Merch` are preserved rather than
      rejected or remapped.

- Run the suite after every change and report actual output. Do not claim
  passing tests without pasting the run.

## 12. Style

- Python 3.11+. Type hints everywhere. `from __future__ import annotations`.
- `dataclasses` as specified. Frozen where given.
- Docstrings on public functions and dataclasses. State gross-units semantics in
  `SalesRow`.
- Parser exceptions in `parser/errors.py`: `ParseError` (base),
  `FilenameParseError`, `FormatError`, `IntegrityError`, `RowParseError`.
  Loader failures use `loader.errors.LoaderError`.
- Parser helpers are small and pure, under `src/barkeep/parser/`: `filename.py`,
  `money.py`, `normalise.py`, `parse.py`, `models.py`, `errors.py`.
- The parser has no third-party dependencies. `psycopg[binary]` is only in the
  `loader`/`dev` optional extras and must never be imported by the parser.
- Dependency direction is one-way: `parser <- loader <- ingest`. Do not import
  upward.

## 13. Loader and idempotency

The loader lives in `src/barkeep/loader/` and is invoked after parsing.

### 13.1 Weekly-only ingestion

- The parser must parse any valid report length (see §3). The **loader** is the
  layer that enforces weekly-only ingestion.
- `load_file(..., require_week=True)` (the default) rejects any report where
  `days_covered != 7` or `period_start.weekday() != 6` (Sunday) with
  `LoaderError`. Do not weaken this in the default path.
- `load_file` always rejects a report with no `REPORT SUMMARY` row with
  `ReportRejected`, regardless of `require_week`. It is recorded as `failed` in
  `core.ingest_log` with its identity and hash, and nothing is loaded.

### 13.2 Report identity and replacement

- Report identity is `(venue, period_start, period_end)` — **not** the filename.
- Hash the raw bytes with SHA-256. If a report already exists for the same
  identity with the same hash, the load is a `skipped` no-op.
- If the identity exists with a different hash (a re-export/revision), replace
  that report atomically in one transaction: delete its `sales_row` and
  `report_summary` rows, update the `report` metadata, then insert the new rows.
- A brand-new identity is a `loaded` insert.
- The whole load runs inside `with conn.transaction():`; a failure rolls back and
  never leaves a partial period.
- Re-loading the same period must never duplicate facts.
- The venue in a filename is unreliable in one specific way: a manual export
  from the web UI writes the venue name (`Bay_Horse_Tavern-SalesByMenuItem-…`),
  while the scheduled email attachment writes the TouchBistro location id
  (`54011-SalesByMenuItem-…`). Both describe one bar. `BARKEEP_VENUE_ALIASES`
  (`"54011=Bay_Horse_Tavern[,from=to]"`) is applied by the loader after parsing
  and before identity is computed, so both formats resolve to one report. This
  is explicit configuration, never inference: the parser still reports the
  filename's venue verbatim, and nothing fuzzy-matches venue names. A malformed
  alias entry, a self-mapping, or a cycle raises `LoaderError`.

### 13.3 Row identity

- A row's natural key is
  `(item_name, sales_category, menu_group)` within one report. This is enforced
  by `core.sales_row` and by the parser, which raises `IntegrityError` on
  duplicates within one file.

### 13.4 Ingest log

- `core.ingest_log` is append-only and records every attempt with status
  `loaded`, `replaced`, `skipped`, or `failed`, plus filename, hash, parser
  version, and optional message.
- `load_directory` continues after a failure, recording each failure via
  `record_failure` instead of aborting the batch.

### 13.5 Migrations

- Numbered SQL files in `sql/` applied in filename order by
  `barkeep.loader.migrate`. Applied filenames are recorded in
  `public.schema_migrations`; each file runs in its own transaction.
- No Alembic. Add a new numbered `NNN_*.sql` file for each schema change; never
  edit an already-applied migration.
- The default SQL directory assumes a `src/` checkout. In a non-editable
  install (the Docker image) set `BARKEEP_SQL_DIR` to the copied `sql/` path.

## 14. Schema and analytics views

- `sql/001_schema.sql` defines `core.report`, `core.sales_row`,
  `core.report_summary`, `core.ingest_log`, plus constraints:
  - `report_period_unique (venue, period_start, period_end)`
  - `sales_row_natural_key (report_id, item_name, sales_category, menu_group)`
  - `sales_row_arithmetic` (`gross_sales - item_discounts - bill_discounts =
    net_sales`)
  - `days_covered = (period_end - period_start) + 1`
- `sql/002_views.sql` defines the BI contract in the `analytics` schema:
  `v_sales_current`, `v_weekly_totals`, `v_weekly_item`, `v_weekly_category`,
  `v_item_catalog`.
- BI tools (Metabase first) must consume `analytics.*` views only, never
  `core.*` base tables. A read-only role for BI is preferred over granting write
  access.
- `v_sales_current` exposes both `menu_group_raw` (verbatim, may contain a
  newline) and `menu_group` (whitespace runs collapsed for display). Keep both.
- Dashboard logic must stay portable: any aggregation worth reusing belongs in a
  view, not in a single BI tool's saved question.

## 15. Email ingestion

- `src/barkeep/ingest/` polls the report mailbox over IMAP and loads CSV
  attachments through the loader. The parser stays unaware of email.
- `attachments.extract_csv_attachments` accepts only `*.csv` filenames
  containing `SalesByMenuItem`; other attachments are ignored.
- `imap.run_once` selects the configured mailbox/label and searches by
  sender/subject/`SINCE` — **never** by read state. A report must still be found
  after someone opens it in a mail client; duplicate protection comes from the
  loader's content hash.
- Connections use `ssl.create_default_context()`; never rely on `imaplib`'s
  default context, which does not verify certificates.
- `IMAP_SENDER` is required. It is both a server-side search filter and a
  post-fetch From allowlist (full address, or a bare domain). Messages whose
  From does not match are skipped untouched. This prevents unsolicited mail to
  the alias from injecting or replacing reports.
- Messages without a report attachment are left untouched, including their
  read/unread state. Never mark unrelated mail as seen.
- A report message is marked `\Seen` only after all its attachments load
  without failure. Failures are recorded in `core.ingest_log` and the message is
  left unread for retry.
- Use a dedicated label/folder (`IMAP_MAILBOX`, e.g. `SalesReports`) rather than
  `INBOX`, so the poller only ever sees report mail.
- Configuration comes from `IMAP_HOST`, `IMAP_USER` (the real account, not the
  alias), `IMAP_PASSWORD` or `IMAP_PASSWORD_FILE`, `IMAP_MAILBOX`, required
  `IMAP_SENDER`, optional `IMAP_SUBJECT`, and `IMAP_SINCE_DAYS` (see
  `.env.example`). Use an app password on a dedicated mailbox; never commit
  credentials.
- The poller is a single pass (`run_once`). Schedule it externally — daily is
  recommended so a late or missed report self-heals on the next run.
- Silence is not success. An empty search prints a `note:` naming the mailbox
  and the window, and the CLI prints a `warning:` when the newest loaded period
  ended more than ten days ago. Neither changes the exit status (an empty
  mailbox is legal), because the failure they catch — a Gmail filter that
  stopped applying the label — would otherwise be indistinguishable from a
  quiet week.

## 16. Deployment

Deployment target is an always-on Linux host running Docker Compose.

- `Dockerfile` installs the package with the `loader` extra, copies `sql/` and
  `scripts/`, sets `BARKEEP_SQL_DIR=/app/sql`, and runs as an unprivileged user.
- `compose.yaml` is production-shaped: `db` publishes no port, `metabase` is
  bound to loopback and stores its app data in the `metabase` database, and
  `migrate`, `ingest`, and `backfill` sit behind the `ingest` profile, so
  `docker compose up -d` starts only the database and Metabase.
- Metabase's AI features call an OpenAI-compatible endpoint, and refuse any
  private address unless `MB_LLM_ALLOWED_NETWORKS` says otherwise. The variable
  is passed through from `.env` and empty by default, so a LAN model is an
  explicit opt-in; use `allow-private`, never `allow-all`, which would also
  permit loopback. Changing it requires recreating the container, since it is
  read from the environment at start.
- `compose.dev.yaml` is a workstation override that publishes Postgres for
  host-run tools and tests. Production uses `compose.yaml` alone.
- `deploy/compose.lan.yaml` is the production override for a host with a reverse
  proxy: it attaches `metabase` to the external `edge` Docker network so a proxy
  in another Compose project can reach `metabase:3000`. It publishes nothing new;
  the loopback port stays for SSH tunnels. The proxy itself is host
  infrastructure, not part of this package — `deploy/edge/` is a worked example.
- `ingest` runs migrations then one poll pass and exits. Trigger it daily from a
  host timer; `deploy/barkeep-ingest.service` and `.timer` are samples. Do not
  add an in-container sleep loop.
- `deploy/barkeep-backup.service` and `.timer` run `scripts/backup.sh`:
  `docker compose exec` `pg_dump`, gzip, then verify (non-empty, valid gzip, ends
  with pg_dump's completion marker) before rotating. A failed backup must never
  delete the last good copy. Off-box copying is optional rsync via
  `/etc/barkeep/backup.env`; a dump on the same disk is not a backup.
- `scripts/preflight.sh` is the read-only host check (distro, systemd, Docker,
  Compose v2, docker group, ports 80/443, memory, disk, NTP). It exits nonzero
  only for conditions that make the deployment impossible.
- `backfill` mounts `data/raw` read-only and runs `scripts/backfill.py` for the
  initial load. It applies migrations first.
- Secrets: the IMAP app password is a Compose secret mounted at
  `/run/secrets/imap_password` and read via `IMAP_PASSWORD_FILE`. The DSN can be
  supplied via `DATABASE_URL` or `DATABASE_URL_FILE`. Secret files are
  gitignored and never passed on the command line. File secrets are bind-mounted
  on Linux, so the host owner and mode are what the container sees: the image
  runs as UID 10001, so `imap_password` must be `10001:10001` mode `400` and the
  `640`/group recipe does not work. The value must also be exactly 16 bytes — a
  trailing newline or Google's four-group display with spaces fails identically
  to a revoked password.
- Container `DATABASE_URL` uses the Compose service name `db`, not `localhost`.

## 17. Explicitly out of scope

Do not write, suggest, or scaffold any of:
- rename detection, fuzzy matching, alias tables (handled later by a curated
  alias table, outside this project)
- Metabase dashboard definitions checked into this repo, or any BI-tool-specific
  query logic that belongs in a view
- always-running ingest daemons or in-process schedulers; use an external timer
- backfilling via HTTP calls to TouchBistro itself
- vendor-prefixed identifiers (`touchbistro_*`, `pos_parser`)

If a task seems to need one of these, stop and ask.
