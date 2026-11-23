#!/usr/bin/env bash
#
# Dump the barkeep database, verify the dump, rotate old copies, and optionally
# copy the new one off the host.
#
# Run by deploy/barkeep-backup.service, but safe to run by hand:
#
#   ./scripts/backup.sh
#
# Environment:
#   BARKEEP_REPO_DIR         checkout to run compose in      (/opt/barkeep)
#   BARKEEP_BACKUP_DIR       where dumps are written          (/var/lib/barkeep-backup)
#   BARKEEP_BACKUP_KEEP      days of dumps to keep             (14)
#   BARKEEP_BACKUP_DATABASE  database name                    (barkeep)
#   BARKEEP_BACKUP_REMOTE    rsync target, e.g. user@mac:/vol/barkeep
#
# The dump is taken with `docker compose exec` against the running container, so
# it needs no credentials on the host and no published port.
#
set -euo pipefail

repo_dir="${BARKEEP_REPO_DIR:-/opt/barkeep}"
backup_dir="${BARKEEP_BACKUP_DIR:-/var/lib/barkeep-backup}"
keep="${BARKEEP_BACKUP_KEEP:-14}"
database="${BARKEEP_BACKUP_DATABASE:-barkeep}"
remote="${BARKEEP_BACKUP_REMOTE:-}"

cd "$repo_dir"
mkdir -p "$backup_dir"

stamp=$(date -u +%Y-%m-%dT%H%M%SZ)
target="${backup_dir}/${database}-${stamp}.sql.gz"

fail() {
    printf 'backup failed: %s\n' "$1" >&2
    rm -f "$target"
    exit 1
}

# pg_dump first writes the schema, so a bad DSN or a stopped container fails
# here rather than producing a plausible-looking empty dump.
if ! docker compose exec -T db pg_dump -U "${POSTGRES_USER:-barkeep}" \
    -d "$database" | gzip -9 >"$target"; then
    fail "pg_dump or gzip returned nonzero"
fi

[[ -s $target ]] || fail "dump is empty"

gzip -t "$target" || fail "dump is not valid gzip"

# pg_dump's plain format ends with this line, though PostgreSQL 17+ appends a
# \unrestrict command after it, so look at the tail rather than the last line.
# Checking it catches a truncated stream, which a container killed mid-dump can
# produce.
if ! zcat "$target" | tail -n 20 | grep -q 'PostgreSQL database dump complete'; then
    fail "dump is incomplete (no pg_dump completion marker)"
fi

size=$(du -h "$target" | cut -f1)
printf 'wrote %s (%s)\n' "$target" "$size"

# Rotation runs after the new dump is verified, so a failing backup never
# deletes the last good copy.
deleted=$(find "$backup_dir" -maxdepth 1 -type f -name '*.sql.gz' \
    -mtime +"$keep" -print -delete | wc -l)
printf 'rotated %s dump(s) older than %s days\n' "$deleted" "$keep"

if [[ -n $remote ]]; then
    command -v rsync >/dev/null 2>&1 \
        || fail "BARKEEP_BACKUP_REMOTE is set but rsync is not installed"
    rsync -a --partial "$target" "${remote%/}/" \
        || fail "off-box copy to ${remote} failed"
    printf 'copied to %s\n' "$remote"
fi
