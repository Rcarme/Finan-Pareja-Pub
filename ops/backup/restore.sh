#!/usr/bin/env bash
# ops/backup/restore.sh — Restauración formal desde dump cifrado age (ARCHITECTURE.md §6, H-08c)
set -euo pipefail
umask 077

if [ "$#" -lt 1 ]; then
    echo "Uso: $0 <archivo_backup.dump[.age]> [target_db] [age_identity_key_file]" >&2
    exit 1
fi

BACKUP_FILE="$1"
TARGET_DB="${2:-${POSTGRES_DB:-finanzas}}"
KEY_FILE="${3:-${AGE_KEY_FILE:-key.txt}}"

# Bloquear restauración destructiva sobre la base de producción salvo confirmación explícita
if [[ "${TARGET_DB}" = postgres || "${TARGET_DB}" = template0 || "${TARGET_DB}" = template1 ]]; then
    echo "ERROR: Nunca restaurar sobre bases de sistema." >&2
    exit 1
fi
if { [ "${TARGET_DB}" = "finanzas" ] || [ "${TARGET_DB}" = "${POSTGRES_DB:-finanzas}" ]; } && [ "${RESTORE_CONFIRM_PROD:-}" != "YES" ]; then
    echo "ERROR: Restaurar sobre la base de producción 'finanzas' requiere definir RESTORE_CONFIRM_PROD=YES." >&2
    exit 1
fi

# Usar el rol dueño (finanzas_owner) para que pg_restore --clean tenga permisos de DROP/CREATE
PGUSER="${POSTGRES_OWNER_USER:-${POSTGRES_MIGRATION_USER:-finanzas_owner}}"
PGHOST="${POSTGRES_HOST:-localhost}"
PGPORT="${POSTGRES_PORT:-5432}"

if [ ! -f "${BACKUP_FILE}" ]; then
    echo "Error: El archivo de backup '${BACKUP_FILE}' no existe." >&2
    exit 1
fi

RESTORE_DUMP="${BACKUP_FILE}"

# Si el archivo está cifrado con age (.age), descifrarlo temporalmente con limpieza garantizada
if [[ "${BACKUP_FILE}" == *.age ]]; then
    if [ ! -f "${KEY_FILE}" ]; then
        echo "Error: Se requiere la clave privada age (${KEY_FILE}) para descifrar el archivo." >&2
        exit 1
    fi
    RESTORE_DUMP="/tmp/decrypted_$(basename "${BACKUP_FILE}" .age)"
    trap 'rm -f "${RESTORE_DUMP}"' EXIT
    echo "Descifrando con age..."
    age -d -i "${KEY_FILE}" -o "${RESTORE_DUMP}" "${BACKUP_FILE}"
fi

echo "Restaurando ${RESTORE_DUMP} en la base ${TARGET_DB} con rol ${PGUSER}..."
pg_restore -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${TARGET_DB}" --clean --if-exists --no-owner "${RESTORE_DUMP}"

echo "Restauración completada exitosamente en ${TARGET_DB}."
