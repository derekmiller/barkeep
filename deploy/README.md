# Deployment

Two environments share one image:

| | Workstation (macOS) | Production (Linux host `roadkill`) |
|---|---|---|
| Compose files | `compose.yaml` + `compose.dev.yaml` | `compose.yaml` + `deploy/compose.lan.yaml` |
| Postgres port | published on `127.0.0.1:5432` | not published |
| Metabase | `127.0.0.1:3000`, reached directly | `127.0.0.1:3000`, reached through Caddy |
| Ingest | `run --rm ingest` by hand | `barkeep-ingest.timer` |
| Backups | the laptop's own backups | `barkeep-backup.timer` |

Production topology on `roadkill`:

```
        Pi-hole DNS                    Caddy (project: edge)
   barkeep.roadkill.lan  ──┐        ┌────────────────────────┐
   roadkill.lan          ──┴───────▶│ :80 :443, tls internal │
                                     └───────────┬────────────┘
                                                 │ edge network
   ┌─────────────────────────────────────────────▼──────────────┐
   │ project: barkeep                                           │
   │   metabase  ──▶ :3000 (loopback published, for tunnels)    │
   │   db        ──▶ Postgres, unpublished                      │
   │   ingest    ──▶ one IMAP pass, run by a systemd timer      │
   └────────────────────────────────────────────────────────────┘
```

Run the steps in order; each ends with a check that must pass before the next
one means anything.

## 0. Preflight

```bash
./scripts/preflight.sh
```

Read-only. `FAIL` means the deployment cannot work as written (no systemd, no
Compose v2, user not in the `docker` group); `WARN` means decide, don't ignore.

## 1. Provision the host

Ubuntu/Debian, systemd, Docker CE from Docker's apt repository.

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install ca-certificates curl gnupg ufw rsync git

sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
ARCH=$(dpkg --print-architecture)
CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' "$ARCH" "$CODENAME" | sudo tee /etc/apt/sources.list.d/docker.list

sudo apt update
sudo apt -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Bounded container logs, because the default `json-file` driver grows forever:

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "log-driver": "local",
  "log-opts": { "max-size": "10m", "max-file": "3" },
  "live-restore": true
}
EOF
sudo systemctl restart docker
```

Users and firewall. `docker` group membership is root-equivalent, so keep it to
the admin and the service account:

```bash
sudo usermod -aG docker "$USER"          # re-login for this to take effect
sudo useradd --system --create-home --home-dir /var/lib/barkeep --shell /usr/sbin/nologin barkeep
sudo usermod -aG docker barkeep

sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 192.168.0.0/24 to any port 22 proto tcp
sudo ufw allow from 192.168.0.0/24 to any port 80 proto tcp
sudo ufw allow from 192.168.0.0/24 to any port 443 proto tcp
sudo ufw --force enable

sudo timedatectl set-ntp true
```

Adjust the subnet. **`ufw` does not filter ports published by containers** —
Docker inserts its own iptables rules ahead of it. That is fine here: Postgres is
never published, and Caddy's 80/443 are the only published ports. When you add
another service, do not assume `ufw` is protecting its published port.

## 2. DNS

In Pi-hole (Local DNS → DNS Records), one A record per name, all pointing at the
host:

```
roadkill.lan          -> 192.168.0.231
barkeep.roadkill.lan  -> 192.168.0.231
```

Also set a DHCP reservation for that address. A DNS record is only as stable as
the lease: if the address moves, the name resolves to whatever took it, which is
worse than a name that fails.

## 3. Deploy barkeep

Create the checkout owned by the admin user, so `git pull` and edits never need
`sudo`; the service account reads it through ordinary file permissions.

```bash
sudo install -d -o "$USER" -g "$USER" /opt/barkeep
```

If the repository is private, the clone needs credentials. A read-only deploy key
beats a personal token: it cannot push, and it is scoped to this one repo.

```bash
ssh-keygen -t ed25519 -N '' -C "$(hostname)-deploy" -f ~/.ssh/barkeep_deploy
cat ~/.ssh/barkeep_deploy.pub
```

Add the public key under the repository's **Settings → Deploy keys**, read access
only, and tell SSH to use it for this host:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/barkeep_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
ssh -T git@github.com      # "successfully authenticated" without a shell
```

