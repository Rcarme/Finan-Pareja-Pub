#!/usr/bin/env bash
set -euo pipefail
umask 077
BACKUP_DIR="${BACKUP_DIR:-/backups}"
ADMIN_USER="${POSTGRES_OWNER_USER:-${POSTGRES_ADMIN_USER:-finanzas_owner}}"
SOURCE_DB="${POSTGRES_DB:-finanzas}"
TEMP_VERIFY_DB="${TEMP_VERIFY_DB:-finanzas_verify}"
PGHOST="${POSTGRES_HOST:-localhost}"
PGPORT="${POSTGRES_PORT:-5432}"
PYTHON_BIN="$(command -v python3 || command -v python)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
validate_verify_target() {
    if [[ ! "${TEMP_VERIFY_DB}" =~ ^finanzas_verify(_[a-zA-Z0-9]+)?$ ]] || [[ "${TEMP_VERIFY_DB}" = "${SOURCE_DB}" ]]; then
        echo "ERROR: destino inseguro; solo finanzas_verify[_sufijo]." >&2
        exit 1
    fi
}
validate_verify_target
CREATED=0
STATUS=failed
MANIFEST="$(mktemp)"
LATEST_BACKUP=""
cleanup() {
    local rc=$?
    trap - EXIT
    rm -f "${MANIFEST}"
    if [ "${CREATED}" = 1 ]; then
        dropdb -h "${PGHOST}" -p "${PGPORT}" -U "${ADMIN_USER}" --if-exists "${TEMP_VERIFY_DB}" || rc=1
    fi
    BACKUP_VERIFICATION_STATUS="${STATUS}" VERIFIED_BACKUP="${LATEST_BACKUP}" "${PYTHON_BIN}" -m ops.backup.record_verification || rc=1
    exit "${rc}"
}
trap cleanup EXIT
# Orden por timestamp del nombre, independiente de daily/weekly/monthly.
LATEST_BACKUP=$(find "${BACKUP_DIR}" -name 'finanzas_*.dump.age' -type f -printf '%f\t%p\n' | sort -r | sed -n '1s/^[^\t]*\t//p')
[ -n "${LATEST_BACKUP}" ] || { echo "No hay backups." >&2; exit 1; }
[ -f "${LATEST_BACKUP}.manifest.age" ] || { echo "Falta manifiesto: verificación no acreditada." >&2; exit 1; }
age -d -i "${AGE_KEY_FILE:-key.txt}" -o "${MANIFEST}" "${LATEST_BACKUP}.manifest.age"
# Nunca eliminar bases preexistentes: createdb falla si el destino está ocupado.
createdb -h "${PGHOST}" -p "${PGPORT}" -U "${ADMIN_USER}" -O "${ADMIN_USER}" "${TEMP_VERIFY_DB}"
CREATED=1
POSTGRES_OWNER_USER="${ADMIN_USER}" bash "${SCRIPT_DIR}/restore.sh" "${LATEST_BACKUP}" "${TEMP_VERIFY_DB}"
POSTGRES_USER="${ADMIN_USER}" "${PYTHON_BIN}" -m ops.backup.snapshot verify "${TEMP_VERIFY_DB}" "${MANIFEST}"
STATUS=ok
echo "Restauración idéntica al snapshot respaldado."
