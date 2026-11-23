#!/usr/bin/env bash
#
# Preflight for a barkeep host. Read-only: changes nothing, prints a report and
# exits nonzero only when a FAIL would make the deployment wrong (as opposed to
# merely inconvenient).
#
#   ./scripts/preflight.sh
#
set -uo pipefail

pass=0
warn=0
fail=0

ok() {
    printf '  PASS  %s\n' "$1"
    pass=$((pass + 1))
}

note() {
    printf '  WARN  %s\n' "$1"
    warn=$((warn + 1))
}

bad() {
    printf '  FAIL  %s\n' "$1"
    fail=$((fail + 1))
}

section() {
    printf '\n%s\n' "$1"
}

section "Operating system"
if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    ok "${PRETTY_NAME:-unknown} ($(uname -m))"
else
    bad "no /etc/os-release; this is not a Linux host"
fi

if [[ -d /run/systemd/system ]]; then
    ok "systemd is the init system"
else
    bad "systemd is not running; the ingest timer needs it"
fi

section "Docker"
if command -v docker >/dev/null 2>&1; then
    ok "docker CLI at $(command -v docker)"
else
    bad "docker is not installed"
fi

if compose_version=$(docker compose version --short 2>/dev/null); then
    ok "docker compose ${compose_version}"
else
    bad "docker compose (v2 plugin) is not available"
fi

if docker info >/dev/null 2>&1; then
    ok "daemon reachable as $(id -un)"
else
    if id -nG | tr ' ' '\n' | grep -qx docker; then
        bad "daemon unreachable despite docker group membership"
    else
        bad "$(id -un) is not in the docker group (or has not re-logged in)"
    fi
fi

if [[ -f /etc/docker/daemon.json ]] \
    && grep -q '"log-driver"' /etc/docker/daemon.json; then
    ok "daemon.json sets a log driver"
else
    note "no log rotation configured in /etc/docker/daemon.json"
fi

if command -v rsync >/dev/null 2>&1; then
    ok "rsync present"
else
    note "rsync absent: off-box backups and data/raw copies need it"
fi

section "Resources"
mem_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
mem_gb=$((mem_kb / 1024 / 1024))
if ((mem_gb >= 2)); then
    ok "${mem_gb} GiB RAM"
else
    note "${mem_gb} GiB RAM: Metabase needs a heap cap (JAVA_OPTS=-Xmx768m)"
fi

cores=$(nproc 2>/dev/null || echo 0)
if ((cores >= 2)); then
    ok "${cores} CPUs"
else
    note "${cores} CPU: Metabase and Postgres will contend"
fi

avail=$(df -P -B1 /var/lib/docker 2>/dev/null | awk 'NR==2 {print $4}')
if [[ -z ${avail:-} ]]; then
    avail=$(df -P -B1 / | awk 'NR==2 {print $4}')
fi
avail_gb=$(( ${avail:-0} / 1024 / 1024 / 1024 ))
if ((avail_gb >= 20)); then
    ok "${avail_gb} GiB free where Docker stores data"
else
    note "${avail_gb} GiB free: images plus pgdata need headroom"
fi

section "Network"
for port in 80 443; do
    if ss -ltnH "sport = :${port}" 2>/dev/null | grep -q .; then
        note "port ${port} is already in use; Caddy cannot bind it"
    else
        ok "port ${port} is free"
    fi
done

if command -v ufw >/dev/null 2>&1; then
    if ufw status 2>/dev/null | grep -q '^Status: active'; then
        ok "ufw is active"
    else
        note "ufw is not active"
    fi
else
    note "ufw is not installed"
fi

section "Clock"
if [[ $(timedatectl show -p NTPSynchronized --value 2>/dev/null) == "yes" ]]; then
    ok "clock is NTP-synchronised"
else
    note "clock is not NTP-synchronised; TLS and the timer depend on it"
fi
tz=$(timedatectl show -p Timezone --value 2>/dev/null || true)
if [[ -n $tz ]]; then
    ok "timezone ${tz}"
else
    note "timezone unknown (no timedatectl)"
fi

printf '\n%d passed, %d warnings, %d failed\n' "$pass" "$warn" "$fail"
if ((fail > 0)); then
    printf 'Fix the failures before deploying.\n'
    exit 1
fi
printf 'Warnings are judgement calls; read them before ignoring them.\n'