Then clone, over SSH for a private repo or HTTPS if it is public:

```bash
git clone git@github.com:derekmiller/barkeep.git /opt/barkeep
# public alternative:
# git clone https://github.com/derekmiller/barkeep.git /opt/barkeep

cd /opt/barkeep
git log --oneline -1       # expect 30f6bf7 or later
cp .env.example .env
```

Fill in `.env`. The values that matter in production:

| Variable | Why |
|---|---|
| `IMAP_SENDER` | required; the From allowlist. Unsolicited mail to the alias cannot inject reports |
| `IMAP_USER`, `IMAP_MAILBOX` | the real account and the dedicated label, never `INBOX` |
| `BARKEEP_VENUE_ALIASES` | `54011=Bay_Horse_Tavern`. **Commented out in `.env.example`.** Without it the scheduled report's location id creates a second venue that double-counts every week |
| `POSTGRES_PASSWORD` | container credential; a distinct value here is cheap |
| `DATABASE_URL` | only used by host-run tools; containers use the `db` service name |

Create the app password (a separate one from any workstation instance, so either
can be revoked alone):

```bash
mkdir -p secrets
read -rsp 'App password: ' P
printf '%s' "$P" > secrets/imap_password
unset P; echo
chmod 700 secrets && chmod 600 secrets/imap_password
```

Give the service user read access to exactly the configuration it needs — the
timer runs as `barkeep`, which is not you:

```bash
sudo chgrp barkeep .env secrets secrets/imap_password
sudo chmod 640 .env secrets/imap_password
sudo chmod 750 secrets
```

Build and start. The `edge` network is created once and survives `down`:

```bash
sudo -u barkeep docker network create edge 2>/dev/null || true
docker compose -f compose.yaml -f deploy/compose.lan.yaml --profile ingest build
docker compose -f compose.yaml -f deploy/compose.lan.yaml up -d
```

`--profile ingest` is required for `build`: `migrate`, `ingest`, and `backfill`
sit behind that profile, and Compose only acts on active services, so without it
the command reports `No services to build` and leaves you with no image.

`up -d` deliberately omits the profile — it starts `db` and `metabase` only.
Adding `--profile ingest` here would launch `ingest` and `backfill` as
containers; they are one-shot jobs driven by `run --rm` or the timers.

Check:

```bash
docker compose ps                                  # db healthy, metabase up
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000/api/health
```

Expect `200`. Metabase's first start runs its own migrations and takes a minute;
`docker compose logs -f metabase` shows `Metabase Initialization COMPLETE`.

## 4. Load the history

`data/raw/` is gitignored, so the clone has nothing to load. Copy it from the
workstation that has the corpus:

```bash
rsync -av --chmod=600 data/raw/ roadkill:/opt/barkeep/data/raw/
```

Then, on the host:

```bash
docker compose -f compose.yaml -f deploy/compose.lan.yaml --profile ingest run --rm backfill
```

The compose service applies migrations, then loads every CSV. Expect a run of
`loaded:` lines and a final `114 files: loaded=114`.

Verify against the known history:

```bash
docker compose exec -T db psql -U barkeep -d barkeep -c "
  SELECT (SELECT count(*) FROM core.report)         AS reports,
         (SELECT count(*) FROM core.sales_row)      AS rows,
         (SELECT count(*) FROM core.report_summary) AS summaries,
         (SELECT min(period_start) FROM core.report) AS first_week,
         (SELECT max(period_end)   FROM core.report) AS last_week"
```

Expect `114 | 13331 | 114 | 2024-09-08 | 2026-11-14`. Then check that the weeks
are contiguous and that no row breaks the arithmetic identity (both should
return zero rows):

