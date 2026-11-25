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
# Rotating an existing password: the old file is owned by the container user at
# mode 400, so it cannot be overwritten in place — remove it first.
sudo rm -f secrets/imap_password
read -rsp 'App password: ' P
printf '%s' "$P" > secrets/imap_password
unset P; echo
```

The value must be exactly **16 bytes**. Two mistakes produce the same
`[AUTHENTICATIONFAILED]` as a revoked password, and both are common: a trailing
newline (`echo` instead of `printf`, 17 bytes) and Google's four-group display
pasted verbatim (`abcd efgh ijkl mnop`, 19 bytes). Strip and confirm:

```bash
tr -d '[:space:]' < secrets/imap_password > secrets/imap_password.clean &&
  mv secrets/imap_password.clean secrets/imap_password
sudo stat -c '%s bytes' secrets/imap_password      # 16
```

Then ownership. Two files, two different readers, no overlap:

| Path | Read by | Owner / mode |
|---|---|---|
| `.env` | `docker compose`, as `barkeep` | `barkeep:barkeep` `640` |
| `secrets/` (directory) | Compose, which stats the path | `barkeep:barkeep` `750` |
| `secrets/imap_password` | the container, as UID **10001** | `10001:10001` `400` |

Compose bind-mounts file secrets on Linux, so the container sees the *host*
owner and mode. `chgrp barkeep` and `640` looks reasonable and leaves the poller
unable to read its own password:

```bash
sudo chown barkeep:barkeep .env secrets
sudo chmod 640 .env && sudo chmod 750 secrets
sudo chown 10001:10001 secrets/imap_password
sudo chmod 400 secrets/imap_password
```

Prove it with the reader production actually uses, not with `sudo cat`:

```bash
docker compose -f compose.yaml -f deploy/compose.lan.yaml --profile ingest \
  run --rm --entrypoint sh ingest -c 'wc -c < /run/secrets/imap_password'
```

`16` is the only acceptable answer. `Permission denied` means the owner and mode
are wrong; any other number means the password itself is malformed or truncated.

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
rsync -av data/raw/ roadkill:/opt/barkeep/data/raw/
```

No `--chmod`: macOS ships openrsync, which rejects it. The corpus is mounted
read-only and the image runs as **UID 10001**, so the files must be readable by
that user. Do not lock them to 600:

```bash
sudo chmod -R a+rX /opt/barkeep/data/raw
```

`a+rX` grants `r` to everyone and adds execute only to directories — enough to
traverse and read, and the `:ro` mount makes the whole thing immutable anyway.
Get this wrong and the loader fails with `PermissionError: [Errno 13] Permission
denied: '/data/raw'` before it opens a single CSV, because `iterdir()` cannot
traverse the directory.

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

