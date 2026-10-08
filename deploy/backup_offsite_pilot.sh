#!/usr/bin/env bash
# Verify a local pilot backup, then upload it to an encrypted restic repository.
set -Eeuo pipefail
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
backup_root="${REAICRM_AUTOMATED_BACKUP_ROOT:-/var/backups/reaicrm-pilot}"
backup_root="${backup_root%/}"
if [[ "$backup_root" != /* || "$backup_root" == / || -L "$backup_root" ]]; then
    echo "The automated backup directory must be absolute and not a symlink." >&2
    exit 2
fi
: "${RESTIC_REPOSITORY:?Set RESTIC_REPOSITORY in the private service environment}"
: "${RESTIC_PASSWORD_FILE:?Set RESTIC_PASSWORD_FILE in the private service environment}"
case "$RESTIC_REPOSITORY" in
    s3:https://*|sftp:*) ;;
    *) echo "Use a remote S3 HTTPS or SFTP restic repository." >&2; exit 2 ;;
esac
if [[ "$RESTIC_PASSWORD_FILE" != /* || ! -f "$RESTIC_PASSWORD_FILE" ]]; then
    echo "RESTIC_PASSWORD_FILE must refer to an existing absolute file." >&2
    exit 2
fi
command -v restic >/dev/null
command -v flock >/dev/null
mkdir -p -- "$backup_root"
chmod 700 "$backup_root"

exec 9>"$backup_root/.offsite.lock"
if ! flock -n 9; then
    echo "Another offsite backup is already running." >&2
    exit 75
fi

result_file="$(mktemp "$backup_root/.result-XXXXXXXX")"
restic_output="$(mktemp "$backup_root/.restic-XXXXXXXX")"
trap 'rm -f -- "$result_file" "$restic_output"' EXIT

bash "$script_dir/backup_verify_pilot.sh" "$backup_root" "$result_file"
backup_dir="$(cat "$result_file")"
if [[ "$backup_dir" != "$backup_root"/pilot-* || ! -f "$backup_dir/VERIFIED" || -L "$backup_dir" ]]; then
    echo "The local backup was not verified; no remote upload was attempted." >&2
    exit 1
fi

restic backup --json --tag reaicrm-pilot "$backup_dir" > "$restic_output"
snapshot_id="$(python3 - "$restic_output" <<'PY'
import json
import sys

snapshot_id = None
with open(sys.argv[1], encoding="utf-8") as output:
    for line in output:
        message = json.loads(line)
        if message.get("message_type") == "summary":
            snapshot_id = message.get("snapshot_id")
if not snapshot_id or len(snapshot_id) != 64 or any(c not in "0123456789abcdef" for c in snapshot_id):
    raise SystemExit("restic did not report a valid snapshot ID")
print(snapshot_id)
PY
)"
restic check

# The source path changes on every run, so retention must group by host and tag.
restic forget --tag reaicrm-pilot --group-by host,tags \
    --keep-last 2 --keep-daily 7 --keep-weekly 4 --keep-monthly 3 --prune
restic check
printf '%s\n' "$snapshot_id" > "$backup_dir/OFFSITE"
python3 "$script_dir/prune_verified_pilot_backups.py" "$backup_root" --keep 3
echo "Encrypted offsite backup verified: $snapshot_id"
