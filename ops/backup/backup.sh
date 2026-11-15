#!/usr/bin/env bash
# ops/backup/backup.sh — Script diario de backup y cifrado age (ARCHITECTURE.md §6, H-08b)
set -euo pipefail
umask 077

BACKUP_DIR="${BACKUP_DIR:-/backups}"
TIMESTAMP="$(date +'%Y%m%d_%H%M%S')"
DUMP_NAME="finanzas_${TIMESTAMP}.dump"
DUMP_PATH="${BACKUP_DIR}/${DUMP_NAME}"
ENC_PATH="${DUMP_PATH}.age"
FINAL_PATH="${ENC_PATH}"
MANIFEST_PATH="${DUMP_PATH}.manifest.json"
MANIFEST_ENC="${ENC_PATH}.manifest.age"

# Nunca dejar un dump en texto plano ni archivo temporal en staging ante cualquier salida o falla
cleanup() {
    rm -f "${DUMP_PATH:-}" "${FINAL_PATH:-}" "${MANIFEST_PATH:-}" "${MANIFEST_ENC:-}"
}
trap cleanup EXIT

PGDATABASE="${POSTGRES_DB:-finanzas}"
PGUSER="${POSTGRES_USER:-finanzas_app}"
PGHOST="${POSTGRES_HOST:-localhost}"
PGPORT="${POSTGRES_PORT:-5432}"

if [ -z "${AGE_PUBLIC_KEY:-}" ]; then
    echo "ERROR: AGE_PUBLIC_KEY no está configurada en el entorno. Abortando backup por seguridad." >&2
    exit 1
fi
AGE_RECIPIENT="${AGE_PUBLIC_KEY}"
RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive:finanzas_backups}"
BACKUP_DESTINATION="local"
REMOTE_STATUS="skipped"

mkdir -p "${BACKUP_DIR}"

source "$(dirname "${BASH_SOURCE[0]}")/rclone_config.sh"
prepare_rclone_config

echo "[$(date -Iseconds)] Iniciando pg_dump para ${PGDATABASE}..."
PYTHON_BIN="$(command -v python3 || command -v python)"
"${PYTHON_BIN}" -m ops.backup.snapshot create "${DUMP_PATH}" "${MANIFEST_PATH}"

echo "[$(date -Iseconds)] Cifrando con age..."
if ! command -v age >/dev/null 2>&1; then
    echo "ERROR: age no está instalado o no se encuentra en PATH. Abortando backup por seguridad." >&2
    exit 1
fi

age -r "${AGE_RECIPIENT}" -o "${ENC_PATH}" "${DUMP_PATH}"
age -r "${AGE_RECIPIENT}" -o "${MANIFEST_ENC}" "${MANIFEST_PATH}"
rm -f "${DUMP_PATH}"

FILE_SIZE=$(wc -c < "${FINAL_PATH}" | tr -d '[:space:]')
echo "[$(date -Iseconds)] Backup generado: ${FINAL_PATH} (${FILE_SIZE} bytes)"

# Copia estructurada en carpetas daily, weekly (domingos), monthly (día 1)
DAY_OF_WEEK="$(date +'%u')"   # 1=Lunes, 7=Domingo
DAY_OF_MONTH="$(date +'%d')"  # 01..31

mkdir -p "${BACKUP_DIR}/daily"
mkdir -p "${BACKUP_DIR}/weekly"
mkdir -p "${BACKUP_DIR}/monthly"

DAILY_TARGET="${BACKUP_DIR}/daily/$(basename "${FINAL_PATH}")"
cp "${FINAL_PATH}" "${DAILY_TARGET}"
cp "${MANIFEST_ENC}" "${DAILY_TARGET}.manifest.age"

if [ "${DAY_OF_WEEK}" -eq 7 ]; then
    echo "[$(date -Iseconds)] Archivando copia semanal..."
    cp "${FINAL_PATH}" "${BACKUP_DIR}/weekly/"
    cp "${MANIFEST_ENC}" "${BACKUP_DIR}/weekly/"
fi

if [ "${DAY_OF_MONTH}" = "01" ]; then
    echo "[$(date -Iseconds)] Archivando copia mensual..."
    cp "${FINAL_PATH}" "${BACKUP_DIR}/monthly/"
    cp "${MANIFEST_ENC}" "${BACKUP_DIR}/monthly/"
fi

