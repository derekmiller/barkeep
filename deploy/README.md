# Deployment

Two environments share the same image and services:

- **development** — macOS workstation; run things on demand.
- **production** — always-on Linux host; `ingest` runs from a systemd timer.

`compose.yaml` is production-shaped: it does not publish Postgres, and Metabase
is bound to loopback. `compose.dev.yaml` adds the Postgres port for a workstation.

## Secrets

Both environments read the Gmail app password as a Compose secret:

```bash
mkdir -p secrets
read -rsp 'App password: ' P
printf '%s' "$P" > secrets/imap_password
unset P
echo
chmod 700 secrets
chmod 600 secrets/imap_password
```

The file is gitignored. Use a separate Google app password per environment
(e.g. `barkeep-dev`, `barkeep-prod`) so either can be revoked independently.

## Development (macOS)

```bash
cp .env.example .env          # set IMAP_SENDER plus the other IMAP_* values
docker compose -f compose.yaml -f compose.dev.yaml up -d db metabase
```

`IMAP_SENDER` is required: it is the From allowlist that decides which mail may
be ingested. Use TouchBistro's sending address, or a bare domain.

Run a poll or a backfill on demand:

```bash
docker compose -f compose.yaml -f compose.dev.yaml --profile ingest run --rm ingest
docker compose -f compose.yaml -f compose.dev.yaml --profile ingest run --rm backfill
```

Pass **both** files for every `--profile ingest` command on a workstation.
Compose compares the resolved config on each invocation, so omitting
`compose.dev.yaml` recreates `db` from the production-shaped config and silently
drops the published `127.0.0.1:5432` port. Host-run tests and tools then fail
with `connection refused` until you bring the dev stack back up. The data itself
lives in the `pgdata` volume and is not lost.

Images build for the workstation architecture, which is fine for local work.

## Production (Linux)

```bash
git clone <repo> /opt/barkeep
cd /opt/barkeep
cp .env.example .env          # set POSTGRES_*, IMAP_*, and a distinct password if desired
# create secrets/imap_password as above
docker compose build          # build natively on the server (amd64)
docker compose up -d          # db + metabase only
```

The server's `.env` `DATABASE_URL` is only used by host-run tools and tests;
containers always use the `db` service name.

### Initial backfill

`data/raw/` is gitignored and intentionally not in the repo. Copy it once:

```bash
rsync -av --chmod=600 data/raw/ user@prod:/opt/barkeep/data/raw/
```

Then, on the server:

```bash
docker compose --profile ingest run --rm backfill
```

### Scheduling

```bash
sudo cp deploy/barkeep-ingest.service deploy/barkeep-ingest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now barkeep-ingest.timer
```

The service runs as `barkeep`; that user must be in the `docker` group (or run
the unit as root) and must own `secrets/imap_password`. Edit `WorkingDirectory`
if the repo is not at `/opt/barkeep`.

## Metabase

Metabase stores its application data in the `metabase` database, created by
`docker/init/01-metabase-db.sql` on first Postgres initialisation. If the
`pgdata` volume already exists without that database:

```bash
docker compose exec db psql -U barkeep -d postgres -c 'CREATE DATABASE metabase;'
```

Metabase listens on `127.0.0.1:3000`; put a TLS reverse proxy in front of it.
Use the `analytics.*` views in a read-only database role, not `core.*` tables.

## Backups

The `pgdata` volume is the only copy of the data. Schedule a dump:

```bash
docker compose exec -T db pg_dump -U barkeep -d barkeep | gzip > barkeep-$(date +%F).sql.gz
```

Keep dumps off the host (object storage or another machine) and rotate them.
