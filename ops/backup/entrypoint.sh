#!/usr/bin/env bash
# ops/backup/entrypoint.sh — Demonio planificador de backups diarios y verificación semanal (ARCHITECTURE.md §6, H-08a)
set -euo pipefail

BACKUP_SCRIPT="/app/ops/backup/backup.sh"
VERIFY_SCRIPT="/app/ops/backup/verify.sh"
LIGHT_VERIFY_SCRIPT="/app/ops/backup/verify_light.sh"
HEARTBEAT_FILE="/tmp/backup_worker_healthy"
LAST_RUN_DATE=""
BACKUP_DIR="${BACKUP_DIR:-/backups}"
source "$(dirname "${BASH_SOURCE[0]}")/rclone_config.sh"
prepare_rclone_config
LAST_RUN_FILE="${BACKUP_DIR}/last_successful_backup_date"
mkdir -p "${BACKUP_DIR}"
if [ -f "${LAST_RUN_FILE}" ]; then
    LAST_RUN_DATE="$(cat "${LAST_RUN_FILE}")"
fi
LAST_VERIFY_FILE="${BACKUP_DIR}/last_successful_weekly_verification_date"
LAST_VERIFY_DATE=""
if [ -f "${LAST_VERIFY_FILE}" ]; then
    LAST_VERIFY_DATE="$(cat "${LAST_VERIFY_FILE}")"
fi
NEXT_VERIFY_SECONDS=0
TASK_FAILURE_DIR="${BACKUP_DIR}/task_failures"
mkdir -p "${TASK_FAILURE_DIR}"
TASK_PID=""
TASK_TIMEOUT="${BACKUP_TASK_TIMEOUT_SECONDS:-7200}"
if [[ ! "${TASK_TIMEOUT}" =~ ^[0-9]+([.][0-9]+)?$ ]] \
    || [[ "${TASK_TIMEOUT//[.0]/}" = "" ]]; then
    echo "ERROR: BACKUP_TASK_TIMEOUT_SECONDS debe ser positivo; no se permite desactivar el límite." >&2
    exit 1
fi

run_supervised() {
    local script="$1"
    local result=0
    # Heartbeat solo mientras una tarea vive y dentro de un límite explícito.
    timeout --kill-after=30s "${TASK_TIMEOUT}" bash "${script}" &
    TASK_PID=$!
    while kill -0 "${TASK_PID}" 2>/dev/null; do
        touch "${HEARTBEAT_FILE}"
        sleep "${BACKUP_HEARTBEAT_INTERVAL_SECONDS:-10}"
    done
    wait "${TASK_PID}" || result=$?
    TASK_PID=""
    if [ "${result}" -ne 0 ]; then
        printf '%s\n' "${result}" > "${TASK_FAILURE_DIR}/$(basename "${script}")"
    else
        rm -f "${TASK_FAILURE_DIR}/$(basename "${script}")"
    fi
    return "${result}"
}

cleanup() {
    if [ -n "${TASK_PID}" ]; then
        kill "${TASK_PID}" 2>/dev/null || true
    fi
    rm -f "${HEARTBEAT_FILE}"
    exit 0
}
trap cleanup SIGTERM SIGINT

touch "${HEARTBEAT_FILE}"
echo "[$(date -Iseconds)] Servicio de backup iniciado (zona horaria: ${TZ:-UTC})."

while true; do
    touch "${HEARTBEAT_FILE}"
    CURRENT_DATE="$(date +'%Y-%m-%d')"
    CURRENT_HOUR="$(date +'%H')"
    DAY_OF_WEEK="$(date +'%u')"

    # Ejecutar backup diario a partir de las 03:00 (una vez por fecha calendario)
    if [ "${CURRENT_HOUR}" -ge 3 ] && [ "${LAST_RUN_DATE}" != "${CURRENT_DATE}" ]; then
        echo "[$(date -Iseconds)] Ejecutando backup diario programado..."
        if run_supervised "${BACKUP_SCRIPT}"; then
            LAST_RUN_DATE="${CURRENT_DATE}"
            printf '%s\n' "${LAST_RUN_DATE}" > "${LAST_RUN_FILE}.tmp"
            mv "${LAST_RUN_FILE}.tmp" "${LAST_RUN_FILE}"
        else
            echo "[$(date -Iseconds)] ERROR en backup diario programado." >&2
        fi
    fi

    # Una verificación fallida puede reintentarse el mismo domingo, sin repetir el dump.
    if [ "${DAY_OF_WEEK}" -eq 7 ] && [ "${LAST_RUN_DATE}" = "${CURRENT_DATE}" ] \
        && [ "${LAST_VERIFY_DATE}" != "${CURRENT_DATE}" ] && [ "${SECONDS}" -ge "${NEXT_VERIFY_SECONDS}" ]; then
        NEXT_VERIFY_SECONDS=$((SECONDS + ${BACKUP_VERIFY_RETRY_SECONDS:-3600}))
        verification_ok=1
        if ! run_supervised "${LIGHT_VERIFY_SCRIPT}"; then
            verification_ok=0
            echo "ERROR en verificación remota del backup." >&2
        elif [ -n "${AGE_KEY_FILE:-}" ] && [ -f "${AGE_KEY_FILE}" ]; then
            if ! run_supervised "${VERIFY_SCRIPT}"; then
                verification_ok=0
                echo "ERROR en verificación de restauración." >&2
            fi
        else
            echo "Sin clave privada disponible; restauración no ejecutada. La verificación liviana no acredita restauración."
        fi
        if [ "${verification_ok}" -eq 1 ]; then
            LAST_VERIFY_DATE="${CURRENT_DATE}"
            printf '%s\n' "${LAST_VERIFY_DATE}" > "${LAST_VERIFY_FILE}.tmp"
            mv "${LAST_VERIFY_FILE}.tmp" "${LAST_VERIFY_FILE}"
        fi
    fi

    sleep 60 &
    wait $! || true
done