# Función de retención local basada en CANTIDAD de copias (nunca borra si hay <= keep_count)
prune_local_by_count() {
    local target_dir="$1"
    local keep_count="$2"
    local start_idx=$((keep_count + 1))

    ls -1t "${target_dir}"/finanzas_*.dump.age 2>/dev/null | tail -n +"${start_idx}" | while IFS= read -r old_file; do
        [ -n "${old_file}" ] && rm -f "${old_file}" "${old_file}.manifest.age"
    done || true
}

# Función de retención remota en Google Drive basada en CANTIDAD de copias
prune_remote_by_count() {
    local sub_dir="$1"
    local keep_count="$2"
    local start_idx=$((keep_count + 1))

    rclone lsf "${RCLONE_REMOTE}/${sub_dir}" --files-only 2>/dev/null \
        | grep '^finanzas_.*\.dump\.age$' \
        | sort -r \
        | tail -n +"${start_idx}" \
        | while IFS= read -r old_remote; do
            if [ -n "${old_remote}" ]; then
                rclone deletefile "${RCLONE_REMOTE}/${sub_dir}/${old_remote}"
                rclone deletefile "${RCLONE_REMOTE}/${sub_dir}/${old_remote}.manifest.age"
            fi
        done || true
}

# Se conserva el respaldo local aunque falle Drive. El error queda visible y persistido.
upload_remote() {
    echo "[$(date -Iseconds)] Copiando a Google Drive (${RCLONE_REMOTE})..."
    rclone copy --drive-stop-on-upload-limit "${FINAL_PATH}" "${RCLONE_REMOTE}/daily/" || return 1
    rclone copy --drive-stop-on-upload-limit "${MANIFEST_ENC}" "${RCLONE_REMOTE}/daily/" || return 1
    prune_remote_by_count "daily" 14

    if [ "${DAY_OF_WEEK}" -eq 7 ]; then
        rclone copy --drive-stop-on-upload-limit "${FINAL_PATH}" "${RCLONE_REMOTE}/weekly/" || return 1
        rclone copy --drive-stop-on-upload-limit "${MANIFEST_ENC}" "${RCLONE_REMOTE}/weekly/" || return 1
        prune_remote_by_count "weekly" 8
    fi
    if [ "${DAY_OF_MONTH}" = "01" ]; then
        rclone copy --drive-stop-on-upload-limit "${FINAL_PATH}" "${RCLONE_REMOTE}/monthly/" || return 1
        rclone copy --drive-stop-on-upload-limit "${MANIFEST_ENC}" "${RCLONE_REMOTE}/monthly/" || return 1
        prune_remote_by_count "monthly" 12
    fi
}

if command -v rclone >/dev/null 2>&1 && rclone listremotes 2>/dev/null | grep -Fxq "${RCLONE_REMOTE%%:*}:"; then
    if (set -e; upload_remote); then
        BACKUP_DESTINATION="drive"
        REMOTE_STATUS="ok"
        rm -f "${BACKUP_DIR}/remote_failure"
    else
        REMOTE_STATUS="failed"
    fi
fi

if [ "${REMOTE_STATUS}" != "ok" ]; then
    printf '%s\n' "${REMOTE_STATUS}" > "${BACKUP_DIR}/remote_failure"
    echo "ERROR: copia remota ${REMOTE_STATUS}; solo se conservó el backup local." >&2
fi

# Política de retención local por cantidad de copias: 14 diarios, 8 semanales, 12 mensuales
prune_local_by_count "${BACKUP_DIR}/daily" 14
prune_local_by_count "${BACKUP_DIR}/weekly" 8
prune_local_by_count "${BACKUP_DIR}/monthly" 12

# Registrar la corrida exitosa en la BD para que la alerta de 36 h de /healthz funcione (RN-38)
PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [ -n "${PYTHON_BIN}" ]; then
    DAILY_TARGET="${DAILY_TARGET}" FILE_SIZE="${FILE_SIZE}" BACKUP_DESTINATION="${BACKUP_DESTINATION}" REMOTE_STATUS="${REMOTE_STATUS}" "${PYTHON_BIN}" -c "import os; from datetime import UTC, datetime; from app.core.db import get_session_factory; from app.services.backup_service import record_backup_run; sf = get_session_factory(); db = sf(); record_backup_run(db, run_at=datetime.now(UTC), dump_path=os.environ['DAILY_TARGET'], size_bytes=int(os.environ['FILE_SIZE']), encrypted=True, destination=os.environ['BACKUP_DESTINATION'], remote_status=os.environ['REMOTE_STATUS']); db.close()" || echo "[$(date -Iseconds)] Advertencia: no se pudo registrar record_backup_run en la BD." >&2
fi

echo "[$(date -Iseconds)] Backup finalizado exitosamente."
