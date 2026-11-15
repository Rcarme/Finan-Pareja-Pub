#!/usr/bin/env bash
# La configuración montada permanece intacta; los tokens renovados viven en /backups.
prepare_rclone_config() {
    local source_config="${RCLONE_CONFIG:-/etc/rclone/rclone.conf}"
    local writable_config="${BACKUP_DIR:-/backups}/.rclone.conf"
    if [ ! -f "${source_config}" ]; then
        return 0
    fi
    mkdir -p "${BACKUP_DIR:-/backups}"
    if [ "${source_config}" != "${writable_config}" ]; then
        if [ ! -f "${writable_config}" ] || [ "${source_config}" -nt "${writable_config}" ]; then
            cp "${source_config}" "${writable_config}"
        fi
    fi
    chmod 600 "${writable_config}"
    export RCLONE_CONFIG="${writable_config}"
}
