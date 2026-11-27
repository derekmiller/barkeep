# barkeep

Turns TouchBistro `SalesByMenuItem` CSV exports into queryable Postgres, so a
bar's sales history can be charted in Metabase without anyone touching a
spreadsheet.

```
TouchBistro scheduled report (email, CSV attachment)
    -> barkeep.ingest      IMAP poller
    -> barkeep.parser      pure CSV -> typed rows
    -> barkeep.loader      idempotent Postgres replace
    -> analytics.* views
    -> Metabase / any other BI tool
```

Three packages, one direction of dependency:

- **`barkeep.parser`** is a pure function. Same bytes in, same objects out — no
  database, no network, no clock, no filesystem. Zero third-party dependencies.
- **`barkeep.loader`** owns Postgres: migrations, the transactional replace, the
  ingest log. It needs `psycopg`.
- **`barkeep.ingest`** owns IMAP. It knows nothing about parsing beyond handing
  bytes to the loader.

The TouchBistro format is confined to the parser's behaviour. It does not appear
in package names, module names, or the public API.

## Project layout

```
sql/                      001_schema.sql, 002_views.sql
src/barkeep/
  parser/                 filename.py money.py normalise.py models.py parse.py
  loader/                 db.py load.py migrate.py secrets.py venues.py
  ingest/                 imap.py attachments.py __main__.py
scripts/                  backfill.py backup.sh preflight.sh
deploy/                   runbook, systemd units, LAN compose override
tests/                    parser/, loader/, ingest/ + fixtures
compose.yaml              db + Metabase; migrate/ingest/backfill behind a profile
```

## Quick start

The parser runs on its own, with nothing installed but Python 3.11:

```python
from pathlib import Path

from barkeep.parser import parse_csv

data = Path("Example_Venue-SalesByMenuItem-2026-11-08-2026-11-14.csv").read_bytes()
result = parse_csv(data, "Example_Venue-SalesByMenuItem-2026-11-08-2026-11-14.csv")

result.venue          # "Example_Venue"
result.period_start   # date(2026, 11, 8) — taken from the filename, not the rows
result.days_covered   # 7
sum(row.units for row in result.rows)   # == result.summary.units
```

Money and quantities are `decimal.Decimal` throughout. Never `float`.

Install for development, including the database and test extras:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

`pytest` deselects two opt-in markers by default:

```bash
pytest -m corpus       # scans data/raw (or BARKEEP_CORPUS) for real-export shapes
pytest -m db           # needs a running Postgres and DATABASE_URL
```

## Loading and ingesting

With a database up — see [Quick start with Docker](#with-docker) below — apply
migrations, then load files:

```bash
python -m barkeep.loader.migrate
python scripts/backfill.py data/raw        # every weekly CSV in a directory
```

Poll the report mailbox once and exit:

```bash
python -m barkeep.ingest
```

That is the whole ingest contract: it is a single pass with no loop and no
scheduler. Something external runs it daily.

### What the loader guarantees

- Report identity is `(venue, period_start, period_end)`, **not** the filename.
- Identical bytes for a known period are a `skipped` no-op.
- Different bytes for a known period replace that period atomically, in one
  transaction. A failure never leaves half a week behind.
- A report with no `REPORT SUMMARY` row is refused: a file truncated cleanly
  between rows still parses, and the summary is the only thing that reveals the
  missing tail.
- Loading is weekly-only by default (`require_week=True`); the parser itself
  accepts any report length.
- Every attempt is appended to `core.ingest_log` as `loaded`, `replaced`,
  `skipped`, or `failed`.

### With Docker

```bash
cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml up -d db metabase
docker compose -f compose.yaml -f compose.dev.yaml --profile ingest run --rm migrate
docker compose -f compose.yaml -f compose.dev.yaml --profile ingest run --rm backfill
```

`compose.dev.yaml` publishes Postgres for host-run tools. Production is
`compose.yaml` alone, where the database publishes nothing.

## Database

`core.*` holds the facts (`report`, `sales_row`, `report_summary`, `ingest_log`)
and enforces the invariants — one row per item/category/menu-group within a
report, and `gross_sales - item_discounts - bill_discounts = net_sales` as a
check constraint, not a convention.

`analytics.*` is the BI contract:

| View | Grain |
|---|---|
| `v_sales_current` | item rows, with venue and period attached |
| `v_weekly_totals` | one row per venue per week |
| `v_weekly_item` | item totals per week |
| `v_weekly_category` | category totals per week |
| `v_item_catalog` | every distinct item per venue and category, for search and filters |

BI tools read `analytics.*` only, never `core.*`. Dashboard logic that is worth
reusing belongs in a view, not in a saved question.

Schema changes are numbered SQL files in `sql/`, applied in filename order and
recorded in `public.schema_migrations`. No Alembic. An applied migration is
never edited.

## Configuration

Copy `.env.example` to `.env`. The values that matter:

| Variable | Notes |
|---|---|
| `DATABASE_URL` | or `DATABASE_URL_FILE`; containers use the `db` service name |
| `IMAP_HOST`, `IMAP_USER` | the real account, never an alias |
| `IMAP_PASSWORD` / `IMAP_PASSWORD_FILE` | a 16-character app password; prefer the file form in Docker |
| `IMAP_MAILBOX` | a dedicated label such as `SalesReports`, never `INBOX` |
| `IMAP_SENDER` | **required**; both the server-side search filter and a post-fetch From allowlist |
| `IMAP_SUBJECT`, `IMAP_SINCE_DAYS` | optional subject filter and lookback window |
| `BARKEEP_VENUE_ALIASES` | `54011=Bay_Horse_Tavern` — maps the location id in scheduled exports onto the venue name a manual export uses |

## What it deliberately does not do

- **Rename detection or fuzzy item matching.** Menu items get renamed, and names
  differ by size suffix (`Alpha Lager` vs `Alpha Lager - Pint`). Merging them
  silently destroys data; a human-curated alias table is the plan, and it is not
  in this package.
- **BI dashboards or tool-specific query logic.** Views are the shared layer.
- **A long-running ingest daemon.** Use a host timer.

## Deployment

Production is an always-on Linux host running Docker Compose, with Metabase
behind a reverse proxy and two systemd timers (ingest, backup):
[`deploy/README.md`](deploy/README.md) is the runbook — host provisioning, the
secret file's exact ownership and length, TLS, backups, and upgrades.

## Where the rules live

- [`AGENTS.md`](AGENTS.md) — the specification: the filename contract, the three
  accepted header layouts, the summary-row trap, gross-unit semantics, item
  identity, testing requirements. Read it before changing parser behaviour.
- [`deploy/README.md`](deploy/README.md) — how it runs in production.
- This file — the overview, for anyone arriving cold.