```bash
docker compose exec -T db psql -U barkeep -d barkeep -c "
  SELECT count(*) AS gaps FROM (
    SELECT period_start,
           lag(period_end) OVER (ORDER BY period_start) AS prev_end
    FROM core.report
  ) t
  WHERE prev_end IS NOT NULL AND period_start <> prev_end + 1"

docker compose exec -T db psql -U barkeep -d barkeep -c "
  SELECT count(*) FROM core.sales_row
  WHERE gross_sales - item_discounts - bill_discounts <> net_sales"
```

If the backfill reports `0 files`, the directory is empty — the mount is
read-only and silently accepts nothing.

## 5. Edge proxy

Caddy lives outside this repo because it will serve every service on the host.

```bash
sudo mkdir -p /opt/edge && sudo chown "$USER": /opt/edge
cp deploy/edge/compose.yaml deploy/edge/Caddyfile /opt/edge/
cd /opt/edge && docker compose up -d
```

Then trust Caddy's local CA on the devices you browse from, or every page warns:

```bash
cd /opt/edge
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt > caddy-root.crt
```

- macOS: double-click it, add to the **System** keychain, then set it to *Always
  Trust*.
- iOS: AirDrop it, install the profile, then Settings → General → About →
  Certificate Trust Settings → enable full trust.

Verify from a laptop, not the host:

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' https://barkeep.roadkill.lan/api/health
```

`200` means DNS, TLS, the `edge` network, and Metabase all agree. A `502` means
Caddy cannot reach `metabase:3000` — usually Metabase is not on the `edge`
network, i.e. started without `deploy/compose.lan.yaml`.

## 6. Timers

Four units: the ingest and backup services, each with a daily timer.

```bash
sudo cp deploy/barkeep-ingest.service deploy/barkeep-ingest.timer \
        deploy/barkeep-backup.service deploy/barkeep-backup.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
```

Dry-run both before trusting the schedule:

```bash
sudo systemctl start barkeep-ingest
journalctl -u barkeep-ingest -n 50 --no-pager

