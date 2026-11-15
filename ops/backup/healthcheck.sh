#!/usr/bin/env bash
# El archivo debe renovarse: su mera existencia no detecta un worker bloqueado.
set -euo pipefail
HEARTBEAT_FILE="${HEARTBEAT_FILE:-/tmp/backup_worker_healthy}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
if [ -d "${BACKUP_DIR}/task_failures" ] \
    && find "${BACKUP_DIR}/task_failures" -type f -print | grep -q .; then
    exit 1
fi
find "${HEARTBEAT_FILE}" -maxdepth 0 -type f -mmin -3 -print 2>/dev/null | grep -q .
