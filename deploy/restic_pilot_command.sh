#!/usr/bin/env bash
# Run a manual restic command with the same private environment as the timer.
set -Eeuo pipefail
umask 077
environment_file=/etc/reaicrm-pilot-backup/restic.env
if [[ ! -f "$environment_file" || -L "$environment_file" ]]; then
    echo "Configure the private restic environment first." >&2
    exit 2
fi
set -a
# The file is root-owned (0600), and uses shell-compatible KEY='value' lines.
# shellcheck disable=SC1090
. "$environment_file"
set +a
: "${RESTIC_REPOSITORY:?Missing RESTIC_REPOSITORY}"
: "${RESTIC_PASSWORD_FILE:?Missing RESTIC_PASSWORD_FILE}"
exec restic "$@"