sudo systemctl start barkeep-backup
journalctl -u barkeep-backup -n 20 --no-pager
```

The ingest dry run should print a wall of `skipped: … (identical report already
loaded)` — the backfill already holds every week the mailbox offers. That is the
proof that search, sender filter, attachment extraction, and hash matching work
against the real server. `loaded:` is a week the backfill missed; `failed:` names
a file; `error:` is a single line for a configuration or connection problem.

Then enable:

```bash
sudo systemctl enable --now barkeep-ingest.timer barkeep-backup.timer
systemctl list-timers 'barkeep-*'
```

Ingest fires daily at 06:15 host time (catch-up after downtime via `Persistent`),
backup at 03:30 with a randomised delay. `OnCalendar` is local time; add
`TimeZone=` to a timer if the host runs UTC and you schedule in local time.

On failure a unit retries at five-minute intervals, three attempts within two
hours, then waits for the next daily firing — a revoked app password does not
hammer Gmail all day.

`Requires=docker.service` matches the Docker CE and RHEL packages. A
socket-activated install needs `docker.socket` instead. Both units hard-code
`/opt/barkeep`; edit `WorkingDirectory`, `Documentation`, and `ExecStart` if the
checkout lives elsewhere.

## 7. Backups

Dumps land in `/var/lib/barkeep-backup`, mode `0700`, owned by `barkeep`; 14 days
are kept. Each dump is verified (non-empty, valid gzip, ends with pg_dump's
completion marker) before rotation, so a failed backup never deletes the last
good copy.

Copy them off the host — a backup on the same disk is a backup of nothing. Create
a key for the service user and allow it to rsync to another machine:

```bash
sudo -u barkeep ssh-keygen -t ed25519 -N '' -C barkeep-backup
sudo -u barkeep ssh-copy-id you@workstation
sudo tee /etc/barkeep/backup.env >/dev/null <<'EOF'
BARKEEP_BACKUP_REMOTE=you@workstation:/Users/you/barkeep-backups
BARKEEP_BACKUP_KEEP=30
EOF
```

Prove a restore works at least once. This is the step that turns a file into a
backup:

```bash
DUMP=$(sudo ls -1t /var/lib/barkeep-backup/*.sql.gz | head -1)
docker compose exec -T db psql -U barkeep -d postgres \
  -c 'DROP DATABASE IF EXISTS restore_test' -c 'CREATE DATABASE restore_test'
sudo zcat "$DUMP" | docker compose exec -T db psql -q -U barkeep -d restore_test
docker compose exec -T db psql -U barkeep -d restore_test -c \
  'SELECT count(*) AS reports FROM core.report'
docker compose exec -T db psql -U barkeep -d postgres -c 'DROP DATABASE restore_test'
```

Expect `114`. The dump includes the `analytics` views, so a restored database is
immediately usable by BI.

## 8. Cut over from the workstation

Two hosts polling the same mailbox into two different databases is not harmful
but it is confusing: Metabase shows one of them while the other keeps moving.

```bash
# on the workstation
docker compose -f compose.yaml -f compose.dev.yaml down
```

Delete the workstation's `secrets/imap_password` if the instance is retiring, and
revoke that app password in the Google account. The workstation's volume can stay
as an archive; do not point its Metabase at production.

## 9. Routine operations

| Task | Command |
|---|---|
| Poll now | `docker compose -f compose.yaml -f deploy/compose.lan.yaml --profile ingest run --rm ingest` |
| Back up now | `sudo systemctl start barkeep-backup` |
| Logs for the timer | `journalctl -u barkeep-ingest --since yesterday` |
| Query the database | `docker compose exec db psql -U barkeep -d barkeep` |
| Upgrade the code | see [Upgrading](#upgrading) |
| Apply new migrations | `docker compose -f compose.yaml -f deploy/compose.lan.yaml --profile ingest run --rm migrate` |
| Edge proxy changes | `cd /opt/edge && docker compose up -d` |

### Upgrading

```bash
cd /opt/barkeep
git pull
docker compose -f compose.yaml -f deploy/compose.lan.yaml --profile ingest build
docker compose -f compose.yaml -f deploy/compose.lan.yaml up -d
```

Not `up -d --build`: the profile-gated services would be skipped, and the timers
would keep running the previous image while `git log` says otherwise.

Schema changes apply on the next ingest or backfill run. To apply them now:

```bash
docker compose -f compose.yaml -f deploy/compose.lan.yaml --profile ingest run --rm migrate
```

Both `-f` files matter on every command, including `run`: omitting
`deploy/compose.lan.yaml` makes Compose resolve a different config for `db` and
can recreate it without the network the proxy expects.

## 10. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `error: cannot connect to IMAP host` | host DNS or egress: `getent hosts imap.gmail.com` on the host |
| `error: missing IMAP configuration: SENDER` | `IMAP_SENDER` empty in `.env` |
| `error: … secret …` on the timer only | `barkeep` cannot read `secrets/imap_password`; check group and mode |
| Ingest runs, prints nothing, exits 0 | the label is empty or `IMAP_SENDER` does not match the sender; nothing was found, not an error |
| `502` from Caddy | Metabase not on `edge`: start with `deploy/compose.lan.yaml` |
| Certificate warning in the browser | Caddy's root CA not installed on that device (step 5) |
| `no such host` for `barkeep.roadkill.lan` | Pi-hole record missing, or the client caches the old answer |
| Timer never fires | `systemctl list-timers barkeep-ingest.timer`; blank `NEXT` means not enabled |
| Backfill loads `0 files` | `data/raw` empty on the host |
| `No services to build` | `build` ran without `--profile ingest`; Compose skips profile-gated services |
| Weeks doubled under two venues | `BARKEEP_VENUE_ALIASES` missing from the host `.env` |
