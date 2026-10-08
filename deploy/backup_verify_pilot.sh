#!/usr/bin/env bash
# Back up the Docker pilot and prove that both archives can be restored.
set -Eeuo pipefail
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${REAICRM_PROJECT_ROOT:-$script_dir/..}"
if [[ ! -f .env.docker ]]; then
    echo "Run this on the pilot VPS, where .env.docker is present." >&2
    exit 1
fi

backup_root="${1:-$HOME/reaicrm-backups}"
result_file="${2:-}"
if (( $# > 2 )); then
    echo "Usage: backup_verify_pilot.sh [absolute-backup-root] [absolute-result-file]" >&2
    exit 2
fi
if [[ "$backup_root" != /* || -L "$backup_root" ]]; then
    echo "The backup directory must be an absolute path and not a symlink." >&2
    exit 1
fi
if [[ -n "$result_file" && ( "$result_file" != /* || ! -f "$result_file" || -L "$result_file" ) ]]; then
    echo "The result file must be an existing absolute regular file, not a symlink." >&2
    exit 2
fi
mkdir -p -- "$backup_root"
chmod 700 -- "$backup_root"

sudo -v
compose=(sudo docker compose --env-file .env.docker --profile mail)
"${compose[@]}" config --quiet
"${compose[@]}" exec -T proxy sh -c 'command -v tar >/dev/null'
sudo docker image inspect postgres:17-bookworm >/dev/null
running_services="$("${compose[@]}" ps --status running --services)"
writers=()
for service in web outbound publications account-emails; do
    if grep -Fxq "$service" <<< "$running_services"; then
        writers+=("$service")
    fi
done

backup_dir="$(mktemp -d "$backup_root/pilot-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
paused=0
restore_container=""

on_exit() {
    local result=$?
    trap - EXIT
    if (( paused && ${#writers[@]} )); then
        "${compose[@]}" start "${writers[@]}" || result=1
    fi
    if [[ -n "$restore_container" ]]; then
        sudo docker rm -f "$restore_container" >/dev/null 2>&1 || true
    fi
    if (( result != 0 )); then
        echo "Backup verification failed. Incomplete files remain in $backup_dir; no VERIFIED marker was created." >&2
    fi
    exit "$result"
}
trap on_exit EXIT

echo "Pausing pilot writers for the database and media snapshot..."
if (( ${#writers[@]} )); then
    paused=1
    "${compose[@]}" stop "${writers[@]}"
fi

"${compose[@]}" exec -T db sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
    > "$backup_dir/database.dump"
"${compose[@]}" exec -T proxy tar -C /srv/media -czf - . > "$backup_dir/media.tar.gz"
test -s "$backup_dir/database.dump"
test -s "$backup_dir/media.tar.gz"

count_sql='SELECT (SELECT count(*) FROM django_migrations), (SELECT count(*) FROM auth_user), (SELECT count(*) FROM properties_property), (SELECT count(*) FROM properties_lead), (SELECT count(*) FROM properties_propertyimage);'
"${compose[@]}" exec -T db sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -Atc "$1"' \
    -- "$count_sql" > "$backup_dir/source-counts.txt"

if (( ${#writers[@]} )); then
    "${compose[@]}" start "${writers[@]}"
fi
paused=0
echo "Pilot writers are running again; checking the saved archives in isolation..."

restore_container="reaicrm-restore-$(date -u +%s)-$$"
sudo docker run --rm -d --network none --name "$restore_container" \
    -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17-bookworm >/dev/null
ready=0
for _ in {1..60}; do
    if sudo docker exec "$restore_container" pg_isready -U postgres -d postgres >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
if (( ! ready )); then
    echo "Disposable PostgreSQL did not become ready." >&2
    exit 1
fi
sudo docker exec "$restore_container" createdb -U postgres -T template0 restorecheck
sudo docker exec -i "$restore_container" pg_restore -U postgres -d restorecheck \
    --exit-on-error --no-owner --no-acl < "$backup_dir/database.dump"
sudo docker exec "$restore_container" psql -X -U postgres -d restorecheck \
    -v ON_ERROR_STOP=1 -Atc "$count_sql" > "$backup_dir/restored-counts.txt"
cmp -s "$backup_dir/source-counts.txt" "$backup_dir/restored-counts.txt"

reference_sql="SELECT image FROM properties_propertyimage WHERE image <> '' UNION ALL SELECT landing_logo FROM properties_property WHERE landing_logo <> '' UNION ALL SELECT photo FROM properties_realtorprofile WHERE photo <> '';"
sudo docker exec "$restore_container" psql -X -U postgres -d restorecheck \
    -v ON_ERROR_STOP=1 -Atc "$reference_sql" > "$backup_dir/media-references.txt"
python3 "$script_dir/verify_media_archive.py" "$backup_dir/media.tar.gz" "$backup_dir/media-references.txt"

(
    cd "$backup_dir"
    sha256sum database.dump media.tar.gz > SHA256SUMS
    printf 'PostgreSQL and media restored successfully at %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > VERIFIED
)
echo "Verified backup: $backup_dir"
echo "Core row counts (migrations|users|properties|leads|images): $(cat "$backup_dir/restored-counts.txt")"
if [[ -n "$result_file" ]]; then
    printf '%s\n' "$backup_dir" > "$result_file"
fi