Caddy, TLS, and the host's DNS names are not in this repo: they front every
service on the host, so they live in
[`derekmiller/homelab`](https://github.com/derekmiller/homelab), deployed at
`/opt/homelab/hosts/roadkill`. Barkeep's only stake in that repo is one site
block, and this is all of it:

```
barkeep.roadkill.lan {
	tls internal
	reverse_proxy metabase:3000
}
```

Deploy it, and keep it updated, from that repo:

```bash
sudo mkdir -p /opt/homelab && sudo chown "$USER": /opt/homelab
git clone https://github.com/derekmiller/homelab.git /opt/homelab
cd /opt/homelab/hosts/roadkill && docker compose up -d
```

HTTPS, not SSH: the repo is public and the host only pulls, so it needs no
GitHub key. Updates work the same way, unauthenticated.

Then trust Caddy's local CA on the devices you browse from, or every page warns:

```bash
cd /opt/homelab/hosts/roadkill
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

Ingest fires daily at 07:30 host time (catch-up after downtime via `Persistent`),
backup at 03:30 with a randomised delay. `OnCalendar` is local time; add
`TimeZone=` to a timer if the host runs UTC and you schedule in local time. The
ingest hour must sit **after** the report mail arrives — TouchBistro sends it
Sunday shortly after 07:00, and a timer that fires before it finds nothing new
and defers the week by a day. Daily rather than Sunday-only, so a report that
arrives late is picked up on the next firing rather than the next week.

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

Expect `114`, or one higher once the timer has loaded a week the corpus did not
contain. Compare against `SELECT count(*) FROM core.report` on production rather
than trusting either number, and read `0` as a failed restore, not an empty
database. The dump includes the `analytics` views, so a restored database is
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
| Edge proxy changes | `cd /opt/homelab/hosts/roadkill && docker compose up -d` |

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

### Upgrading Metabase

`compose.yaml` pins an exact Metabase version. A new version migrates its
application database (dashboards, questions, users) in place on first start, and
an older version refuses to start against a newer schema — so there is no
downgrade except restoring a backup. Never pin backwards, and back up first:

```bash
cd /opt/barkeep
# The version that is actually running, asked of Metabase itself.
curl -fsS http://127.0.0.1:3000/api/session/properties \
  | grep -o '"tag":"v[^"]*"' || echo "could not read the running version"
sudo systemctl start barkeep-backup
```

Do not read the version from `docker inspect … .Config.Image`: that prints the
reference the container was *created* with — on a host installed from `latest`
it prints `metabase/metabase:latest`, which is the question, not the answer.

The version it prints must be **older than or equal to** the new pin. A host
that started from `latest` may already be ahead of an older pin; check before
editing anything. Then change the `image:` tag, and:

```bash
docker compose -f compose.yaml -f deploy/compose.lan.yaml pull metabase
docker compose -f compose.yaml -f deploy/compose.lan.yaml up -d metabase
docker compose -f compose.yaml -f deploy/compose.lan.yaml logs -f metabase
```

Wait for `Metabase Initialization COMPLETE`. The first start after a version
change runs migrations and can take a few minutes; interrupting it is how a
half-migrated application database happens.

### Metabase AI against a LAN model

Metabase's AI features take any OpenAI-compatible endpoint, which is how you
point them at a model on another machine (Open WebUI, Ollama, vLLM). Metabase
refuses private addresses unless told otherwise, so a compromised admin account
cannot redirect the LLM call at the database host or at loopback.

Set the narrowest value that works, and only if you actually use the feature:

```bash
# /opt/barkeep/.env
MB_LLM_ALLOWED_NETWORKS=allow-private
```

then recreate — the variable is read from the environment at start, so
`restart` does not apply it:

```bash
docker compose -f compose.yaml -f deploy/compose.lan.yaml up -d metabase
docker compose -f compose.yaml -f deploy/compose.lan.yaml exec -T metabase \
  printenv MB_LLM_ALLOWED_NETWORKS
```

In Metabase, the base URL is the vendor's OpenAI-compatible path — Open WebUI
serves it under `/api`, so `http://pc.lan:8080/api`, with the key from Open WebUI
→ Settings → Account → API Keys. Check the endpoint from a container before
blaming the guard:

```bash
docker run --rm --network barkeep_default curlimages/curl \
  -sS --max-time 5 http://pc.lan:8080/api/version
```

`allow-private` permits every RFC1918 destination, not just the model, and the
feature sends table and column metadata (and sample rows when it needs them) to
that endpoint. A model on the LAN keeps the bar's numbers on the LAN; a weak one
produces plausible wrong SQL that still has to be reviewed.

### Tinfoil proxy

Tinfoil's proxy is an OpenAI-compatible endpoint in front of their verifiable
inference, so Metabase can be pointed at it without a model running anywhere on
the LAN. It is **not** part of the pipeline and does not belong in `compose.yaml`:
it is host infrastructure, like Caddy, and it lives in the same
[`homelab`](https://github.com/derekmiller/homelab) repo as
`hosts/roadkill/compose.tinfoil.yaml`. It runs as its own Compose project
(`name: tinfoil`), so it never interferes with the Caddy stack:

```bash
cd /opt/homelab/hosts/roadkill
docker compose -f compose.tinfoil.yaml up -d
docker compose -f compose.tinfoil.yaml ps
```

It joins the external `edge` network, which Metabase already joins through
`deploy/compose.lan.yaml`. Test it from that network rather than from the host,
because that is the path Metabase actually uses:

```bash
docker run --rm --network edge curlimages/curl \
  -sS --max-time 10 http://tinfoil:3301/v1/models
```

The `127.0.0.1:3301` publish is for `curl` while debugging; nothing on the LAN
can reach it, and `ufw` needs no change. In Metabase the base URL is
`http://tinfoil:3301/v1` and the model is one of the ids that `/v1/models`
returned. `MB_LLM_ALLOWED_NETWORKS=allow-private` covers it, since Docker bridge
addresses are RFC1918.

The proxy verifies attestation against Tinfoil's servers, so this container needs
**outbound** access to the internet — the one direction `ufw` does not restrict.
Unlike a model on the LAN, prompts and the schema metadata Metabase sends leave
this host; that is the trade for not running a model yourself.

## 10. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `error: cannot connect to IMAP host` | host DNS or egress: `getent hosts imap.gmail.com` on the host |
| `error: missing IMAP configuration: SENDER` | `IMAP_SENDER` empty in `.env` |
| `error: cannot read secret file …` or `Permission denied` for `secrets/imap_password` | the container reads the bind-mounted file as UID **10001**, so the host owner and mode are what matter: file `10001:10001` `400`. `barkeep` does not read it and `chgrp barkeep` `640` cannot work |
| IMAP `[AUTHENTICATIONFAILED]` | the app password is not exactly 16 bytes: `sudo stat -c '%s bytes' secrets/imap_password` catches a trailing newline (17) or Google's grouped display with spaces (19) |
| Ingest prints `note: no report mail matched` | the label is empty or the Gmail filter is not applying it; check the filter, and check Spam — filters skip spam unless the filter says otherwise |
| Ingest prints `warning: newest loaded report ended N days ago` | mail is arriving somewhere the poller does not look: wrong label, wrong mailbox, or `IMAP_SENDER` no longer matching |
| `502` from Caddy | Metabase not on `edge`: start with `deploy/compose.lan.yaml` |
| Certificate warning in the browser | Caddy's root CA not installed on that device (step 5) |
| `no such host` for `barkeep.roadkill.lan` | Pi-hole record missing, or the client caches the old answer |
| Timer never fires | `systemctl list-timers barkeep-ingest.timer`; blank `NEXT` means not enabled |
| Backfill loads `0 files` | `data/raw` empty on the host |
| `PermissionError: … '/data/raw'` | corpus not readable by the container's UID 10001; `sudo chmod -R a+rX data/raw` |
| `No services to build` | `build` ran without `--profile ingest`; Compose skips profile-gated services |
| Metabase AI: `host … is on a network Metabase is not allowed to connect to` | `MB_LLM_ALLOWED_NETWORKS=allow-private` in `.env`, then `up -d metabase` to recreate (not `restart`) |
| Weeks doubled under two venues | `BARKEEP_VENUE_ALIASES` missing from the host `.env` |
