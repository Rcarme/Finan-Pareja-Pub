#!/usr/bin/env bash
# Compara archivos cifrados sin clave privada; no acredita una restauración.
set -euo pipefail
umask 077

BACKUP_DIR="${BACKUP_DIR:-/backups}"
source "$(dirname "${BASH_SOURCE[0]}")/rclone_config.sh"
prepare_rclone_config
RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive:finanzas_backups}"
LATEST_BACKUP="$(find "${BACKUP_DIR}/daily" -maxdepth 1 -type f -name 'finanzas_*.dump.age' | sort -r | head -n 1)"

if [ -z "${LATEST_BACKUP}" ] || [ ! -s "${LATEST_BACKUP}" ]; then
    echo "ERROR: no hay backup cifrado no vacío para verificar." >&2
    exit 1
fi
MANIFEST="${LATEST_BACKUP}.manifest.age"
if [ ! -s "${MANIFEST}" ]; then
    echo "ERROR: falta el manifiesto cifrado no vacío del backup." >&2
    exit 1
fi

STAGING="$(mktemp -d)"
cleanup() {
    rm -f "${STAGING}/$(basename "${LATEST_BACKUP}")" "${STAGING}/$(basename "${MANIFEST}")"
    rmdir "${STAGING}"
}
trap cleanup EXIT
cp "${LATEST_BACKUP}" "${MANIFEST}" "${STAGING}/"

# Descargar para comparar hashes no depende del algoritmo ofrecido por el remoto.
rclone check "${STAGING}" "${RCLONE_REMOTE}/daily" --download --one-way
echo "Verificación liviana exitosa: existencia, tamaño y hash remotos. No acredita restauración."
